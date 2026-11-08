import json
import shutil
import tempfile
import unittest
from pathlib import Path

from local_agent_lab.project import ProjectFiles, HarnessFactory, MODULES, check_text
from local_agent_lab.harness import Harness
from local_agent_lab.trace import ROOT, Run
from test_harness import FixtureRuntime, tool_call


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "local_agent_lab").mkdir()
        for name in MODULES:
            shutil.copyfile(
                ROOT / "local_agent_lab" / f"{name}.py",
                self.root / "local_agent_lab" / f"{name}.py",
            )
        shutil.copytree(ROOT / "demo_workspace", self.root / "demo_workspace")
        self.project = ProjectFiles(self.root)
        self.factory = HarnessFactory(self.root)
        self.base = Harness(
            workspace=self.root / "demo_workspace",
            state_dir=self.root / ".runs",
            runtime=FixtureRuntime(),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def calculate(self, harness):
        run = Run("calc", "editor-test")
        result = harness.dispatch_tool(
            run, [], tool_call("add_numbers", a=17, b=25), set()
        )
        return json.loads(result["content"])["result"]

    def test_saved_code_is_loaded_next_time_without_changing_previous_version(self):
        old, version1 = self.factory.prepare(self.base)
        self.assertEqual(self.calculate(old), 42)
        file = self.project.read("local_agent_lab/toolkit.py")
        modified = file["text"].replace('"result": a + b}', '"result": a + b + 10}')
        self.assertNotEqual(modified, file["text"])
        saved = self.project.save(file["path"], modified, file["sha256"])
        new, version2 = self.factory.prepare(self.base)
        self.assertNotEqual(version1["version"], version2["version"])
        self.assertEqual(self.calculate(new), 52)
        self.assertEqual(self.calculate(old), 42)
        original = self.project.read(file["path"], version1["version"])
        self.assertEqual(original["text"], file["text"])
        self.assertFalse(original["editable"])
        self.project.restore(saved["edit_id"], saved["sha256"])
        restored, version3 = self.factory.prepare(self.base)
        self.assertEqual(version3["version"], version1["version"])
        self.assertEqual(self.calculate(restored), 42)

    def test_invalid_syntax_or_edit_conflict_does_not_replace_file(self):
        file = self.project.read("local_agent_lab/policy.py")
        result = self.project.save(file["path"], "return 1\n", file["sha256"])
        self.assertFalse(result["saved"])
        self.assertEqual(self.project.read(file["path"])["text"], file["text"])
        with self.assertRaises(ValueError):
            self.project.save(file["path"], file["text"], "not-current-hash")

    def test_failed_module_load_keeps_previous_usable_bundle(self):
        old, _ = self.factory.prepare(self.base)
        file = self.project.read("local_agent_lab/toolkit.py")
        saved = self.project.save(
            file["path"],
            file["text"] + "\nraise RuntimeError('test load failure')\n",
            file["sha256"],
        )
        self.assertTrue(saved["saved"])
        with self.assertRaises(RuntimeError):
            self.factory.prepare(self.base)
        self.assertEqual(self.calculate(old), 42)
        self.project.restore(saved["edit_id"], saved["sha256"])
        self.assertEqual(self.calculate(self.factory.prepare(self.base)[0]), 42)

    def test_editor_cannot_write_server_or_escape_workspace(self):
        for path in (
            "../outside",
            "demo_workspace/../../outside",
            "demo_workspace/../local_agent_lab/harness.py",
            "local_agent_lab/web.py",
            "local_agent_lab/reference_engine.py",
            "C:/anything",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.project.resolve(path, write=True)
        file = self.project.read("demo_workspace/AGENTS.md")
        self.assertTrue(file["editable"])

    def write_case(self, name, raw):
        target = self.root / "demo_workspace" / name
        target.write_bytes(raw)
        return target, "demo_workspace/" + name

    def test_save_keeps_the_newline_style_and_bom_of_the_file(self):
        cases = {
            "lf.md": (b"one\ntwo\n", b"one\ntwo\nadded\n", "lf", False),
            "crlf.md": (b"one\r\ntwo\r\n", b"one\r\ntwo\r\nadded\r\n", "crlf", False),
            "cr.md": (b"one\rtwo\r", b"one\rtwo\radded\r", "cr", False),
            "bom-lf.md": (
                b"\xef\xbb\xbfone\ntwo\n",
                b"\xef\xbb\xbfone\ntwo\nadded\n",
                "lf",
                True,
            ),
            "bom-crlf.md": (
                b"\xef\xbb\xbfone\r\ntwo\r\n",
                b"\xef\xbb\xbfone\r\ntwo\r\nadded\r\n",
                "crlf",
                True,
            ),
        }
        for name, (raw, expected, newline, bom) in cases.items():
            with self.subTest(file=name):
                target, relative = self.write_case(name, raw)
                opened = self.project.read(relative)
                self.assertNotIn("\r", opened["text"])
                self.assertEqual(
                    opened["file_format"],
                    {"newline": newline, "mixed": False, "bom": bom},
                )
                saved = self.project.save(
                    relative, opened["text"] + "added\n", opened["sha256"]
                )
                self.assertEqual(target.read_bytes(), expected)
                self.assertFalse(saved["newline_unified"])
                self.project.restore(saved["edit_id"], saved["sha256"])
                self.assertEqual(target.read_bytes(), raw)

    def test_save_without_change_leaves_the_bytes_identical(self):
        raw = b"\xef\xbb\xbfone\r\ntwo\r\n"
        target, relative = self.write_case("same.md", raw)
        opened = self.project.read(relative)
        self.project.save(relative, opened["text"], opened["sha256"])
        self.assertEqual(target.read_bytes(), raw)

    def test_mixed_newlines_are_unified_and_restored_exactly(self):
        raw = b"one\r\ntwo\nthree\r\n"
        target, relative = self.write_case("mixed.md", raw)
        opened = self.project.read(relative)
        self.assertEqual(
            opened["file_format"], {"newline": "crlf", "mixed": True, "bom": False}
        )
        saved = self.project.save(relative, opened["text"], opened["sha256"])
        self.assertTrue(saved["newline_unified"])
        self.assertEqual(target.read_bytes(), b"one\r\ntwo\r\nthree\r\n")
        restored = self.project.restore(saved["edit_id"], saved["sha256"])
        self.assertFalse(restored["newline_unified"])
        self.assertTrue(restored["file_format"]["mixed"])
        self.assertEqual(target.read_bytes(), raw)

    def test_file_without_newline_gets_lf_when_a_line_is_added(self):
        target, relative = self.write_case("single.md", b"one")
        opened = self.project.read(relative)
        self.assertIsNone(opened["file_format"]["newline"])
        self.project.save(relative, "one\ntwo", opened["sha256"])
        self.assertEqual(target.read_bytes(), b"one\ntwo")

    def test_restore_of_older_edit_without_original_bytes_still_works(self):
        raw = b"one\r\ntwo\r\n"
        target, relative = self.write_case("older.md", raw)
        opened = self.project.read(relative)
        saved = self.project.save(relative, "changed\n", opened["sha256"])
        (self.project.state_dir / "edits" / saved["edit_id"] / "before.bin").unlink()
        self.project.restore(saved["edit_id"], saved["sha256"])
        self.assertEqual(target.read_bytes(), raw)

    def test_save_treats_crlf_and_lf_from_the_client_as_the_same_text(self):
        raw = b"one\ntwo\n"
        target, relative = self.write_case("client.md", raw)
        opened = self.project.read(relative)
        saved = self.project.save(relative, "one\r\ntwo\r\nthree\r\n", opened["sha256"])
        self.assertEqual(target.read_bytes(), b"one\ntwo\nthree\n")
        self.assertEqual(saved["sha256"], self.project.read(relative)["sha256"])

    def test_check_compiles_python_without_executing_it(self):
        self.assertTrue(
            check_text("module.py", "raise RuntimeError('not executed')")["passed"]
        )
        self.assertFalse(check_text("module.py", "return 1")["passed"])
        self.assertFalse(check_text("file.json", "{invalid}")["passed"])


if __name__ == "__main__":
    unittest.main()
