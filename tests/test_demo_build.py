"""웹 데모 빌드. 실제 저장 기록으로 정적 파일을 만들고 모델은 호출하지 않는다."""

import getpass
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from local_agent_lab.trace import ROOT

spec = importlib.util.spec_from_file_location("build_demo", ROOT / "scripts" / "build_demo.py")
build_demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_demo)


class DemoBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.site = build_demo.build(Path(cls.temp.name) / "site")
        cls.samples = json.loads((ROOT / "samples" / "index.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def load(self, name):
        return json.loads((self.site / "data" / name).read_text(encoding="utf-8"))

    def test_every_sample_has_a_record_a_diagnosis_and_a_report(self):
        for sample in self.samples:
            name = "sample-" + sample["slug"] + ".json"
            run = self.load("runs/" + name)
            self.assertEqual(len(run["events"]), sample["events"])
            self.assertIn("focus", self.load("diagnosis/" + name))
            self.assertIn("repairs", self.load("feedback/" + name))

    def test_status_says_this_is_a_replay_and_hides_local_details(self):
        status = self.load("status.json")
        self.assertTrue(status["demo"])
        self.assertEqual(status["mode"], "sample")
        self.assertFalse(status["connected"])
        for key in ("project_root", "working_directory", "pid", "instance", "workspace"):
            self.assertNotIn(key, status)

    def test_files_are_read_only(self):
        self.assertFalse(any(item["editable"] for item in self.load("project.json")["files"]))
        number = self.load("files/index.json")["demo_workspace/AGENTS.md"]
        self.assertFalse(self.load(f"files/{number}.json")["editable"])

    def test_page_loads_from_a_sub_path_with_the_fetch_replacement_first(self):
        html = (self.site / "index.html").read_text(encoding="utf-8")
        self.assertNotRegex(html, r'(href|src)="/(?!/)')
        self.assertLess(html.index('src="demo.js"'), html.index('src="workbench.js"'))
        for name in build_demo.ASSETS:
            self.assertTrue((self.site / name).is_file(), name)

    def test_no_private_paths_reach_the_public_site(self):
        needles = {str(Path.home()).lower(), str(ROOT).lower(), "onedrive"}
        user = getpass.getuser().lower()
        if len(user) >= 4:
            needles.add(user)
        for path in (self.site / "data").rglob("*.json"):
            text = path.read_text(encoding="utf-8").lower().replace("\\\\", "\\")
            for needle in needles:
                self.assertNotIn(needle, text, path.name)


if __name__ == "__main__":
    unittest.main()
