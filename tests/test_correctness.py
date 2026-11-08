"""관찰 출처·검증·세션 예산·프로토콜 복구의 회귀 조건."""

import json
import shutil
import sys
import tempfile
import time
import unittest
import hashlib
from pathlib import Path

from local_agent_lab.harness import Harness
from local_agent_lab.context import describe_context
from local_agent_lab.context_memory import STATE_LIMIT
from local_agent_lab.integrations import prepare, close_all
from local_agent_lab.orchestration import inference_turn
from local_agent_lab.policy import contains_value, requirement_gate
from local_agent_lab.runtime import RuntimeFailure
from local_agent_lab.stdio_rpc import StdioRpc
from local_agent_lab.trace import ROOT, Run
from local_agent_lab.model_files import verify_snapshot
from local_agent_lab.web import Application
from test_harness import FixtureRuntime, response, tool_call


class CorrectnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        shutil.copytree(ROOT / "demo_workspace", self.workspace)

    def tearDown(self):
        close_all()
        self.temp.cleanup()

    def harness(self, runtime):
        return Harness(self.workspace, self.root / "state", runtime=runtime)

    def test_message_origin_is_not_inferred_from_api_role(self):
        runtime = FixtureRuntime(
            response({"content": "아직 실행하지 않은 답"}),
            response({"tool_calls": [tool_call("add_numbers", a=1, b=2)]}),
            response({"content": "3"}),
            response({"content": "검토한 답 3"}),
        )
        run = Run(
            "계산",
            "origins",
            mode="deep",
            requirements=[{"tool": "add_numbers", "arguments": {"a": 1, "b": 2}}],
        )
        self.harness(runtime).execute(run)
        self.assertEqual(run.status, "completed", run.error)
        request = [e for e in run.events if e["kind"] == "model.request"][-1]
        parts = request["output"]["segments"]
        expected = {
            "현재 요청": "user",
            "완료 조건": "harness",
            "미충족 조건 보완 요청": "harness",
            "집중 모드 점검 지시": "harness",
            "모델 응답": "model",
            "모델 도구 요청": "model",
            "도구 결과": "tool",
        }
        for label, origin in expected.items():
            matches = [p for p in parts if p["label"] == label]
            self.assertTrue(matches, label)
            self.assertTrue(all(p["origin"] == origin for p in matches), label)
            for part in matches:
                if part.get("source_event_id"):
                    self.assertLess(part["source_event_id"], request["id"])
        unknown = describe_context(
            Run("x", "unknown"), [{"role": "user", "content": "untracked"}], []
        )
        self.assertEqual(unknown[0]["origin"], "unknown")

    def test_nested_results_keep_types_and_array_order(self):
        for actual, expected in [
            ([1], [True]),
            ([0], [False]),
            (["1"], [1]),
            ([[{"x": 1}]], [[{"x": True}]]),
            ([1, 2], [2, 1]),
            ([1], [1, 2]),
        ]:
            with self.subTest(actual=actual, expected=expected):
                self.assertFalse(contains_value({"flags": actual}, {"flags": expected}))
        self.assertTrue(
            contains_value(
                {"flags": [{"x": True, "extra": 2}]}, {"flags": [{"x": True}]}
            )
        )

    def test_model_configuration_hash_is_checked_before_loading(self):
        path = self.root / "config.json"
        path.write_bytes(b'{"model_type":"qwen3"}')
        files = {
            "config.json": (
                path.stat().st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        }
        verify_snapshot(self.root, files)
        path.write_bytes(b'{"model_type":"other"}')
        with self.assertRaisesRegex(ValueError, "config.json"):
            verify_snapshot(self.root, files)

    def test_ab_comparison_detects_top_k_and_top_p_changes(self):
        app = Application(
            self.harness(
                FixtureRuntime(response({"content": "A"}), response({"content": "B"}))
            )
        )
        ident = app.experiments.start(
            "answer",
            left_engine={"top_k": 1, "top_p": 0.1},
            right_engine={"top_k": 100, "top_p": 1.0},
        )
        deadline = time.monotonic() + 8
        while app.experiment_active and time.monotonic() < deadline:
            with app.lock:
                runs = list(app.runs.values())
            if runs:
                with runs[-1].condition:
                    runs[-1].condition.wait(timeout=0.02)
        item = app.experiments.get(ident)
        self.assertEqual(item["status"], "completed", item.get("error"))
        self.assertFalse(item["controls"]["same_generation"])
        self.assertEqual(item["generation_diff"]["top_k"], {"A": 1, "B": 100})
        self.assertEqual(item["generation_diff"]["top_p"], {"A": 0.1, "B": 1.0})

    def test_path_aliases_match_latest_receipt_without_normalizing_text(self):
        run = Run(
            "check",
            "paths",
            requirements=[
                {
                    "tool": "check_file",
                    "arguments": {"path": "data/value.json"},
                    "result": {"passed": True},
                }
            ],
        )

        def receipt(path, passed=True):
            return {
                "name": "check_file",
                "arguments": {"path": path},
                "event_id": 1,
                "result": {"passed": passed},
            }

        for alias in ("./data/value.json", "data\\value.json", "data/./value.json"):
            self.assertEqual(requirement_gate(run, [receipt(alias)]), [])
        for other in (
            "../data/value.json",
            "/data/value.json",
            "data/other.json",
            "C:/data/value.json",
        ):
            self.assertTrue(requirement_gate(run, [receipt(other)]))
        self.assertTrue(
            requirement_gate(
                run, [receipt("data/value.json"), receipt("./data/value.json", False)]
            )
        )

    def test_large_completed_turn_is_archived_and_next_turn_still_runs(self):
        (self.workspace / "large.txt").write_text("가" * 20000, encoding="utf-8")
        runtime = FixtureRuntime(
            *[
                response({"tool_calls": [tool_call("read_file", path="large.txt")]})
                for _ in range(9)
            ],
            response({"content": "완료"}),
            response({"content": "다음 답"}),
            response({"content": "메모리 없이 답"}),
        )
        harness = self.harness(runtime)
        first = Run("read", "large", mode="deep")
        # 집중 모드의 추가 점검 대신 저장 계약을 단독으로 검사한다.
        first.mode = "single"
        first.max_model_calls = 16
        harness.execute(first)
        self.assertEqual(first.status, "completed", first.error)
        path = harness.history_path("large")
        self.assertLessEqual(path.stat().st_size, STATE_LIMIT)
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["turns"], [])
        archive = harness.state_dir / "conversations/large.jsonl"
        original = json.loads(archive.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(sum(m["role"] == "tool" for m in original["messages"]), 9)
        for memory in (True, False):
            next_run = Run("next", "large", features={"memory": memory})
            harness.execute(next_run)
            self.assertEqual(next_run.status, "completed", next_run.error)

    def test_legacy_oversize_state_is_recovered_even_with_memory_disabled(self):
        harness = self.harness(FixtureRuntime(response({"content": "ok"})))
        path = harness.history_path("legacy")
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "turns": [[{"role": "user", "content": "가" * 200000}]],
                    "turn_ids": ["legacy-id"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        run = Run("x", "legacy", features={"memory": False})
        harness.execute(run)
        self.assertEqual(run.status, "completed", run.error)
        self.assertLessEqual(path.stat().st_size, STATE_LIMIT)
        self.assertIn(
            "legacy-id",
            (harness.state_dir / "conversations/legacy.jsonl").read_text(
                encoding="utf-8"
            ),
        )

    def test_legacy_recovery_archives_raw_before_trimming_model_input(self):
        harness = self.harness(FixtureRuntime(response({"content": "ok"})))
        path = harness.history_path("legacy-on")
        path.parent.mkdir(parents=True)
        original = "가" * 200000
        path.write_text(
            json.dumps(
                {
                    "turns": [[{"role": "user", "content": original}]],
                    "turn_ids": ["old-id"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        run = Run("x", "legacy-on")
        harness.execute(run)
        self.assertEqual(run.status, "completed", run.error)
        records = [
            json.loads(line)
            for line in (harness.state_dir / "conversations/legacy-on.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(records[0]["messages"][0]["content"], original)
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["archive_turn_count"], 2
        )

    def test_plan_mcp_needs_host_read_only_classification(self):
        config_path = self.workspace / "integrations.json"
        config = json.loads(config_path.read_text())
        config["mcp"]["read_only_tools"] = []
        config_path.write_text(json.dumps(config), encoding="utf-8")
        run = Run("plan", "plan-mcp", mode="plan", features={"mcp": True})
        prepare(run, self.workspace)
        self.assertEqual(run.extra_tools, [])
        harness = self.harness(FixtureRuntime())
        result = json.loads(
            harness.dispatch_tool(run, [], tool_call("mcp_math_add", a=1, b=2), set())[
                "content"
            ]
        )
        self.assertFalse(result["executed_successfully"])
        self.assertFalse(
            any(
                e["kind"] == "mcp.send"
                and e["input"]["message"]["method"] == "tools/call"
                for e in run.events
            )
        )
        config["mcp"]["read_only_tools"] = ["add"]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        allowed = Run("plan", "plan-read-only", mode="plan", features={"mcp": True})
        prepare(allowed, self.workspace)
        result = json.loads(
            harness.dispatch_tool(
                allowed, [], tool_call("mcp_math_add", a=1, b=2), set()
            )["content"]
        )
        self.assertEqual(result["data"]["result"], 3)

    def test_queue_event_pause_is_not_counted_as_lock_wait(self):
        class Observed(Run):
            def emit(self, kind, *a, **kw):
                if kind == "agent.queue":
                    time.sleep(0.08)
                return super().emit(kind, *a, **kw)

        run = Observed("x", "queue")
        with inference_turn(self.harness(FixtureRuntime()), run, 1):
            pass
        wait = next(
            e["output"]["wait_ms"] for e in run.events if e["kind"] == "agent.compute"
        )
        self.assertLess(wait, 50)


class RpcRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        script = self.root / "server.py"
        script.write_text(
            """import sys, json, time
for line in sys.stdin:
    m = json.loads(line)
    p = m.get("params", {})
    time.sleep(p.get("delay", 0))
    if p.get("notify"):
        print(json.dumps({"jsonrpc":"2.0", "method":"notice"}), flush=True)
    if not p.get("silent"):
        print(json.dumps({"jsonrpc":"2.0", "id":m["id"], "result":{"id":m["id"]}}), flush=True)
""",
            encoding="utf-8",
        )
        self.client = StdioRpc(
            [sys.executable, "-u", str(script)], self.root, "mcp", timeout=0.08
        )
        self.run = Run("rpc", "rpc")
        self.client.start(self.run)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_late_reply_does_not_poison_following_requests(self):
        with self.assertRaises(RuntimeFailure):
            self.client.request(self.run, "slow", {"delay": 0.22})
        self.client.timeout = 1
        for identity in (2, 3, 4):
            self.assertEqual(self.client.request(self.run, "normal"), {"id": identity})
        self.assertEqual(
            sum(e["kind"] == "mcp.response.stale" for e in self.run.events), 1
        )
        self.assertEqual(sum(e["kind"] == "mcp.send" for e in self.run.events), 4)

    def test_observer_pause_does_not_expire_a_received_result(self):
        class Paused(Run):
            def emit(self, kind, *a, **kw):
                if kind == "mcp.receive":
                    time.sleep(0.16)
                return super().emit(kind, *a, **kw)

        run = Paused("pause", "rpc-pause")
        self.client.timeout = 0.12
        self.assertEqual(
            self.client.request(run, "notice", {"notify": True}), {"id": 1}
        )
        with self.assertRaises(RuntimeFailure):
            self.client.request(run, "never", {"silent": True})
