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

    def test_check_compiles_python_without_executing_it(self):
        self.assertTrue(
            check_text("module.py", "raise RuntimeError('not executed')")["passed"]
        )
        self.assertFalse(check_text("module.py", "return 1")["passed"])
        self.assertFalse(check_text("file.json", "{invalid}")["passed"])


if __name__ == "__main__":
    unittest.main()
