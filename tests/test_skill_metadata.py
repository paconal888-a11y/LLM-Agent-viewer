import shutil
import tempfile
import unittest
from pathlib import Path

from local_agent_lab.context import (
    SKILL_METADATA_FORMAT,
    discover_skills,
    parse_skill_metadata,
)
from local_agent_lab.trace import ROOT, Run


class SkillMetadataTests(unittest.TestCase):
    def test_supported_single_line_scalars_are_read_exactly(self):
        cases = {
            "---\nname: file-summary\ndescription: 문서를 읽고 요약한다.\n---\n본문": (
                "file-summary",
                "문서를 읽고 요약한다.",
            ),
            '---\nname: "quoted"\ndescription: "Reads: files, \\"then\\" summarizes"\n---\n': (
                "quoted",
                'Reads: files, "then" summarizes',
            ),
            "---\nname: 'it''s'\ndescription: 'x: y'\n---\n": ("it's", "x: y"),
            "---\nname: c # 주석\ndescription: d # 설명 주석\n---\n": ("c", "d"),
            "---\r\nname: crlf\r\ndescription: d\r\n---\r\n": ("crlf", "d"),
            "﻿---\nname: bom\ndescription: d\n---\n": ("bom", "d"),
            "---\nname: x\nversion: 2\ndescription: see http://h/#frag and a#b\n---\n\n---\n": (
                "x",
                "see http://h/#frag and a#b",
            ),
        }
        for text, expected in cases.items():
            metadata, detail = parse_skill_metadata(text)
            self.assertEqual((metadata["name"], metadata["description"]), expected, text)
            self.assertEqual(detail["problems"], [])

    def test_unsupported_yaml_is_rejected_with_a_reason_instead_of_a_wrong_value(self):
        cases = {
            "---\nname: folded\ndescription: >\n  첫 줄\n  둘째 줄\n---\n": "multiline_scalar",
            "---\nname: literal\ndescription: |\n  여러 줄\n---\n": "multiline_scalar",
            "---\nname: [a, b]\ndescription: x\n---\n": "flow_collection",
            "---\n  name: ind\n  description: d\n---\n": "indented_key",
            "---\nname: a\nname: b\ndescription: d\n---\n": "duplicate_key",
            "---\nname: only\n---\n": "missing_key",
            "# title\nname: x\ndescription: y\n": "front_matter_missing",
            "---\nname: x\ndescription: y\n": "front_matter_unterminated",
            '---\nname: "x" extra\ndescription: y\n---\n': "text_after_closing_quote",
            "---\nname:\ndescription: y\n---\n": "empty_value",
            "---\nname: &a x\ndescription: *a\n---\n": "anchor_alias_or_tag",
            "---\nname: " + "n" * 65 + "\ndescription: y\n---\n": "too_long",
        }
        for text, reason in cases.items():
            metadata, detail = parse_skill_metadata(text)
            self.assertIsNone(metadata, text)
            self.assertEqual(detail["reason"], reason, text)
        # 이전 구현은 '>'를 설명 문자열로 넘겼다. 거부 사유가 있을 때 값은 절대 만들지 않는다.
        self.assertNotIn(">", str(parse_skill_metadata("---\nname: f\ndescription: >\n  x\n---\n")[0]))

    def test_discovery_records_each_skipped_skill_with_its_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            shutil.copytree(ROOT / "demo_workspace", workspace)
            folded = workspace / "skills" / "folded"
            folded.mkdir()
            (folded / "SKILL.md").write_text(
                "---\nname: folded\ndescription: >\n  여러 줄 설명\n---\n# 본문\n",
                encoding="utf-8",
            )
            run = Run("skills", "skill-metadata")
            catalog = discover_skills(run, workspace)
            self.assertEqual(
                sorted(item["name"] for item in catalog), ["code-check", "file-summary"]
            )
            events = [e for e in run.events if e["kind"] == "skills.metadata"]
            self.assertEqual(len(events), 3)
            rejected = next(e for e in events if not e["output"]["accepted"])
            self.assertEqual(rejected["input"]["path"], "skills/folded/SKILL.md")
            self.assertEqual(rejected["output"]["reason"], "multiline_scalar")
            self.assertEqual(rejected["status"], "warning")
            self.assertEqual(rejected["input"]["format"], SKILL_METADATA_FORMAT["id"])
            listing = next(e for e in run.events if e["kind"] == "skills.catalog")
            self.assertEqual(
                listing["output"]["skipped"],
                [{"path": "skills/folded/SKILL.md", "reason": "multiline_scalar"}],
            )
            self.assertEqual(listing["output"]["format"]["id"], SKILL_METADATA_FORMAT["id"])


if __name__ == "__main__":
    unittest.main()
