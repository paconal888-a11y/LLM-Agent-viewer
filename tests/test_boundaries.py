"""독립 검토에서 재현한 실행·검증 경계 결함의 회귀 검사."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from local_agent_lab.agent_loop import unexpected_location
from local_agent_lab.features import normalize_requirements
from local_agent_lab.harness import Harness, tools_for
from local_agent_lab.integrations import prepare as prepare_integrations, close_all
from local_agent_lab.policy import contains_value, same_argument, validate_tool_call
from local_agent_lab.runtime import RuntimeFailure
from local_agent_lab.toolkit import ToolContext
from local_agent_lab.trace import ROOT, Run
from test_harness import FixtureRuntime, response, tool_call


class CompletionValueTests(unittest.TestCase):
    def test_integer_valued_float_requirement_matches_an_integer_argument(self):
        normalized = normalize_requirements(
            [{"tool": "add_numbers", "arguments": {"a": 137.0, "b": 0.5, "c": True}}]
        )[0]["arguments"]
        self.assertEqual(normalized, {"a": 137, "b": 0.5, "c": True})
        self.assertIs(type(normalized["a"]), int)
        self.assertIs(type(normalized["c"]), bool)

    def test_json_numbers_compare_by_value_but_never_across_types(self):
        for actual, expected, same in (
            (3, 3.0, True),
            (3.0, 3, True),
            (1e2, 100, True),
            (1, True, False),
            (True, 1, False),
            (0, False, False),
            ("1", 1, False),
            (0.1 + 0.2, 0.3, False),
        ):
            with self.subTest(actual=actual, expected=expected):
                self.assertEqual(contains_value(actual, expected), same)
                self.assertEqual(same_argument("mcp_x", "value", actual, expected), same)

    def test_nested_results_still_compare_type_and_order(self):
        self.assertTrue(contains_value({"a": [1, {"b": 2.0}]}, {"a": [1.0, {"b": 2}]}))
        self.assertFalse(contains_value({"a": [1, 2]}, {"a": [2, 1]}))
        self.assertFalse(contains_value({"a": True}, {"a": 1}))

    def test_tool_context_resolves_the_workspace_once(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "real").mkdir()
            link = base / "link"
            try:
                os.symlink(base / "real", link, target_is_directory=True)
            except (OSError, NotImplementedError):
                link = base / "real" / ".." / "real"
            context = ToolContext(None, link, [], set(), 0, "x")
            self.assertEqual(context.workspace, (base / "real").resolve())

    def test_path_named_arguments_of_mcp_tools_stay_inside_the_workspace(self):
        # 이름이 path인 인자만 작업 공간 검사를 받는다. 다른 이름은 서버가 직접 제한해야 한다(EXTENDING.md).
        with tempfile.TemporaryDirectory() as folder:
            run = Run("x", "s", features={"writes": True})
            tool = {
                "function": {
                    "name": "mcp_demo_stat",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "file": {"type": "string"}},
                    },
                }
            }
            call = lambda **args: {  # noqa: E731
                "function": {"name": "mcp_demo_stat", "arguments": json.dumps(args)}
            }
            with self.assertRaises(ValueError):
                validate_tool_call(run, Path(folder), [tool], call(path="../outside"), None)
            _, args = validate_tool_call(run, Path(folder), [tool], call(file="../outside"), None)
            self.assertEqual(args, {"file": "../outside"})

    def test_unexpected_exception_records_type_and_location(self):
        try:
            {}["missing"]
        except KeyError as error:
            location = unexpected_location(error)
        self.assertEqual(location["type"], "KeyError")
        self.assertEqual(location["file"], "test_boundaries.py")
        self.assertEqual(location["function"], "test_unexpected_exception_records_type_and_location")
        self.assertEqual(unexpected_location(RuntimeFailure("모델 호출 8회 제한")), {})


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        shutil.copytree(ROOT / "demo_workspace", self.workspace)

    def tearDown(self):
        close_all()
        self.temp.cleanup()

    def harness(self, *responses):
        return Harness(self.workspace, self.root / "state", runtime=FixtureRuntime(*responses))

    def test_check_file_picks_the_checker_from_the_resolved_file_not_the_requested_string(self):
        # H-01: 'data/new.json\\\\'·대소문자 변형·심볼릭 링크 별칭으로 JSON 검사를 비껴가 거짓 통과하던 경로.
        broken = '{"ok":false'
        (self.workspace / "data" / "new.json").write_text(broken, encoding="utf-8")
        aliases = ["data/new.json\\", "data/NEW.JSON", "data/./new.json"]
        link = self.workspace / "notes" / "alias.txt"
        try:
            os.symlink(self.workspace / "data" / "new.json", link)
            aliases.append("notes/alias.txt")
        except (OSError, NotImplementedError):
            pass
        harness = self.harness()
        run = Run("검사", "checker", features={"rules": False})
        for alias in aliases:
            try:
                (self.workspace / "data" / "new.json").resolve(strict=True)
                target = (self.workspace / alias.replace("\\", "/")).resolve()
            except OSError:
                continue
            if not target.exists():
                continue  # 대소문자를 구분하는 파일시스템에서는 'NEW.JSON'이 다른 파일이다.
            result = json.loads(
                harness.dispatch_tool(run, [], tool_call("check_file", path=alias), set())["content"]
            )
            self.assertEqual(result.get("path"), "data/new.json", alias)
            self.assertFalse(result.get("passed"), alias)
            self.assertNotEqual(result.get("checker"), "non-empty text", alias)
        # 전체 루프: 깨진 JSON을 쓰고 별칭으로 검사해도 완료되지 않는다.
        harness = self.harness(
            response({"tool_calls": [tool_call("write_file", path="data/new.json", content=broken)]}),
            response({"tool_calls": [tool_call("check_file", path="data/new.json\\")]}),
            *[response({"content": "검사 통과했습니다."}) for _ in range(4)],
        )
        run = Run("새 JSON 작성", "checker-loop", features={"writes": True, "rules": False})
        harness.execute(run)
        self.assertNotEqual(run.status, "completed")
        finals = [e["output"] for e in run.events if e["kind"] == "validation.final"]
        self.assertTrue(finals and not finals[-1]["passed"])

    def test_search_text_path_aliases_satisfy_requirements(self):
        # H-05: search_text의 path는 완료 조건 비교에서 정규화되지 않아 'notes/'가 거짓 실패였다.
        for alias in ("notes/", "./notes", "notes"):
            harness = self.harness(
                response({"tool_calls": [tool_call("search_text", query="하네스", path=alias)]}),
                response({"content": "찾았습니다."}),
            )
            run = Run(
                "검색",
                "search-" + alias.strip("./") + str(len(alias)),
                features={"rules": False, "memory": False},
                requirements=[{"tool": "search_text", "arguments": {"query": "하네스", "path": "notes"}}],
            )
            harness.execute(run)
            self.assertEqual(run.status, "completed", alias)
            self.assertEqual(run.model_calls, 2, alias)

    def test_malformed_function_field_is_a_model_format_error_not_a_crash(self):
        # H-08: function이 객체가 아니면 AttributeError로 실행 전체가 죽었다.
        harness = self.harness(
            response({"tool_calls": [{"id": "x", "type": "function", "function": ["read_file"]}]}),
        )
        run = Run("깨진 호출", "bad-call", features={"rules": False, "memory": False})
        harness.execute(run)
        self.assertEqual(run.status, "failed")
        self.assertIn("도구 호출 형식", run.error)

    def external_server(self, body):
        script = self.workspace / "server.py"
        script.write_text(body, encoding="utf-8")
        (self.workspace / "integrations.json").write_text(
            json.dumps(
                {
                    "mcp": {
                        "name": "ext",
                        "command": "{python}",
                        "args": ["-u", str(script)],
                        "allow_tools": ["rpc_error", "slow", "dup"],
                        "read_only_tools": ["rpc_error", "slow", "dup"],
                    }
                }
            ),
            encoding="utf-8",
        )

    def test_mcp_tool_call_rpc_error_and_timeout_are_tool_failures_not_run_failures(self):
        # H-03: JSON-RPC 오류·시간 초과가 run.error로 끝나 모델이 복구할 기회가 없었다.
        self.external_server(
            "import json, sys, time\n"
            "for line in sys.stdin:\n"
            "    m = json.loads(line)\n"
            "    if m['method'] == 'server/discover': result = {'supportedVersions': ['2026-07-28']}\n"
            "    elif m['method'] == 'tools/list': result = {'tools': [\n"
            "        {'name': 'rpc_error', 'inputSchema': {'type': 'object', 'properties': {}}},\n"
            "        {'name': 'slow', 'inputSchema': {'type': 'object', 'properties': {}}}]}\n"
            "    elif m['params']['name'] == 'rpc_error':\n"
            "        print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'error': {'code': -32602, 'message': 'bad params'}}), flush=True)\n"
            "        continue\n"
            "    else:\n"
            "        time.sleep(1.0)\n"
            "        result = {'structuredContent': {'done': True}}\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': result}), flush=True)\n"
        )
        harness = self.harness(
            response({"tool_calls": [tool_call("mcp_ext_rpc_error")]}),
            response({"content": "서버 오류를 확인했습니다."}),
        )
        run = Run("rpc", "mcp-rpc-error", features={"mcp": True, "rules": False, "memory": False})
        harness.execute(run)
        self.assertEqual(run.status, "completed")
        errors = [e for e in run.events if e["kind"] == "tool.result" and "error" in (e["output"] or {})]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["output"]["rpc_error"]["code"], -32602)
        self.assertFalse(errors[0]["output"]["executed_successfully"])
        tool_messages = [m for e in run.events if e["kind"] == "model.request" for m in e["input"]["messages"] if m.get("role") == "tool"]
        self.assertIn("-32602", tool_messages[-1]["content"])
        close_all()
        from local_agent_lab import integrations

        run = Run("slow", "mcp-slow", features={"mcp": True, "rules": False, "memory": False})
        prepare_integrations(run, self.workspace)
        client = next(iter(integrations._clients.values()))
        client.timeout = 0.2
        result = json.loads(harness.dispatch_tool(run, tools_for(run), tool_call("mcp_ext_slow"), set())["content"])
        self.assertFalse(result["executed_successfully"])
        self.assertIn("시간 초과", result["error"])
        self.assertEqual(result["side_effects"], "unknown")

    def test_duplicate_remote_tool_names_are_rejected(self):
        # H-10: 같은 이름을 두 번 광고하면 검증은 첫 schema, 실행은 마지막 handler로 갈렸다.
        self.external_server(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    m = json.loads(line)\n"
            "    if m['method'] == 'server/discover': result = {'supportedVersions': ['2026-07-28']}\n"
            "    elif m['method'] == 'tools/list': result = {'tools': [\n"
            "        {'name': 'dup', 'inputSchema': {'type': 'object', 'properties': {'x': {'type': 'string'}}}},\n"
            "        {'name': 'dup', 'inputSchema': {'type': 'object', 'properties': {'y': {'type': 'string'}}}}]}\n"
            "    else: result = {}\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': result}), flush=True)\n"
        )
        run = Run("dup", "mcp-dup", features={"mcp": True})
        with self.assertRaises(Exception) as caught:
            prepare_integrations(run, self.workspace)
        self.assertIn("같은 이름", str(caught.exception))
        self.assertEqual(run.extra_tools, [])


    def test_reader_failure_restarts_the_server_process_on_the_next_connection(self):
        # H-04: 서버가 stdout에 JSON이 아닌 줄을 쓰면 reader 스레드가 죽고 프로세스가 살아 있는 동안
        # 모든 후속 요청이 시간 초과였다. 다음 연결에서 프로세스를 다시 시작해야 한다.
        from local_agent_lab import integrations
        from local_agent_lab.stdio_rpc import StdioRpc

        script = self.workspace / "junk_server.py"
        script.write_text(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    m = json.loads(line)\n"
            "    if m['method'] == 'junk':\n"
            "        print('DEBUG: not json', flush=True)\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': {'ok': True}}), flush=True)\n",
            encoding="utf-8",
        )
        import sys

        client = StdioRpc([sys.executable, "-u", str(script)], str(self.workspace), "mcp", timeout=0.5)
        run = Run("junk", "junk")
        client.start(run)
        first_pid = client.process.pid
        self.assertEqual(client.request(run, "ping"), {"ok": True})
        with self.assertRaises(Exception):
            client.request(run, "junk")
        self.assertTrue(client.broken)
        run2 = Run("junk again", "junk-2")
        client.start(run2)
        self.assertNotEqual(client.process.pid, first_pid)
        self.assertEqual(client.request(run2, "ping"), {"ok": True})
        kinds = [e["kind"] for e in run2.events]
        self.assertIn("mcp.process.restart", kinds)
        self.assertIn("mcp.process.start", kinds)
        client.close()

    def test_stop_during_a_tool_leaves_an_interrupted_record_instead_of_vanishing(self):
        # H-09: 중지가 도구 실행 도중 도착하면 부수효과는 끝났지만 어떤 결과 이벤트도 남지 않았다.
        import threading
        import time

        from local_agent_lab import toolkit

        marker = []

        def slow_tool(ctx):
            marker.append("side effect done")
            time.sleep(0.3)
            return ctx.finish({"done": True})

        original = dict(toolkit.IMPLEMENTATIONS)
        toolkit.IMPLEMENTATIONS["add_numbers"] = lambda ctx, a, b: slow_tool(ctx)
        try:
            harness = self.harness(
                response({"tool_calls": [tool_call("add_numbers", a=1, b=2)]}),
                response({"content": "끝"}),
            )
            run = Run("느린 도구", "slow-stop", features={"rules": False, "memory": False})
            worker = threading.Thread(target=harness.execute, args=(run,), daemon=True)
            worker.start()
            deadline = time.monotonic() + 3
            while not marker and time.monotonic() < deadline:
                time.sleep(0.01)
            run.control("stop")
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(run.status, "stopped")
            interrupted = [e for e in run.events if e["kind"] == "tool.interrupted"]
            self.assertEqual(len(interrupted), 1)
            self.assertEqual(interrupted[0]["input"]["name"], "add_numbers")
            self.assertEqual(interrupted[0]["status"], "warning")
            self.assertFalse(any(e["kind"] == "tool.result" for e in run.events))
        finally:
            toolkit.IMPLEMENTATIONS.clear()
            toolkit.IMPLEMENTATIONS.update(original)


if __name__ == "__main__":
    unittest.main()
