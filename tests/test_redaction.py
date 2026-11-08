import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from local_agent_lab import redaction
from local_agent_lab.harness import Harness
from local_agent_lab.trace import ROOT, Run
from test_harness import FixtureRuntime, response, tool_call


class RedactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        shutil.copytree(ROOT / "demo_workspace", self.workspace)
        self.home = self.root / "home" / "alice"
        self.home.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def real_snapshot(self):
        secret = "sk-" + "a1b2c3d4e5" * 3
        (self.workspace / "data" / "env.txt").write_text(
            "API=" + secret + "\nmail=person@example.com\nhost=10.0.0.7\n",
            encoding="utf-8",
        )
        runtime = FixtureRuntime(
            response({"tool_calls": [tool_call("read_file", path="data/env.txt")]}),
            response({"content": "읽었습니다: " + secret}),
        )
        harness = Harness(self.workspace, self.root / "state", runtime=runtime)
        run = Run("env.txt를 읽어줘 " + str(self.workspace), "share-test", features={"rules": False})
        harness.execute(run)
        return run.snapshot(compact=True), secret

    @staticmethod
    def in_json(path):
        """JSON 본문 안에서 경로가 보이는 형태. Windows 경로는 역슬래시가 이스케이프되어 들어간다."""
        return json.dumps(str(path), ensure_ascii=False)[1:-1]

    def test_share_copy_redacts_nested_json_strings_and_sources_without_touching_the_original(self):
        snapshot, secret = self.real_snapshot()
        original = copy.deepcopy(snapshot)
        encoded = json.dumps(snapshot, ensure_ascii=False)
        self.assertIn(secret, encoded)
        self.assertIn(self.in_json(self.workspace), encoded)
        shared = redaction.share_copy(
            snapshot, workspace=self.workspace, home=self.home, user="alice"
        )
        shared_text = json.dumps(shared, ensure_ascii=False)
        self.assertEqual(snapshot, original, "원본 스냅샷은 바뀌지 않아야 한다")
        self.assertNotIn(secret, shared_text)
        self.assertNotIn("person@example.com", shared_text)
        self.assertNotIn("10.0.0.7", shared_text)
        self.assertNotIn(self.in_json(self.workspace), shared_text)
        self.assertNotIn(self.in_json(ROOT), shared_text)
        # JSON 문자열 안에 JSON으로 다시 들어간 경로(이스케이프된 역슬래시 포함)도 치환된다.
        self.assertNotIn(self.in_json(str(self.workspace).replace("\\", "\\\\")), shared_text)
        self.assertIn("<WORKSPACE>", shared_text)
        self.assertEqual(
            redaction.Redactor(home=self.home, user="x").text(str(ROOT) + "/web/x.js " + str(ROOT).replace("\\", "\\\\")),
            "<PROJECT>/web/x.js <PROJECT>",
        )
        self.assertIn("<REDACTED:openai_key>", shared_text)
        self.assertIn("<EMAIL>", shared_text)
        self.assertIn("<IP>", shared_text)
        self.assertIn("127.0.0.1", json.dumps({"x": redaction.Redactor(home=self.home, user="x").text("127.0.0.1")}))
        # 소스 스냅샷의 코드 본문 안 경로도 같은 규칙을 거친다. 소스 자체는 보존된다.
        self.assertEqual(len(shared["source_snapshots"]), len(snapshot["source_snapshots"]))
        self.assertTrue(shared["share"]["redacted"])
        self.assertEqual(shared["share"]["source_run_id"], snapshot["id"])
        self.assertGreater(shared["share"]["counts"]["path"], 0)
        # 소스 스냅샷에는 이 테스트 파일의 코드(자리표시자 문자열 포함)도 들어가므로 이벤트·답변만 센다.
        without_sources = json.dumps({k: v for k, v in shared.items() if k != "source_snapshots"}, ensure_ascii=False)
        self.assertEqual(shared["share"]["counts"]["secret"], without_sources.count("<REDACTED:openai_key>"))
        # 바이트 수·해시 같은 측정값은 다시 계산하지 않는다.
        before = next(e for e in snapshot["events"] if e["kind"] == "file.read")
        after = next(e for e in shared["events"] if e["kind"] == "file.read")
        self.assertEqual(before["output"]["byte_count"], after["output"]["byte_count"])
        self.assertEqual(before["output"]["sha256"], after["output"]["sha256"])

    def test_preview_counts_without_changing_anything(self):
        snapshot, secret = self.real_snapshot()
        original = copy.deepcopy(snapshot)
        report = redaction.preview(snapshot, workspace=self.workspace, home=self.home, user="alice")
        self.assertEqual(snapshot, original)
        self.assertGreaterEqual(report["counts"]["secret"], 2)
        self.assertGreaterEqual(report["counts"]["email"], 1)
        self.assertTrue(any(secret[:20] in x for x in report["examples"]["secret"]))
        self.assertEqual(report["includes"]["tool_results"], 1)
        self.assertGreaterEqual(report["includes"]["file_texts"], 1)
        self.assertTrue(report["options"]["paths"])
        self.assertIn(secret, json.dumps(snapshot))

    def test_percent_encoded_paths_in_lsp_uris_are_replaced(self):
        root = Path(self.temp.name) / "Corp Folder" / "한글 프로젝트"
        redactor = redaction.Redactor(workspace=root, home=Path(self.temp.name), user="")
        uri = root.joinpath("demo.py").resolve().as_uri()
        self.assertIn("%20", uri)
        replaced = redactor.text(uri)
        self.assertNotIn("Corp%20Folder", replaced)
        self.assertIn("<WORKSPACE>", replaced)

    def test_options_omit_bodies_and_generic_home_paths_and_user_name(self):
        snapshot, secret = self.real_snapshot()
        shared = redaction.share_copy(
            snapshot,
            {"omit_file_text": True, "omit_sources": True},
            workspace=self.workspace,
            home=self.home,
            user="alice",
        )
        text = json.dumps(shared, ensure_ascii=False)
        self.assertNotIn(secret, text)
        decode = next(e for e in shared["events"] if e["kind"] == "file.decode" and e["input"]["path"].endswith("env.txt"))
        self.assertTrue(decode["output"]["text"].startswith("<OMITTED "))
        result = next(e for e in shared["events"] if e["kind"] == "tool.result")
        self.assertTrue(result["output"]["content"].startswith("<OMITTED "))
        self.assertTrue(all(v["code"] == "<OMITTED SOURCE>" for v in shared["source_snapshots"].values()))
        redactor = redaction.Redactor(home=self.home, user="alice")
        self.assertEqual(
            redactor.text('{"p": "C:\\\\Users\\\\someone\\\\x", "q": "/home/other/y", "u": "alice"}'),
            '{"p": "<USER_HOME>\\\\x", "q": "<USER_HOME>/y", "u": "<USER>"}',
        )
        self.assertEqual(redactor.text("aliceastic realiceable"), "aliceastic realiceable")
        self.assertEqual(redaction.parse_options({"paths": ["0"], "emails": ["1"], "bogus": ["1"]}), {"paths": False, "emails": True})


if __name__ == "__main__":
    unittest.main()
