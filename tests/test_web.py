import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from local_agent_lab.harness import Harness
from local_agent_lab.project import ProjectFiles
from local_agent_lab.trace import ROOT, Run
from local_agent_lab.web import Application, make_handler
from test_harness import FixtureRuntime, response


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / "demo_workspace", self.root / "demo_workspace")
        self.app = Application(
            Harness(
                self.root / "demo_workspace",
                self.root / ".runs",
                runtime=FixtureRuntime(response({"content": "hello"})),
            )
        )
        self.app.project = ProjectFiles(self.root)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.temp.cleanup()

    def request(self, path, payload=None, headers=None):
        request = urllib.request.Request(
            self.url + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=3) as reply:
            return json.load(reply)

    def test_foreign_origin_and_host_cannot_write(self):
        for headers in (
            {"Origin": "https://example.com"},
            {"Host": "untrusted.example:" + str(self.server.server_port)},
        ):
            with (
                self.subTest(headers=headers),
                self.assertRaises(urllib.error.HTTPError) as error,
            ):
                self.request("/api/project/save", {}, headers)
            self.assertEqual(error.exception.code, 403)

    def test_input_inspection_script_is_served_as_javascript(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(self.url + "/inspection.js", timeout=3) as reply:
            self.assertIn("javascript", reply.headers["Content-Type"])
            self.assertIn(b"scope.AgentInspection", reply.read())

    def test_memory_endpoint_uses_a_validated_session_identifier(self):
        empty = self.request("/api/memory?session_id=new-chat")
        self.assertEqual(empty["records"], [])
        self.assertFalse(empty["weights_updated"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/memory?session_id=../../secret")
        self.assertEqual(error.exception.code, 400)

    def test_sample_mode_loads_real_record_without_model_calls(self):
        app = Application(self.app.harness, samples_only=True)
        self.assertFalse(app.health()["connected"])
        sample = app.get_run("sample-tensors", compact=True)
        self.assertTrue(sample["sample"])
        self.assertTrue(
            any(e["kind"] == "reference.cache.write" for e in sample["events"])
        )
        encoded = json.dumps(sample, ensure_ascii=False, allow_nan=False)
        self.assertNotRegex(encoded, r"[A-Z]:[\\/]+Users[\\/]+")
        self.assertEqual(self.app.harness.runtime.requests, [])
        with self.assertRaises(ValueError):
            app.start("hello", "sample", False)

    def test_changing_backend_keeps_the_server_given_at_startup(self):
        class Reference(FixtureRuntime):
            backend = "reference"

        app = Application(
            Harness(self.app.harness.workspace, self.app.harness.state_dir, runtime=Reference()),
            llama_url="http://127.0.0.1:1234",
            llama_model="custom-model",
        )
        app.select_backend("llama")
        self.assertEqual(app.harness.runtime.base_url, "http://127.0.0.1:1234")
        self.assertEqual(app.harness.model, "custom-model")

    def test_status_identifies_the_app_instance_for_launcher_and_stop_scripts(self):
        import os

        from local_agent_lab import app_version

        status = self.request("/api/status")
        self.assertEqual(status["app"], "Agent Lens")
        self.assertEqual(status["version"], app_version())
        self.assertNotEqual(status["version"], "unknown")
        self.assertEqual(Path(status["project_root"]), ROOT)
        self.assertEqual(status["pid"], os.getpid())
        self.assertEqual(status["port"], self.server.server_port)
        self.assertEqual(len(status["instance"]), 12)
        self.assertFalse(status["samples_only"])
        self.assertEqual(status["instance"], self.request("/api/status")["instance"])
        sample = Application(self.app.harness, samples_only=True).health()
        self.assertEqual(sample["mode"], "sample")
        self.assertTrue(sample["samples_only"])
        self.assertEqual(sample["project_root"], str(ROOT))
        self.assertNotEqual(sample["instance"], status["instance"])

    def test_export_preview_and_share_copy_endpoints(self):
        preview = self.request("/api/runs/sample-summary-reuse/export/preview")
        self.assertIn("counts", preview)
        self.assertEqual(preview["event_count"], 79)
        self.assertTrue(preview["options"]["paths"])
        self.assertGreaterEqual(preview["includes"]["model_requests"], 1)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(
            self.url + "/api/runs/sample-summary-reuse/export/share?omit_sources=1", timeout=5
        ) as reply:
            self.assertIn("attachment", reply.headers["Content-Disposition"])
            self.assertIn("-share.json", reply.headers["Content-Disposition"])
            shared = json.load(reply)
        self.assertTrue(shared["share"]["redacted"])
        self.assertFalse(shared["share"]["options"]["omit_file_text"])
        self.assertTrue(shared["share"]["options"]["omit_sources"])
        self.assertTrue(all(v["code"] == "<OMITTED SOURCE>" for v in shared["source_snapshots"].values()))
        self.assertEqual(shared["id"], "sample-summary-reuse")
        self.assertEqual(shared["sample"]["source_run_id"], "cce2190999264fbb")
        raw = self.request("/api/runs/sample-summary-reuse/export")
        self.assertNotIn("share", raw)
        self.assertNotEqual(raw["source_snapshots"], shared["source_snapshots"])

    def test_project_manifest_matches_the_version_recorded_on_a_run(self):
        from local_agent_lab.project import HarnessFactory

        manifest = self.request("/api/project/manifest")
        self.assertEqual(len(manifest["files"]), 9)
        self.assertEqual(len(manifest["version"]), 64)
        self.assertIn("sha256", manifest["workspace"])
        self.assertTrue(any(f["path"] == "AGENTS.md" for f in manifest["workspace"]["files"]))
        _, version = HarnessFactory(state_dir=self.root / ".runs").prepare(self.app.harness)
        self.assertEqual(version["version"], manifest["version"])
        self.assertEqual(version["files"], manifest["files"])

    def test_worker_thread_exception_outside_the_loop_finalizes_the_run(self):
        # W-01: Harness.execute 자체가 예외를 던지면 실행이 영구 running으로 남아 모든 쓰기가 막혔다.
        import time

        class Broken(Harness):
            def execute(self, run):
                raise AttributeError("'Run' object has no attribute 'promt'")

        self.app.harness = Broken(self.app.harness.workspace, self.app.harness.state_dir, runtime=FixtureRuntime())
        run = self.app.start("깨진 하네스", "broken", False)
        deadline = time.monotonic() + 3
        while not run.closed and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(run.status, "failed")
        self.assertIn("promt", run.error)
        self.assertIsNone(self.request("/api/activity")["run"])
        failure = next(e for e in run.events if e["kind"] == "run.error")
        self.assertEqual(failure["output"]["type"], "AttributeError")
        self.assertTrue(failure["output"]["file"].endswith("test_web.py"))
        self.assertTrue((self.root / ".runs" / "traces" / (run.id + ".json")).exists())
        # 잠금이 풀려 편집·새 실행이 다시 가능하다.
        self.assertTrue(self.request("/api/project/check", {"path": "demo_workspace/AGENTS.md", "text": "# 규칙\n"})["passed"])
        self.assertFalse(self.app.busy())

    def test_missing_edit_id_and_sample_mode_writes_return_json_errors(self):
        # W-03: 없는 edit_id가 미처리 FileNotFoundError로 응답 없이 연결을 끊었다. W-05: 샘플 모드 저장 허용.
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/project/restore", {"edit_id": "20260101-000000-deadbeef", "expected_sha256": "x"})
        self.assertEqual(error.exception.code, 400)
        self.assertIn("수정 기록", json.load(error.exception)["error"])
        self.app.samples_only = True
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/project/save", {"path": "demo_workspace/AGENTS.md", "text": "x", "expected_sha256": "y"})
            self.assertEqual(error.exception.code, 400)
            self.assertIn("샘플 보기 모드", json.load(error.exception)["error"])
        finally:
            self.app.samples_only = False
        history = self.request("/api/history")
        self.assertIsInstance(history, list)

    def test_project_edit_is_blocked_during_active_run(self):
        run = Run("paused", "web-test")
        run.status = "paused"
        self.app.runs[run.id] = run
        before = self.app.project.read("demo_workspace/AGENTS.md")
        with self.assertRaises(urllib.error.HTTPError):
            self.request(
                "/api/project/save",
                {
                    "path": before["path"],
                    "text": "changed",
                    "expected_sha256": before["sha256"],
                },
            )
        self.assertEqual(self.app.project.read(before["path"])["text"], before["text"])

    def test_refresh_can_discover_and_control_paused_work_without_a_browser_run_id(
        self,
    ):
        run = self.app.start("pause here", "refresh-recovery", True)
        with run.condition:
            self.assertTrue(
                run.condition.wait_for(lambda: run.status == "paused", timeout=3)
            )
        active = self.request("/api/activity")
        self.assertEqual(active["run"]["id"], run.id)
        self.assertEqual(active["run"]["session_id"], "refresh-recovery")
        with self.assertRaises(urllib.error.HTTPError) as blocked:
            self.request("/api/runs", {"prompt": "new draft", "session_id": "new"})
        self.assertEqual(blocked.exception.code, 409)
        self.assertEqual(json.load(blocked.exception)["activity"]["run"]["id"], run.id)
        self.request("/api/runs/" + run.id + "/control", {"action": "stop"})
        with run.condition:
            self.assertTrue(
                run.condition.wait_for(lambda: run.status == "stopped", timeout=3)
            )
        self.assertIsNone(self.request("/api/activity")["run"])

        self.assertTrue(
            self.request("/api/runs/" + run.id + "/control", {"action": "stop"})[
                "already_finished"
            ]
        )

    def test_saved_paused_trace_is_not_reported_as_a_live_worker(self):
        run = Run("old trace", "old")
        run.status = "paused"
        run.save(self.app.harness.state_dir / "traces")
        self.assertIsNone(self.request("/api/activity")["run"])
        snapshot = self.request("/api/runs/" + run.id + "?compact=1")
        self.assertEqual(snapshot["status"], "interrupted")
        self.assertEqual(snapshot["recorded_status"], "paused")

    def test_browser_observation_and_full_journey_are_persisted(self):
        result = self.request(
            "/api/runs",
            {
                "prompt": "hello",
                "session_id": "web-journey",
                "client_trace": {"input_events": 2, "characters": 5, "utf8_bytes": 5},
            },
        )
        run = self.app.runs[result["id"]]
        with run.condition:
            self.assertTrue(
                run.condition.wait_for(
                    lambda: run.status in {"completed", "failed"}, timeout=3
                )
            )
        self.assertEqual(run.status, "completed")
        snapshot = self.request("/api/runs/" + run.id + "?compact=1")
        kinds = [e["kind"] for e in snapshot["events"]]
        self.assertEqual(
            kinds[:4],
            [
                "browser.input",
                "browser.serialize",
                "http.body.read",
                "http.json.decode",
            ],
        )
        self.assertEqual(kinds[-1], "run.complete")
        self.assertIn("context.assemble", kinds)
        self.assertIn("validation.final", kinds)
        source = snapshot["events"][0]["source"]
        self.assertEqual(
            snapshot["source_snapshots"][source["source_ref"]]["file"],
            "web/workbench.js",
        )
        for _ in range(2):
            self.request("/api/runs/" + run.id + "/render", {"text": run.answer})
        final = self.request("/api/runs/" + run.id + "?compact=1")
        self.assertEqual(final["event_count"], snapshot["event_count"] + 1)
        self.assertEqual(final["events"][-1]["kind"], "browser.render")
