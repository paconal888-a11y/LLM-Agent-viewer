import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from local_agent_lab.features import BASELINE_FEATURES, normalize_features, run_metrics
from local_agent_lab.harness import (
    Harness,
    build_context,
    expand_command,
    execute_tool,
    tools_for,
)
from local_agent_lab.experiments import CASES, evaluate
from local_agent_lab.trace import ROOT, Run
from local_agent_lab.web import Application
from test_harness import FixtureRuntime, response, tool_call


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        shutil.copytree(ROOT / "demo_workspace", self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def test_disabled_features_change_actual_context_and_tools(self):
        run = Run("hello", "no-features", features=BASELINE_FEATURES)
        messages, catalog = build_context(
            run,
            self.workspace,
            {
                "excerpt": "DO NOT INCLUDE",
                "turns": [[{"role": "user", "content": "OLD"}]],
            },
        )
        self.assertEqual(catalog, [])
        self.assertEqual(tools_for(run), [])
        serialized = json.dumps(messages)
        self.assertNotIn("project_rules", serialized)
        self.assertNotIn("DO NOT INCLUDE", serialized)
        self.assertNotIn("OLD", serialized)
        self.assertFalse(any(event["kind"] == "file.open" for event in run.events))
        result = execute_tool(
            run, self.workspace, [], tool_call("read_file", path="AGENTS.md"), set()
        )
        self.assertFalse(json.loads(result["content"])["executed_successfully"])

    def test_tool_disable_also_disables_skills_and_rejects_unknown_flags(self):
        self.assertFalse(normalize_features({"tools": False})["skills"])
        for invalid in ({"unknown_feature": True}, {"rules": "yes"}, [True]):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    normalize_features(invalid)

    def test_read_only_default_rejects_writes_even_if_model_requests_them(self):
        run = Run("문서 요약", "read-only")
        self.assertNotIn(
            "write_file", [tool["function"]["name"] for tool in tools_for(run)]
        )
        target = self.workspace / "notes/agent-guide.md"
        before = target.read_bytes()
        result = execute_tool(
            run,
            self.workspace,
            [],
            tool_call("write_file", path="notes/agent-guide.md", content="changed"),
            set(),
        )
        self.assertFalse(json.loads(result["content"])["executed_successfully"])
        self.assertEqual(target.read_bytes(), before)

    def test_command_expansion_and_raw_input_are_both_preserved(self):
        run = Run("/summary notes/agent-guide.md", "command")
        expand_command(run, self.workspace)
        messages, _ = build_context(run, self.workspace, {})
        self.assertEqual(run.prompt, "/summary notes/agent-guide.md")
        self.assertNotIn("$ARGUMENTS", run.effective_prompt)
        self.assertIn("notes/agent-guide.md", run.effective_prompt)
        self.assertEqual(messages[-1]["content"], run.effective_prompt)
        self.assertTrue(any(event["kind"] == "command.expand" for event in run.events))

    def test_disabled_command_does_not_read_or_expand_template(self):
        run = Run("/summary file.md", "command-off", features={"commands": False})
        expand_command(run, self.workspace)
        self.assertEqual(run.effective_prompt, run.prompt)
        self.assertFalse(any(event["kind"] == "file.open" for event in run.events))

    def test_context_segments_exactly_reconstruct_system_message(self):
        run = Run("hello", "segments")
        messages, _ = build_context(run, self.workspace, {})
        pieces = [
            piece for piece in run.context_segments if piece["message_index"] == 0
        ]
        self.assertEqual(
            "".join(piece["text"] for piece in pieces), messages[0]["content"]
        )
        for piece in pieces:
            self.assertEqual(
                messages[0]["content"][piece["start"] : piece["end"]], piece["text"]
            )

    def test_quality_criteria_are_not_applied_to_custom_prompts(self):
        snapshot = Run("custom", "evaluation").snapshot()
        self.assertFalse(evaluate(snapshot, "grounding")["available"])
        snapshot["prompt"] = CASES["grounding"]["prompt"]
        snapshot["answer"] = "NARI-042, 2026-12-12 (notes/release-brief.md)"
        snapshot["events"] = [
            {
                "kind": "tool.result",
                "output": {"path": "notes/release-brief.md", "content": "actual file"},
            }
        ]
        self.assertEqual(evaluate(snapshot, "grounding")["passed"], 3)

    def test_two_real_harness_configurations_are_executed_serially(self):
        runtime = FixtureRuntime(
            response({"content": "파일을 읽을 도구가 없습니다."}),
            response(
                {"tool_calls": [tool_call("read_file", path="notes/release-brief.md")]}
            ),
            response({"content": "NARI-042 / 2026-12-12 / notes/release-brief.md"}),
        )
        app = Application(Harness(self.workspace, self.root / "state", runtime=runtime))
        experiment_id = app.experiments.start(
            CASES["grounding"]["prompt"], case_id="grounding"
        )
        # Conditions are signalled by the harness; no fixed sleep is required.
        done = threading.Event()

        def wait_finished():
            while app.experiment_active:
                with app.lock:
                    runs = list(app.runs.values())
                if runs:
                    with runs[-1].condition:
                        runs[-1].condition.wait(timeout=0.02)
            done.set()

        waiter = threading.Thread(target=wait_finished, daemon=True)
        waiter.start()
        self.assertTrue(done.wait(timeout=8))
        item = app.experiments.get(experiment_id)
        self.assertEqual(item["status"], "completed")
        self.assertEqual(item["results"]["A"]["metrics"]["tool_calls"], 0)
        self.assertEqual(item["results"]["B"]["metrics"]["tool_calls"], 1)
        self.assertEqual(item["results"]["B"]["evaluation"]["passed"], 3)
        self.assertTrue(all(item["controls"].values()))
        requests = [
            payload
            for route, payload in runtime.requests
            if route.endswith("completions")
        ]
        self.assertEqual(requests[0]["tools"], [])
        self.assertGreater(len(requests[1]["tools"]), 0)
        self.assertNotEqual(
            item["results"]["A"]["run_id"], item["results"]["B"]["run_id"]
        )


if __name__ == "__main__":
    unittest.main()
