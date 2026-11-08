"""지시 파일과 설정 파일의 계층·우선순위. 모델 응답은 픽스처이고 파일 탐색과 메시지 조립은 실제로 실행한다."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from local_agent_lab.context import (
    build_context,
    default_settings,
    discover_rules,
    resolve_settings,
)
from local_agent_lab.harness import Harness, execute_tool
from local_agent_lab.trace import ROOT, Run
from test_harness import FixtureRuntime, response, tool_call


class RuleLayerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"
        shutil.copytree(ROOT / "demo_workspace", self.workspace)
        self.run = Run("요청", "rules")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, text):
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def event(self, kind, run=None):
        return next(e for e in (run or self.run).events if e["kind"] == kind)

    def test_single_project_file_keeps_the_previous_prompt_format(self):
        block = discover_rules(self.run, self.workspace)
        self.assertTrue(block.startswith("\n<project_rules source='AGENTS.md'>\n"))
        self.assertNotIn("precedence", block)
        self.assertEqual(
            self.event("rules.resolve")["output"]["order"],
            [{"precedence": 1, "scope": "project", "source": "AGENTS.md"}],
        )

    def test_user_file_comes_first_and_project_has_higher_precedence(self):
        self.write(".user-home/AGENTS.md", "전역 규칙")
        block = discover_rules(self.run, self.workspace)
        self.assertLess(block.index("전역 규칙"), block.index("실습 작업 공간 지침"))
        self.assertIn("scope='user' precedence='1'", block)
        self.assertIn("scope='project' precedence='2'", block)
        self.assertIn("higher precedence number", block)
        self.assertEqual(
            [item["scope"] for item in self.event("rules.resolve")["output"]["order"]],
            ["user", "project"],
        )

    def test_blank_user_file_is_treated_as_absent(self):
        self.write(".user-home/AGENTS.md", "  \n\n")
        self.assertNotIn("precedence", discover_rules(self.run, self.workspace))

    def test_fallback_name_is_used_only_when_the_first_is_missing(self):
        (self.workspace / "AGENTS.md").unlink()
        self.write("agent.md", "대체 규칙")
        block = discover_rules(self.run, self.workspace)
        self.assertIn("source='agent.md'", block)

    def test_settings_choose_which_file_names_are_searched(self):
        self.write("CUSTOM.md", "맞춤 규칙")
        self.write("agent-lens.json", json.dumps({"rules": {"file_names": ["CUSTOM.md"]}}))
        resolve_settings(self.run, self.workspace)
        self.assertIn("맞춤 규칙", discover_rules(self.run, self.workspace))

    def test_later_layers_win_and_every_layer_is_recorded(self):
        self.write(".user-home/agent-lens.json", json.dumps({"loop": {"max_model_calls": 5}}))
        self.write(
            "agent-lens.json",
            json.dumps({"loop": {"max_model_calls": 3}, "rules": {"directory_rules": False}}),
        )
        settings = resolve_settings(self.run, self.workspace)
        self.assertEqual(settings["loop.max_model_calls"], 3)
        rows = {r["key"]: r for r in self.event("config.resolve")["output"]["settings"]}
        row = rows["loop.max_model_calls"]
        self.assertEqual(row["source"], "agent-lens.json")
        self.assertEqual([l["layer"] for l in row["layers"]], ["default", "user", "project"])
        self.assertEqual([l["value"] for l in row["layers"]], [8, 5, 3])
        self.assertEqual(rows["context.max_summaries"]["layer"], "default")

    def test_mode_override_is_the_highest_layer(self):
        self.write("agent-lens.json", json.dumps({"loop": {"max_model_calls": 3}}))
        settings = resolve_settings(
            self.run, self.workspace, {"loop.max_model_calls": ("mode", 16)}
        )
        self.assertEqual(settings["loop.max_model_calls"], 16)

    def test_shipped_settings_files_change_nothing(self):
        self.assertEqual(resolve_settings(self.run, self.workspace), default_settings())

    def test_invalid_settings_are_rejected_with_the_file_name(self):
        for text, message in (
            ('{"loop": {"unknown": 1}}', "unknown"),
            ('{"loop": {"max_model_calls": 99}}', "max_model_calls"),
            ('{"loop": {"max_model_calls": true}}', "max_model_calls"),
            ('{"rules": {"file_names": ["../secret.md"]}}', "file_names"),
            ('{"rules": 1}', "rules"),
        ):
            with self.subTest(text=text):
                self.write("agent-lens.json", text)
                with self.assertRaisesRegex(ValueError, message):
                    resolve_settings(Run("x", "y"), self.workspace)

    def test_model_cannot_write_rule_or_setting_layers(self):
        run = Run("x", "y", features={"writes": True})
        for path in ("agent-lens.json", ".user-home/AGENTS.md", ".user-home/note.txt"):
            with self.subTest(path=path):
                result = json.loads(
                    execute_tool(
                        run,
                        self.workspace,
                        [],
                        tool_call("write_file", path=path, content="x"),
                        set(),
                    )["content"]
                )
                self.assertFalse(result["executed_successfully"])

    def execute(self, *responses, **features):
        runtime = FixtureRuntime(*responses)
        harness = Harness(self.workspace, self.root / "state", runtime=runtime)
        run = Run("요청", "directory", features=features or None)
        harness.execute(run)
        requests = [p for route, p in runtime.requests if route.endswith("completions")]
        return run, requests

    def test_directory_rule_enters_the_next_request_after_the_tool_result(self):
        run, requests = self.execute(
            response({"tool_calls": [tool_call("read_file", path="rules-demo/memo.md")]}),
            response({"content": "끝"}),
        )
        self.assertEqual(run.status, "completed", run.error)
        self.assertNotIn("rules-demo 규칙", json.dumps(requests[0], ensure_ascii=False))
        last = requests[1]["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertIn("source='rules-demo/AGENTS.md' scope='directory'", last["content"])
        self.assertEqual(requests[1]["messages"][-2]["role"], "tool")
        event = self.event("rules.directory", run)
        self.assertEqual(event["input"]["trigger_path"], "rules-demo/memo.md")
        segment = next(s for s in run.context_segments if s.get("label") == "폴더 규칙")
        self.assertEqual(segment["origin"], "harness")
        self.assertEqual(segment["source_event_id"], event["id"])

    def test_directory_rule_is_added_once_and_outer_folders_first(self):
        self.write("a/AGENTS.md", "바깥 규칙")
        self.write("a/b/AGENTS.md", "안쪽 규칙")
        self.write("a/b/one.md", "1")
        self.write("a/b/two.md", "2")
        run, requests = self.execute(
            response({"tool_calls": [tool_call("read_file", path="a/b/one.md")]}),
            response({"tool_calls": [tool_call("read_file", path="a/b/two.md")]}),
            response({"content": "끝"}),
        )
        added = [e for e in run.events if e["kind"] == "rules.directory"]
        self.assertEqual([e["input"]["folder"] for e in added], ["a", "a/b"])
        body = json.dumps(requests[2]["messages"], ensure_ascii=False)
        self.assertEqual(body.count("바깥 규칙"), 1)
        self.assertLess(body.index("바깥 규칙"), body.index("안쪽 규칙"))

    def test_folder_without_a_rule_file_adds_nothing(self):
        run, requests = self.execute(
            response({"tool_calls": [tool_call("read_file", path="notes/agent-guide.md")]}),
            response({"content": "끝"}),
        )
        self.assertFalse([e for e in run.events if e["kind"] == "rules.directory"])
        self.assertEqual(requests[1]["messages"][-1]["role"], "tool")

    def test_failed_tool_does_not_load_folder_rules(self):
        run, _ = self.execute(
            response({"tool_calls": [tool_call("read_file", path="rules-demo/none.md")]}),
            response({"content": "끝"}),
        )
        self.assertFalse([e for e in run.events if e["kind"] == "rules.directory"])

    def test_directory_rules_can_be_turned_off_by_setting_or_switch(self):
        self.write("agent-lens.json", json.dumps({"rules": {"directory_rules": False}}))
        run, _ = self.execute(
            response({"tool_calls": [tool_call("read_file", path="rules-demo/memo.md")]}),
            response({"content": "끝"}),
        )
        self.assertFalse([e for e in run.events if e["kind"] == "rules.directory"])
        self.write("agent-lens.json", "{}")
        run, _ = self.execute(
            response({"tool_calls": [tool_call("read_file", path="rules-demo/memo.md")]}),
            response({"content": "끝"}),
            rules=False,
        )
        self.assertFalse([e for e in run.events if e["kind"] == "rules.directory"])

    def test_settings_control_the_call_budget_of_a_real_loop(self):
        self.write("agent-lens.json", json.dumps({"loop": {"max_model_calls": 1}}))
        run, requests = self.execute(
            response({"tool_calls": [tool_call("add_numbers", a=1, b=2)]}),
            response({"content": "끝"}),
        )
        self.assertEqual(run.status, "failed")
        self.assertIn("1회 제한", run.error)
        self.assertEqual(len(requests), 1)

    def test_build_context_places_layers_in_the_system_message(self):
        self.write(".user-home/AGENTS.md", "전역 규칙")
        messages, _ = build_context(self.run, self.workspace, {})
        self.assertIn("전역 규칙", messages[0]["content"])
        self.assertEqual(messages[0]["role"], "system")


if __name__ == "__main__":
    unittest.main()
