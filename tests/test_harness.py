import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from local_agent_lab.harness import (
    Harness,
    build_context,
    discover_skills,
    execute_tool,
)
from local_agent_lab.trace import ROOT, Run, RunStopped


def tool_call(tool_name, **args):
    return {
        "id": "test-call",
        "type": "function",
        "function": {"name": tool_name, "arguments": json.dumps(args)},
    }


def response(message):
    return {
        "choices": [
            {
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                "message": {"role": "assistant", **message},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


class FixtureRuntime:
    base_url = "http://127.0.0.1:1"

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def request(self, route, payload=None):
        self.requests.append((route, json.loads(json.dumps(payload))))
        if route == "/apply-template":
            return {"prompt": "fixture prompt"}
        if route == "/tokenize":
            return {"tokens": [{"id": 1, "piece": "fixture"}]}
        return next(self.responses)


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        shutil.copytree(ROOT / "demo_workspace", self.workspace)
        self.run = Run("문서를 요약해줘", "test-session", features={"writes": True})

    def tearDown(self):
        self.temp.cleanup()

    def test_context_records_file_bytes_and_actual_model_inclusion(self):
        messages, catalog = build_context(self.run, self.workspace, {})
        kinds = [event["kind"] for event in self.run.events]
        self.assertLess(kinds.index("file.open"), kinds.index("file.read"))
        self.assertLess(kinds.index("file.read"), kinds.index("file.decode"))
        self.assertLess(kinds.index("file.decode"), kinds.index("context.assemble"))
        self.assertIn("<project_rules", messages[0]["content"])
        self.assertEqual(messages[-1], {"role": "user", "content": self.run.prompt})
        self.assertEqual(len(catalog), 2)
        self.assertNotIn("마지막에 출처 파일 경로를 표시한다", messages[0]["content"])
        self.assertTrue(all(event["source"] for event in self.run.events))
        self.assertIn(
            "def read_traced",
            next(e for e in self.run.events if e["kind"] == "file.open")["source"][
                "code"
            ],
        )

    def test_recorded_data_is_a_snapshot_not_a_mutable_reference(self):
        value = {"messages": [{"content": "original"}]}
        self.run.emit("test", "test", "test", output=value)
        value["messages"][0]["content"] = "changed"
        self.assertEqual(
            self.run.events[0]["output"]["messages"][0]["content"], "original"
        )

    def test_path_escape_and_extra_tool_arguments_are_rejected(self):
        for call in (
            tool_call("read_file", path="../secret.txt"),
            tool_call("read_file", path="AGENTS.md", extra="bad"),
        ):
            result = execute_tool(self.run, self.workspace, [], call, set())
            self.assertFalse(json.loads(result["content"])["executed_successfully"])
        self.assertFalse(any(event["kind"] == "file.read" for event in self.run.events))

    def test_model_cannot_rewrite_the_rules(self):
        original = (self.workspace / "AGENTS.md").read_text(encoding="utf-8")
        result = execute_tool(
            self.run,
            self.workspace,
            [],
            tool_call("write_file", path="AGENTS.md", content="changed"),
            set(),
        )
        self.assertIn("error", json.loads(result["content"]))
        self.assertEqual(
            (self.workspace / "AGENTS.md").read_text(encoding="utf-8"), original
        )

    def test_skill_activation_returns_real_instructions(self):
        catalog = discover_skills(self.run, self.workspace)
        active = set()
        result = execute_tool(
            self.run,
            self.workspace,
            catalog,
            tool_call("load_skill", name="file-summary"),
            active,
        )
        self.assertIn("출처 파일 경로", json.loads(result["content"])["instructions"])
        self.assertIn("file-summary", active)

    def test_search_and_plan_record_actual_results_and_distinguish_plan_state(self):
        run = Run("검색 계획", "plan-search", features={"planning": True})
        result = execute_tool(
            run,
            self.workspace,
            [],
            tool_call("search_text", query="하네스", path="notes"),
            set(),
        )
        found = json.loads(result["content"])
        self.assertTrue(
            any(item["path"] == "notes/agent-guide.md" for item in found["matches"])
        )
        self.assertTrue(any(e["kind"] == "file.read" for e in run.events))
        execute_tool(
            run,
            self.workspace,
            [],
            tool_call("set_plan", steps="검색하기\n결과 확인하기"),
            set(),
        )
        original = next(
            e["output"]["plan"]
            for e in reversed(run.events)
            if e["kind"] == "tool.result"
        )
        result = execute_tool(
            run, self.workspace, [], tool_call("complete_plan_step", index=1), set()
        )
        self.assertEqual(original[0]["status"], "pending")
        self.assertEqual(json.loads(result["content"])["plan"][0]["status"], "done")
        self.assertFalse(json.loads(result["content"])["verified_work"])

    def test_write_and_check_do_not_execute_python(self):
        content = "raise RuntimeError('must not execute')\n"
        written = execute_tool(
            self.run,
            self.workspace,
            [],
            tool_call("write_file", path="examples/test.py", content=content),
            set(),
        )
        result = execute_tool(
            self.run,
            self.workspace,
            [],
            tool_call("check_file", path="examples/test.py"),
            set(),
        )
        self.assertTrue(json.loads(result["content"])["passed"])
        self.assertEqual(
            (self.workspace / "examples/test.py").read_text(encoding="utf-8"), content
        )
        self.assertEqual(
            json.loads(written["content"])["written_bytes"],
            (self.workspace / "examples/test.py").stat().st_size,
        )
        (self.workspace / "examples/test.py").write_text("return 1\n", encoding="utf-8")
        invalid = execute_tool(
            self.run,
            self.workspace,
            [],
            tool_call("check_file", path="examples/test.py"),
            set(),
        )
        self.assertFalse(json.loads(invalid["content"])["passed"])

    def test_claiming_work_does_not_satisfy_declared_requirements(self):
        runtime = FixtureRuntime(
            *[response({"content": "파일을 수정하고 검사했습니다."}) for _ in range(8)]
        )
        run = Run(
            "수정해줘",
            "required-missing",
            features={"writes": True},
            requirements=[
                {"tool": "write_file", "arguments": {"path": "data/new.json"}}
            ],
        )
        Harness(self.workspace, self.root / "state", runtime=runtime).execute(run)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.tool_calls, 0)
        self.assertEqual(run.answer, "")
        checks = [e for e in run.events if e["kind"] == "validation.requirements"]
        self.assertEqual(len(checks), 3)
        repeated = [e for e in run.events if e["kind"] == "validation.repeat"]
        self.assertEqual([e["output"]["streak"] for e in repeated], [1, 2, 3])
        self.assertTrue(repeated[-1]["output"]["blocked"])
        self.assertEqual(len(repeated[-1]["output"]["response_event_ids"]), 3)
        self.assertEqual(run.model_calls, 3)
        self.assertTrue(all(not e["output"]["passed"] for e in checks))
        self.assertFalse((self.workspace / "data/new.json").exists())

    def test_repeated_answer_guard_resets_after_a_new_tool_call(self):
        runtime = FixtureRuntime(
            response({"content": "done"}),
            response({"content": "done"}),
            response({"tool_calls": [tool_call("add_numbers", a=2, b=2)]}),
            response({"content": "done"}),
            response({"content": "done"}),
            response({"tool_calls": [tool_call("add_numbers", a=1, b=2)]}),
            response({"content": "done"}),
        )
        run = Run(
            "calculate",
            "repeat-progress",
            requirements=[{"tool": "add_numbers", "arguments": {"a": 1, "b": 2}}],
        )
        Harness(self.workspace, self.root / "state", runtime=runtime).execute(run)
        self.assertEqual(run.status, "completed", run.error)
        repeated = [e["output"] for e in run.events if e["kind"] == "validation.repeat"]
        self.assertEqual([item["streak"] for item in repeated], [1, 2, 1, 2])
        self.assertFalse(any(item["blocked"] for item in repeated))
        self.assertEqual(run.model_calls, 7)

    def test_different_rejected_answers_are_not_counted_as_identical_repeats(self):
        runtime = FixtureRuntime(
            response({"content": "A"}),
            response({"content": "B"}),
            response({"content": "A"}),
            response({"tool_calls": [tool_call("add_numbers", a=1, b=2)]}),
            response({"content": "done"}),
        )
        run = Run(
            "calculate",
            "repeat-different",
            requirements=[{"tool": "add_numbers", "arguments": {"a": 1, "b": 2}}],
        )
        Harness(self.workspace, self.root / "state", runtime=runtime).execute(run)
        self.assertEqual(run.status, "completed", run.error)
        self.assertEqual(
            [
                e["output"]["streak"]
                for e in run.events
                if e["kind"] == "validation.repeat"
            ],
            [1, 1, 1],
        )

    def test_rejected_answer_is_repaired_and_actual_results_satisfy_requirements(self):
        runtime = FixtureRuntime(
            response({"content": "저장했습니다."}),
            response(
                {
                    "tool_calls": [
                        tool_call(
                            "write_file", path="data/new.json", content='{"ok":true}'
                        )
                    ]
                }
            ),
            response({"tool_calls": [tool_call("check_file", path="data/new.json")]}),
            response({"content": "검사 결과를 확인했습니다."}),
        )
        run = Run(
            "수정해줘",
            "required-repair",
            features={"writes": True},
            requirements=[
                {"tool": "write_file", "arguments": {"path": "data/new.json"}},
                {"tool": "check_file", "arguments": {"path": "data/new.json"}},
            ],
        )
        Harness(self.workspace, self.root / "state", runtime=runtime).execute(run)
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.tool_calls, 2)
        self.assertTrue(any(e["kind"] == "context.repair" for e in run.events))
        check = [e for e in run.events if e["kind"] == "validation.requirements"][-1]
        self.assertTrue(check["output"]["passed"])
        for proof in check["output"]["evidence"]:
            self.assertEqual(
                run.events[proof["tool_result_event"] - 1]["kind"], "tool.result"
            )

    def test_valid_json_with_wrong_value_does_not_pass_result_requirement(self):
        runtime = FixtureRuntime(
            response(
                {
                    "tool_calls": [
                        tool_call(
                            "write_file",
                            path="data/value.json",
                            content='{"practice":false}',
                        )
                    ]
                }
            ),
            response({"tool_calls": [tool_call("check_file", path="data/value.json")]}),
            response({"content": "완료했습니다."}),
            response(
                {
                    "tool_calls": [
                        tool_call(
                            "write_file",
                            path="data/value.json",
                            content='{"practice":true}',
                        )
                    ]
                }
            ),
            response({"tool_calls": [tool_call("check_file", path="data/value.json")]}),
            response({"content": "값을 확인했습니다."}),
        )
        run = Run(
            "practice=true로 수정",
            "value-check",
            features={"writes": True},
            requirements=[
                {
                    "tool": "check_file",
                    "arguments": {"path": "data/value.json"},
                    "result": {"json_value": {"practice": True}},
                }
            ],
        )
        Harness(self.workspace, self.root / "state", runtime=runtime).execute(run)
        self.assertEqual(run.status, "completed")
        self.assertEqual(
            [
                e["output"]["passed"]
                for e in run.events
                if e["kind"] == "validation.requirements"
            ],
            [False, True],
        )
        self.assertTrue(
            json.loads((self.workspace / "data/value.json").read_text())["practice"]
        )
        from local_agent_lab.policy import requirement_gate

        stale = [
            {
                "name": "check_file",
                "arguments": {"path": "data/value.json"},
                "event_id": 1,
                "result": {"json_value": {"practice": True}},
            },
            {
                "name": "check_file",
                "arguments": {"path": "data/value.json"},
                "event_id": 2,
                "result": {"json_value": {"practice": False}},
            },
        ]
        self.assertTrue(requirement_gate(run, stale))

    def test_actual_tool_round_trip_and_session_restore_with_fixture_model(self):
        runtime = FixtureRuntime(
            response(
                {"tool_calls": [tool_call("read_file", path="notes/agent-guide.md")]}
            ),
            response({"content": "요약 결과"}),
        )
        harness = Harness(self.workspace, self.root / "state", runtime=runtime)
        harness.execute(self.run)
        self.assertEqual(self.run.status, "completed")
        self.assertEqual(self.run.model_calls, 2)
        self.assertEqual(self.run.tool_calls, 1)
        calls = [
            payload
            for route, payload in runtime.requests
            if route.endswith("completions")
        ]
        self.assertEqual(calls[1]["messages"][-1]["role"], "tool")
        tool_result = json.loads(calls[1]["messages"][-1]["content"])
        self.assertEqual(tool_result["path"], "notes/agent-guide.md")
        self.assertEqual(
            tool_result["content"],
            (self.workspace / "notes/agent-guide.md").read_bytes().decode("utf-8-sig"),
        )
        restored = harness.load_history(Run("다음 질문", "test-session"))
        self.assertEqual(restored["excerpt"], "요약 결과")
        self.assertIsNotNone(self.run.finished)

    def test_changed_file_cannot_complete_without_a_passing_check(self):
        runtime = FixtureRuntime(
            response(
                {
                    "tool_calls": [
                        tool_call(
                            "write_file", path="data/new.json", content='{"ok":true}'
                        )
                    ]
                }
            ),
            response({"content": "검사 없이 완료"}),
            response({"tool_calls": [tool_call("check_file", path="data/new.json")]}),
            response({"content": "검사 후 완료"}),
        )
        Harness(self.workspace, self.root / "state", runtime=runtime).execute(self.run)
        self.assertEqual(self.run.status, "completed")
        self.assertEqual(self.run.answer, "검사 후 완료")
        checks = [
            event["output"]["passed"]
            for event in self.run.events
            if event["kind"] == "validation.final"
        ]
        self.assertEqual(checks, [False, True])

    def test_model_failure_is_visible_and_does_not_fabricate_an_answer(self):
        Harness(
            self.workspace, self.root / "state", runtime=FixtureRuntime({"choices": []})
        ).execute(self.run)
        self.assertEqual(self.run.status, "failed")
        self.assertEqual(self.run.answer, "")
        self.assertTrue(any(event["kind"] == "run.error" for event in self.run.events))

    def test_step_mode_pauses_at_a_real_event_then_continues(self):
        run = Run("step", "step-session", step=True)
        finished = threading.Event()

        def worker():
            run.emit("one", "one", "one")
            run.emit("two", "two", "two")
            finished.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        with run.condition:
            self.assertTrue(
                run.condition.wait_for(lambda: len(run.events) == 1, timeout=2)
            )
        self.assertEqual(run.status, "paused")
        self.assertFalse(finished.is_set())
        run.control("continue")
        thread.join(timeout=2)
        self.assertTrue(finished.is_set())
        self.assertEqual(len(run.events), 2)


if __name__ == "__main__":
    unittest.main()
