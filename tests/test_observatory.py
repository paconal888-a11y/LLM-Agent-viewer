import json
import shutil
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from local_agent_lab.harness import Harness, TOOLS, build_context
from local_agent_lab.observatory import catalog
from local_agent_lab.trace import ROOT, Run
from local_agent_lab.web import Application, make_handler


class ObservatoryTests(unittest.TestCase):
    def test_every_registered_tool_links_to_its_real_function_and_schema(self):
        data = catalog()
        self.assertEqual(
            {item["name"] for item in data["tools"]},
            {item["function"]["name"] for item in TOOLS},
        )
        for item in data["tools"]:
            source = item["source"]
            lines = (
                (ROOT / source["file"])
                .read_text(encoding="utf-8")
                .splitlines(keepends=True)
            )
            actual = "".join(
                lines[
                    source["start_line"] - 1 : source["start_line"]
                    - 1
                    + len(source["code"].splitlines())
                ]
            )
            self.assertEqual(source["code"], actual)
            self.assertIn("def " + item["name"] + "(", source["code"])
            self.assertIsNone(source["line"])  # Browsing is not an execution hit.
            self.assertEqual(item["schema"]["function"]["name"], item["name"])

    def test_file_read_captures_actual_callers_and_their_suspended_lines(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            shutil.copytree(ROOT / "demo_workspace", workspace)
            run = Run("hello", "stack-test")
            build_context(run, workspace, {})
            event = next(item for item in run.events if item["kind"] == "file.read")
            stack = event["source"]["stack"]
            names = [frame["qualified_name"] for frame in stack]
            self.assertEqual(
                names[-3:], ["build_context", "discover_rules", "read_traced"]
            )
            rules_frame = stack[-2]
            line = rules_frame["code"].splitlines()[
                rules_frame["line"] - rules_frame["start_line"]
            ]
            self.assertIn("read_traced(", line)
            self.assertIn(
                "run.emit(",
                stack[-1]["code"].splitlines()[
                    stack[-1]["line"] - stack[-1]["start_line"]
                ],
            )
            snapshot = run.snapshot()
            event["source"]["stack"][-1]["code"] = "changed later"
            saved = next(
                item for item in snapshot["events"] if item["kind"] == "file.read"
            )
            self.assertNotEqual(saved["source"]["stack"][-1]["code"], "changed later")

    def test_implementation_endpoint_serves_catalog_without_arbitrary_file_access(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Application()))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/implementation?path=../../secrets"
            with urllib.request.urlopen(url) as response:
                data = json.load(response)
            self.assertEqual(len(data["tools"]), len(TOOLS))
            self.assertTrue(
                all(
                    item["source"]["file"].startswith("local_agent_lab/")
                    for item in data["components"]
                    if item["source"]
                )
            )
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
