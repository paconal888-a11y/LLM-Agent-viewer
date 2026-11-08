import json
import shutil
import tempfile
import unittest
from pathlib import Path
from local_agent_lab.harness import Harness
from local_agent_lab.trace import ROOT, Run
from local_agent_lab.context_memory import archive_path, inspect
from test_harness import FixtureRuntime, response


class ContextMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        shutil.copytree(ROOT / "demo_workspace", self.workspace)
        self.records = [
            {
                "run_id": "old" + str(i),
                "messages": [
                    {
                        "role": "user",
                        "content": "ORIGINAL-" + str(i) + " " + "source data " * 20,
                    },
                    {"role": "assistant", "content": "answer " + str(i)},
                ],
            }
            for i in range(3)
        ]

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, runtime, trigger=100):
        harness = Harness(self.workspace, self.root / "state", runtime=runtime)
        path = harness.history_path("chat")
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "turns": [r["messages"] for r in self.records[-2:]],
                    "turn_ids": ["old1", "old2"],
                    "archive_turn_count": 3,
                }
            ),
            encoding="utf-8",
        )
        archive = archive_path(harness, "chat")
        archive.parent.mkdir(parents=True)
        archive.write_text(
            "".join(json.dumps(r) + "\n" for r in self.records), encoding="utf-8"
        )
        config = json.loads(
            (self.workspace / "memory.json").read_text(encoding="utf-8")
        )
        config.update(trigger_characters=trigger, chunk_characters=6000)
        (self.workspace / "memory.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        return harness

    def run_chat(self, harness):
        run = Run(
            "continue",
            "chat",
            features={
                "compaction": True,
                "tools": False,
                "rules": False,
                "commands": False,
            },
        )
        harness.execute(run)
        return run

    def test_actual_summary_request_preserves_archive_and_changes_next_messages(self):
        runtime = FixtureRuntime(
            response({"content": "보존할 작업 메모"}),
            response({"content": "최종 답변"}),
        )
        harness = self.seed(runtime)
        original = archive_path(harness, "chat").read_bytes()
        run = self.run_chat(harness)
        self.assertEqual(run.status, "completed", run.error)
        self.assertEqual(run.model_calls, 2)
        self.assertEqual(run.usage, {"prompt_tokens": 20, "completion_tokens": 10})
        cache = next(e for e in run.events if e["kind"] == "runtime.cache")
        self.assertFalse(cache["output"]["cache_prompt"])
        calls = [e for e in run.events if e["kind"] == "model.request"]
        self.assertEqual(calls[0]["model_purpose"], "compaction")
        self.assertIn("ORIGINAL-0", json.dumps(calls[0]["input"]))
        self.assertNotIn("ORIGINAL-0", json.dumps(calls[1]["input"]))
        self.assertIn("ORIGINAL-2", json.dumps(calls[1]["input"]))
        self.assertIn(
            "보존할 작업 메모", json.dumps(calls[1]["input"], ensure_ascii=False)
        )
        self.assertTrue(archive_path(harness, "chat").read_bytes().startswith(original))
        state = inspect(harness, "chat")
        self.assertEqual(len(state["records"]), 4)
        self.assertEqual(state["summary"]["through_turn"], 2)
        self.assertTrue(Path(state["summary"]["path"]).is_file())
        self.assertFalse(state["weights_updated"])

    def test_below_threshold_uses_unsummarized_original_turns_instead_of_dropping_them(
        self,
    ):
        harness = self.seed(
            FixtureRuntime(response({"content": "done"})), trigger=50000
        )
        run = self.run_chat(harness)
        self.assertEqual(run.status, "completed", run.error)
        self.assertEqual(run.model_calls, 1)
        request = next(e["input"] for e in run.events if e["kind"] == "model.request")
        for i in range(3):
            self.assertIn("ORIGINAL-" + str(i), json.dumps(request))
        self.assertFalse(
            any(e["kind"] == "memory.compaction.start" for e in run.events)
        )

    def test_truncated_summary_is_not_committed_and_original_is_unchanged(self):
        cut = response({"content": "cut"})
        cut["choices"][0]["finish_reason"] = "length"
        harness = self.seed(FixtureRuntime(cut))
        original = archive_path(harness, "chat").read_bytes()
        run = self.run_chat(harness)
        self.assertEqual(run.status, "failed")
        self.assertEqual(archive_path(harness, "chat").read_bytes(), original)
        self.assertEqual(inspect(harness, "chat")["summary"]["text"], "")

    def test_memory_disabled_never_calls_summarizer(self):
        harness = self.seed(FixtureRuntime(response({"content": "done"})))
        run = Run(
            "new",
            "chat",
            features={"memory": False, "compaction": True, "tools": False},
        )
        harness.execute(run)
        self.assertEqual(run.status, "completed", run.error)
        self.assertFalse(run.features["compaction"])
        self.assertEqual(run.model_calls, 1)
        self.assertFalse(
            any(e["kind"] == "memory.compaction.check" for e in run.events)
        )

    def test_saved_summary_is_reused_by_a_new_harness_without_another_summary_call(
        self,
    ):
        harness = self.seed(
            FixtureRuntime(
                response({"content": "보존한 결정"}), response({"content": "완료"})
            )
        )
        first = self.run_chat(harness)
        self.assertEqual(first.status, "completed", first.error)
        config_path = self.workspace / "memory.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["trigger_characters"] = 50000
        config_path.write_text(json.dumps(config), encoding="utf-8")
        restored = Harness(
            self.workspace,
            self.root / "state",
            runtime=FixtureRuntime(response({"content": "재사용"})),
        )
        second = self.run_chat(restored)
        self.assertEqual(second.status, "completed", second.error)
        self.assertEqual(second.model_calls, 1)
        self.assertFalse(
            any(e["kind"] == "memory.compaction.start" for e in second.events)
        )
        request = next(e for e in second.events if e["kind"] == "model.request")
        self.assertIn("보존한 결정", json.dumps(request["input"], ensure_ascii=False))
        self.assertEqual(len(inspect(restored, "chat")["records"]), 5)
