"""중단된 실행 이어가기. 모델 응답은 픽스처이고 하네스·도구·저장 파일은 실제로 사용한다."""

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_agent_lab.checkpoint import checkpoint_path, resume_summary
from local_agent_lab.harness import Harness
from local_agent_lab.trace import ROOT, Run, RunStopped
from local_agent_lab.web import Application
from test_harness import FixtureRuntime, response, tool_call


WRITES = {"rules": True, "memory": True, "skills": True, "tools": True, "commands": True, "writes": True}


class StopAt(FixtureRuntime):
    """N번째 답변 호출을 받으면 실행을 중지한다."""

    def __init__(self, stop_on, *responses):
        super().__init__(*responses)
        self.stop_on = stop_on
        self.calls = 0
        self.run = None

    def request(self, route, payload=None):
        if route.endswith("completions"):
            self.calls += 1
            if self.calls == self.stop_on:
                self.run.control("stop")
                raise RunStopped()
        return super().request(route, payload)


def add(a=1, b=2, call_id="call"):
    call = tool_call("add_numbers", a=a, b=b)
    call["id"] = call_id
    return call


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"
        shutil.copytree(ROOT / "demo_workspace", self.workspace)
        self.state = self.root / "state"

    def tearDown(self):
        self.temp.cleanup()

    def app(self, runtime):
        return Application(Harness(self.workspace, self.state, runtime=runtime))

    def wait(self, run):
        # 저장까지 끝나야 다음 단계가 파일을 읽거나 임시 폴더를 지울 수 있다.
        trace = self.state / "traces" / (run.id + ".json")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if run.status in {"completed", "failed", "stopped"}:
                try:
                    if json.loads(trace.read_text(encoding="utf-8"))["status"] == run.status:
                        return
                except (OSError, ValueError):
                    pass
            time.sleep(0.05)
        self.fail("실행이 제한 시간 안에 끝나지 않았습니다.")

    def start(self, app, prompt="요청", **kwargs):
        run = app.start(prompt, "resume-session", False, launch=False, **kwargs)
        app.runtime_run = run
        if isinstance(app.harness.runtime, StopAt):
            app.harness.runtime.run = run
        app.launch(run)
        self.wait(run)
        return run

    def stopped_after_one_tool(self):
        runtime = StopAt(2, response({"tool_calls": [add()]}))
        app = self.app(runtime)
        return app, runtime, self.start(app)

    def completions(self, runtime):
        return [p for route, p in runtime.requests if route.endswith("completions")]

    def test_stopped_run_keeps_a_checkpoint_and_reports_it(self):
        app, _, run = self.stopped_after_one_tool()
        self.assertEqual(run.status, "stopped")
        self.assertTrue(checkpoint_path(self.state, run.id).is_file())
        info = app.get_run(run.id)["resume"]
        self.assertEqual(info["next_call"], 2)
        self.assertEqual(info["budget_left"], 7)

    def test_resume_continues_from_saved_messages_without_rereading_rules(self):
        app, runtime, run = self.stopped_after_one_tool()
        runtime.responses = iter([response({"content": "끝"})])
        runtime.stop_on = 0
        before = len(self.completions(runtime))
        resumed = app.resume(run.id)
        self.wait(resumed)
        self.assertEqual(resumed.status, "completed", resumed.error)
        self.assertEqual(resumed.answer, "끝")
        self.assertEqual(resumed.resumed_from, run.id)
        sent = self.completions(runtime)[before]["messages"]
        self.assertEqual(sent[-1]["role"], "tool")
        self.assertIn('"result": 3', sent[-1]["content"])
        self.assertEqual(resumed.tool_calls, 1)
        kinds = [e["kind"] for e in resumed.events]
        self.assertIn("run.resume", kinds)
        self.assertNotIn("rules.wrap", kinds)
        self.assertNotIn("context.assemble", kinds)

    def test_finished_resume_removes_both_checkpoints(self):
        app, runtime, run = self.stopped_after_one_tool()
        runtime.responses = iter([response({"content": "끝"})])
        runtime.stop_on = 0
        resumed = app.resume(run.id)
        self.wait(resumed)
        self.assertFalse(checkpoint_path(self.state, run.id).exists())
        self.assertFalse(checkpoint_path(self.state, resumed.id).exists())
        self.assertIsNone(app.get_run(run.id)["resume"])

    def test_completed_run_has_nothing_to_resume(self):
        app = self.app(FixtureRuntime(response({"content": "끝"})))
        run = self.start(app)
        self.assertEqual(run.status, "completed")
        self.assertFalse(checkpoint_path(self.state, run.id).exists())
        with self.assertRaises(ValueError):
            app.resume(run.id)

    def test_tool_call_without_a_result_is_reported_not_repeated(self):
        first, second = add(1, 2, "one"), add(3, 4, "two")
        runtime = FixtureRuntime(response({"tool_calls": [first, second]}))
        app = self.app(runtime)
        calls = []
        original = app.harness.dispatch_tool

        def interrupted(run, catalog, call, skills):
            calls.append(call["id"])
            if call["id"] == "two":
                run.control("stop")
                raise RunStopped()
            return original(run, catalog, call, skills)

        app.harness.dispatch_tool = interrupted
        run = self.start(app)
        self.assertEqual(run.status, "stopped")
        app.harness.dispatch_tool = original
        runtime.responses = iter([response({"content": "끝"})])
        resumed = app.resume(run.id)
        self.wait(resumed)
        self.assertEqual(resumed.status, "completed", resumed.error)
        sent = self.completions(runtime)[-1]["messages"]
        tools = [m for m in sent if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tools], ["one", "two"])
        self.assertIn('"result": 3', tools[0]["content"])
        self.assertIn('"side_effects": "unknown"', tools[1]["content"])
        event = next(e for e in resumed.events if e["kind"] == "run.resume")
        self.assertEqual(event["output"]["unanswered_tool_calls"], ["add_numbers"])
        self.assertEqual(event["status"], "warning")
        self.assertEqual(calls, ["one", "two"])

    def test_counters_and_budget_continue_across_the_resume(self):
        app, runtime, run = self.stopped_after_one_tool()
        runtime.responses = iter([response({"content": "끝"})])
        runtime.stop_on = 0
        resumed = app.resume(run.id)
        self.wait(resumed)
        self.assertEqual(resumed.model_calls, 2)
        self.assertEqual(resumed.max_model_calls, 8)

    def test_exhausted_budget_needs_extra_calls(self):
        (self.workspace / "agent-lens.json").write_text(
            json.dumps({"loop": {"max_model_calls": 1}}), encoding="utf-8"
        )
        runtime = FixtureRuntime(response({"tool_calls": [add()]}))
        app = self.app(runtime)
        run = self.start(app)
        self.assertEqual(run.status, "failed")
        with self.assertRaisesRegex(ValueError, "예산"):
            app.resume(run.id)
        for value in (-1, 9, "1", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.resume(run.id, value)
        runtime.responses = iter([response({"content": "끝"})])
        resumed = app.resume(run.id, 2)
        self.wait(resumed)
        self.assertEqual(resumed.status, "completed", resumed.error)
        self.assertEqual(resumed.max_model_calls, 3)

    def test_changed_workspace_files_are_reported_to_the_model(self):
        app, runtime, run = self.stopped_after_one_tool()
        (self.workspace / "notes" / "agent-guide.md").write_text("바뀐 내용", encoding="utf-8")
        runtime.responses = iter([response({"content": "끝"})])
        runtime.stop_on = 0
        resumed = app.resume(run.id)
        self.wait(resumed)
        sent = self.completions(runtime)[-1]["messages"]
        self.assertIn("notes/agent-guide.md", sent[-1]["content"])
        self.assertEqual(sent[-1]["role"], "user")
        event = next(e for e in resumed.events if e["kind"] == "run.resume")
        self.assertEqual(event["output"]["workspace_changed_files"], ["notes/agent-guide.md"])

    def test_state_survives_a_server_restart(self):
        _, _, run = self.stopped_after_one_tool()
        trace = self.state / "traces" / (run.id + ".json")
        data = json.loads(trace.read_text(encoding="utf-8"))
        data["status"] = "running"
        trace.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        runtime = FixtureRuntime(response({"content": "끝"}))
        restarted = self.app(runtime)
        self.assertEqual(restarted.get_run(run.id)["status"], "interrupted")
        self.assertIsNotNone(restarted.get_run(run.id)["resume"])
        listed = next(item for item in restarted.history() if item["id"] == run.id)
        self.assertEqual(listed["resume"]["next_call"], 2)
        resumed = restarted.resume(run.id)
        self.wait(resumed)
        self.assertEqual(resumed.status, "completed", resumed.error)

    def test_conversation_history_is_saved_after_a_resumed_run(self):
        app, runtime, run = self.stopped_after_one_tool()
        runtime.responses = iter([response({"content": "끝"})])
        runtime.stop_on = 0
        resumed = app.resume(run.id)
        self.wait(resumed)
        state = json.loads((self.state / "sessions" / "resume-session.json").read_text(encoding="utf-8"))
        self.assertEqual(state["turns"][-1][0]["content"], "요청")
        self.assertEqual(state["turns"][-1][-1]["content"], "끝")

    def stop_at(self, kind):
        original = Run.emit

        def emit(run, event_kind, *args, **kwargs):
            if event_kind == kind and not run.resumed_from:
                run.control("stop")
                raise RunStopped()
            return original(run, event_kind, *args, **kwargs)

        return patch.object(Run, "emit", emit)

    def writing_run(self, stop_kind):
        call = tool_call("write_file", path="data/new.json", content="{}")
        runtime = FixtureRuntime(response({"tool_calls": [call]}))
        app = self.app(runtime)
        with self.stop_at(stop_kind):
            run = self.start(app, features=WRITES)
        self.assertEqual(run.status, "stopped")
        return app, runtime, run

    def assert_final_gate_still_applies(self, app, runtime, run):
        check = tool_call("check_file", path="data/new.json")
        runtime.responses = iter(
            [response({"content": "끝"}), response({"tool_calls": [check]}), response({"content": "검사 후 끝"})]
        )
        resumed = app.resume(run.id)
        self.wait(resumed)
        self.assertEqual(resumed.status, "completed", resumed.error)
        gates = [e for e in resumed.events if e["kind"] == "validation.final"]
        self.assertFalse(gates[0]["output"]["passed"])
        self.assertEqual(gates[0]["output"]["unchecked_files"], ["data/new.json"])
        self.assertEqual(resumed.answer, "검사 후 끝")

    def test_write_recorded_before_stopping_still_needs_a_check(self):
        self.assert_final_gate_still_applies(*self.writing_run("context.append"))

    def test_write_with_unknown_result_still_needs_a_check(self):
        app, runtime, run = self.writing_run("tool.result")
        self.assert_final_gate_still_applies(app, runtime, run)

    def test_own_writes_are_not_reported_as_outside_changes(self):
        app, runtime, run = self.writing_run("context.append")
        runtime.responses = iter([response({"tool_calls": [tool_call("check_file", path="data/new.json")]}), response({"content": "끝"})])
        resumed = app.resume(run.id)
        self.wait(resumed)
        event = next(e for e in resumed.events if e["kind"] == "run.resume")
        self.assertEqual(event["output"]["workspace_changed_files"], [])

    def test_resume_records_the_runtime_that_actually_runs(self):
        app, runtime, run = self.stopped_after_one_tool()
        runtime.responses = iter([response({"content": "끝"})])
        runtime.stop_on = 0
        app.harness.model = "다른-모델"
        resumed = app.resume(run.id)
        self.wait(resumed)
        self.assertEqual(resumed.conditions["model"], "다른-모델")
        event = next(e for e in resumed.events if e["kind"] == "run.resume")
        self.assertEqual(event["status"], "warning")
        self.assertEqual(event["output"]["runtime_changed"]["now"]["model"], "다른-모델")

    def test_restored_provenance_does_not_point_at_events_of_this_trace(self):
        app, runtime, run = self.stopped_after_one_tool()
        runtime.responses = iter([response({"content": "끝"})])
        runtime.stop_on = 0
        resumed = app.resume(run.id)
        self.wait(resumed)
        restored = [p for p in resumed.context_segments if p.get("restored_from") == run.id]
        self.assertTrue(restored)
        self.assertTrue(all(p["source_event_id"] is None for p in restored))

    def test_finished_history_is_not_saved_twice_after_a_late_stop(self):
        runtime = FixtureRuntime(response({"content": "끝"}))
        app = self.app(runtime)
        with self.stop_at("run.complete"):
            run = self.start(app)
        self.assertEqual(run.status, "stopped")
        self.assertIsNone(app.get_run(run.id)["resume"])

    def test_malformed_tool_calls_never_reach_the_checkpoint(self):
        runtime = FixtureRuntime(response({"tool_calls": ["잘못된 값"]}))
        app = self.app(runtime)
        run = self.start(app)
        self.assertEqual(run.status, "failed")
        self.assertIn("형식", run.error)
        saved = json.loads(checkpoint_path(self.state, run.id).read_text(encoding="utf-8"))
        self.assertFalse(any(m["role"] == "assistant" for m in saved["messages"]))

    def test_samples_only_mode_cannot_resume(self):
        app = Application(Harness(self.workspace, self.state, runtime=FixtureRuntime()), samples_only=True)
        with self.assertRaises(ValueError):
            app.resume("0123456789abcdef")

    def test_invalid_ids_and_summaries(self):
        with self.assertRaises(ValueError):
            checkpoint_path(self.state, "../../secret")
        self.assertIsNone(resume_summary(self.state, "0123456789abcdef", "stopped"))
        self.assertIsNone(resume_summary(self.state, "0123456789abcdef", "completed"))

    def test_child_runs_do_not_write_checkpoints(self):
        call = tool_call(
            "delegate_tasks",
            tasks_json=json.dumps([{"profile": "explore", "task": "x"}]),
        )
        runtime = FixtureRuntime(
            response({"tool_calls": [call]}),
            response({"content": "하위 답변"}),
            response({"content": "끝"}),
        )
        app = self.app(runtime)
        run = self.start(app, mode="team")
        self.assertEqual(run.status, "completed", run.error)
        folder = self.state / "checkpoints"
        self.assertEqual(list(folder.glob("*.json")) if folder.exists() else [], [])


if __name__ == "__main__":
    unittest.main()
