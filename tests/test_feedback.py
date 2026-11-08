import json
import shutil
import tempfile
import unittest
from pathlib import Path
from local_agent_lab.feedback import recall_and_audit, report
from local_agent_lab.harness import Harness, build_context
from local_agent_lab.trace import ROOT, Run
from test_harness import FixtureRuntime, response, tool_call


class FeedbackTests(unittest.TestCase):
    def test_feedback_moves_out_of_recent_turns_but_can_be_explicitly_recalled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            shutil.copytree(ROOT / "demo_workspace", workspace)
            runtime = FixtureRuntime(
                response({"content": "읽었습니다."}),
                response(
                    {
                        "tool_calls": [
                            tool_call("read_file", path="notes/agent-guide.md")
                        ]
                    }
                ),
                response({"content": "실제 내용을 확인했습니다."}),
                response({"content": "첫 후속 답변"}),
                response({"content": "두 번째 후속 답변"}),
            )
            harness = Harness(workspace, root / "state", runtime=runtime)
            first = Run(
                "문서를 확인해줘",
                "same-session",
                requirements=[
                    {"tool": "read_file", "arguments": {"path": "notes/agent-guide.md"}}
                ],
            )
            harness.execute(first)
            self.assertEqual(first.status, "completed")
            feedback = report(first.snapshot())
            self.assertTrue(feedback["repairs"][0]["feedback_in_next_request"])
            self.assertFalse(feedback["weights_updated"])
            for prompt in ["다른 질문 1", "다른 질문 2"]:
                harness.execute(Run(prompt, "same-session"))
            current = Run(first.prompt, "same-session")
            history = harness.load_history(current)
            self.assertNotIn(first.id, history["turn_ids"])
            self.assertTrue(history["feedback_ledger"])
            messages, _ = build_context(current, workspace, history)
            recall_and_audit(current, history, messages)
            audit = current.events[-1]["output"]
            self.assertEqual(audit["included_feedback"], 0)
            recalled = Run(
                first.prompt, "same-session", features={"feedback_recall": True}
            )
            messages, _ = build_context(recalled, workspace, history)
            recall_and_audit(recalled, history, messages)
            self.assertEqual(recalled.events[-1]["output"]["included_feedback"], 1)
            self.assertTrue(
                any(e["kind"] == "memory.feedback.recall" for e in recalled.events)
            )

    def test_memory_off_does_not_put_stored_feedback_into_model_messages(self):
        run = Run("request", "off", features={"memory": False, "feedback_recall": True})
        self.assertFalse(run.features["feedback_recall"])
        messages = [{"role": "user", "content": "request"}]
        recall_and_audit(run, {}, messages)
        self.assertEqual(len(messages), 1)
        self.assertIsNotNone(run.events[-1]["output"]["unavailable_reason"])
