import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from local_agent_lab.harness import Harness, execute_tool, tools_for
from local_agent_lab.orchestration import prepare, inference_turn
from local_agent_lab.trace import ROOT, Run, RunStopped
from test_harness import FixtureRuntime, response, tool_call


class RoutedRuntime(FixtureRuntime):
    def request(self, route, payload=None):
        if route != "/v1/chat/completions":
            return super().request(route, payload)
        self.requests.append((route, json.loads(json.dumps(payload))))
        child = "You are a read-only subagent" in payload["messages"][0]["content"]
        results = [m for m in payload["messages"] if m["role"] == "tool"]
        if results:
            return response(
                {
                    "content": "자료에 근거한 하위 보고"
                    if child
                    else "두 하위 보고를 합쳤습니다."
                }
            )
        if child:
            return response(
                {"tool_calls": [tool_call("read_file", path="notes/agent-guide.md")]}
            )
        tasks = [
            {"profile": role, "task": "notes/agent-guide.md를 읽고 근거를 보고하세요."}
            for role in ("explore", "oracle")
        ]
        return response(
            {
                "tool_calls": [
                    tool_call(
                        "delegate_tasks",
                        tasks_json=json.dumps(tasks, ensure_ascii=False),
                    )
                ]
            }
        )


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        shutil.copytree(ROOT / "demo_workspace", self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def harness(self, runtime):
        return Harness(self.workspace, self.root / "state", runtime=runtime)

    def test_deep_mode_makes_actual_additional_review_request(self):
        runtime = FixtureRuntime(
            response({"content": "초안"}), response({"content": "검토 후 최종 답변"})
        )
        run = Run("설명해줘", "deep", mode="deep")
        self.harness(runtime).execute(run)
        self.assertEqual(run.status, "completed", run.error)
        self.assertEqual(run.model_calls, 2)
        self.assertEqual(run.max_model_calls, 16)
        review = next(e for e in run.events if e["kind"] == "agent.review")
        self.assertEqual(review["input"]["candidate_answer"], "초안")
        actual = [p for route, p in runtime.requests if route == "/v1/chat/completions"]
        self.assertEqual(
            actual[1]["messages"][-1], review["output"]["appended_message"]
        )
        self.assertEqual(run.answer, "검토 후 최종 답변")

    def test_team_uses_independent_contexts_then_actual_tool_result_join(self):
        runtime = RoutedRuntime()
        harness = self.harness(runtime)
        history = harness.history_path("main")
        history.parent.mkdir(parents=True)
        history.write_text(
            json.dumps(
                {"turns": [[{"role": "user", "content": "PRIVATE_PARENT_HISTORY"}]]}
            ),
            encoding="utf-8",
        )
        run = Run("문서를 나눠 확인해줘", "main", mode="team")
        harness.execute(run)
        self.assertEqual(run.status, "completed", run.error)
        self.assertEqual(len(run.children), 2)
        self.assertEqual(run.model_calls, 2)
        self.assertEqual(run.snapshot()["totals"]["model_calls"], 6)
        self.assertEqual(run.snapshot()["totals"]["tool_calls"], 3)
        for child in run.children:
            self.assertEqual(child.status, "completed", child.error)
            self.assertEqual(child.tool_calls, 1)
            self.assertFalse(child.features["memory"])
            self.assertFalse(child.features["writes"])
            self.assertNotIn(
                "write_file", [t["function"]["name"] for t in tools_for(child)]
            )
            inputs = [e["input"] for e in child.events if e["kind"] == "model.request"]
            self.assertNotIn("PRIVATE_PARENT_HISTORY", json.dumps(inputs))
            if "load_skill" not in child.allowed_tool_names:
                self.assertNotIn(
                    "available_skills", inputs[0]["messages"][0]["content"]
                )
            self.assertTrue(
                (harness.state_dir / "traces" / (child.id + ".json")).is_file()
            )
            mirrored = [e for e in run.events if e["agent_id"] == child.id]
            self.assertEqual(len(mirrored), len(child.events))
            self.assertEqual(mirrored[0]["child_event_id"], 1)
        root_requests = [
            e
            for e in run.events
            if e["kind"] == "model.request" and e["agent_id"] == run.id
        ]
        returned = json.loads(root_requests[-1]["input"]["messages"][-1]["content"])
        self.assertEqual(returned["protocol"], "subagents")
        self.assertTrue(returned["all_completed"])
        self.assertEqual(len(returned["results"]), 2)
        json.dumps(run.snapshot(compact=True), allow_nan=False)

    def test_plan_mode_drops_write_permission_even_if_requested(self):
        run = Run("계획만", "plan", mode="plan", features={"writes": True})
        harness = self.harness(FixtureRuntime())
        prepare(run, harness)
        before = (self.workspace / "data/settings.json").read_bytes()
        result = execute_tool(
            run,
            self.workspace,
            [],
            tool_call("write_file", path="data/settings.json", content="{}"),
            set(),
        )
        self.assertFalse(json.loads(result["content"])["executed_successfully"])
        self.assertEqual((self.workspace / "data/settings.json").read_bytes(), before)

    def test_invalid_delegation_does_not_start_children(self):
        run = Run("분담", "invalid", mode="team")
        harness = self.harness(FixtureRuntime())
        prepare(run, harness)
        for value in ('[{"profile":"unknown","task":"x"}]', "[]", "{}"):
            result = execute_tool(
                run,
                self.workspace,
                [],
                tool_call("delegate_tasks", tasks_json=value),
                set(),
            )
            self.assertFalse(json.loads(result["content"])["executed_successfully"])
        self.assertEqual(run.children, [])

    def test_stop_propagates_to_child_waiting_for_shared_model(self):
        harness = self.harness(FixtureRuntime())
        parent = Run("main", "cancel")
        child = Run("child", "cancel-child", parent_run=parent)
        parent.children.append(child)
        harness.inference_lock.acquire()
        entered = threading.Event()
        stopped = threading.Event()

        def worker():
            entered.set()
            try:
                with inference_turn(harness, child, 1):
                    self.fail("중지한 하위 실행은 모델 계산을 시작하면 안 됩니다.")
            except RunStopped:
                stopped.set()

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(entered.wait(1))
        parent.control("stop")
        self.assertTrue(stopped.wait(2))
        thread.join(2)
        self.assertFalse(thread.is_alive())
        harness.inference_lock.release()

    def test_unknown_or_recursive_mode_is_rejected(self):
        for value in ("recursive", [], None):
            with self.assertRaises(ValueError):
                Run("x", "x", mode=value)

    def test_feedback_matches_only_the_same_agent_and_is_not_inherited_as_root_memory(
        self,
    ):
        from local_agent_lab.feedback import collect_feedback, report

        parent = Run("main", "feedback-main")
        first = Run("one", "feedback-one", parent_run=parent, agent_name="one")
        other = Run("two", "feedback-two", parent_run=parent, agent_name="two")
        correction = {"role": "user", "content": "실제 도구를 호출하세요."}
        first.emit(
            "context.repair",
            "지적",
            "",
            input={"rejected_answer": "완료"},
            output={"appended_message": correction},
        )
        other.emit("model.request", "다른 대화", "", input={"messages": [correction]})
        first.emit("model.request", "해당 대화", "", input={"messages": [correction]})
        result = report(parent.snapshot())["repairs"][0]
        self.assertEqual(result["next_request_event"], 3)
        self.assertEqual(result["agent_name"], "one")
        self.assertTrue(result["feedback_in_next_request"])
        self.assertEqual(collect_feedback(parent), [])
