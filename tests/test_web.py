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
