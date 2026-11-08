import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from local_agent_lab.harness import Harness, tools_for
from local_agent_lab.integrations import prepare, close_all
from local_agent_lab.trace import ROOT, Run
from test_harness import tool_call


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "workspace"
        shutil.copytree(ROOT / "demo_workspace", self.workspace)
        self.harness = Harness(self.workspace, Path(self.temp.name) / "state")

    def tearDown(self):
        close_all()
        self.temp.cleanup()

    def call(self, run, name, **args):
        return json.loads(
            self.harness.dispatch_tool(run, [], tool_call(name, **args), set())[
                "content"
            ]
        )

    def test_actual_mcp_process_discovery_mapping_and_call(self):
        run = Run("MCP calc", "mcp-test", features={"mcp": True})
        prepare(run, self.workspace)
        self.assertIn("mcp_math_add", [t["function"]["name"] for t in tools_for(run)])
        result = self.call(run, "mcp_math_add", a=137, b=248)
        self.assertEqual(result["data"]["result"], 385)
        sent = [e["input"]["message"] for e in run.events if e["kind"] == "mcp.send"]
        self.assertEqual(
            [m["method"] for m in sent], ["server/discover", "tools/list", "tools/call"]
        )
        self.assertTrue(
            all(
                m["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"]
                == "2026-07-28"
                for m in sent
            )
        )
        response = next(
            e["output"]["message"]
            for e in reversed(run.events)
            if e["kind"] == "mcp.receive"
        )
        self.assertEqual(response["id"], sent[-1]["id"])
        self.assertEqual(response["result"]["structuredContent"]["result"], 385)
        self.assertNotEqual(run.integration_info[0]["pid"], __import__("os").getpid())

    @unittest.skipUnless(
        importlib.util.find_spec("pylsp"), "integrations 추가 의존성 필요"
    )
    def test_real_lsp_finds_undefined_name_after_syntax_compile_passes(self):
        text = (self.workspace / "examples/lsp_demo.py").read_text(encoding="utf-8")
        compile(text, "lsp_demo.py", "exec")
        run = Run("LSP check", "lsp-test", features={"lsp": True})
        prepare(run, self.workspace)
        result = self.call(run, "lsp_diagnostics", path="examples/lsp_demo.py")
        self.assertEqual(result["diagnostic_count"], 1)
        self.assertIn(
            "undefined name 'missing_amount'", result["diagnostics"][0]["message"]
        )
        self.assertEqual(result["diagnostics"][0]["range"]["start"]["line"], 7)
        self.assertEqual(result["locations"][0]["line_1based"], 8)
        symbols = self.call(run, "lsp_symbols", path="examples/lsp_demo.py")
        self.assertEqual(
            {symbol["name"] for symbol in symbols["result"]}, {"greet", "broken_total"}
        )
        file = self.workspace / "examples/lsp_demo.py"
        with_caller = text + '\n\nprint(greet("test"))\n'
        file.write_text(with_caller, encoding="utf-8")
        caller = next(
            i
            for i, line in enumerate(with_caller.splitlines())
            if "print(greet" in line
        )
        definition = self.call(
            run, "lsp_definition", path="examples/lsp_demo.py", line=caller, character=7
        )
        self.assertEqual(definition["result"][0]["range"]["start"]["line"], 2)
        file.write_text(with_caller.replace("missing_amount", "20"), encoding="utf-8")
        clean = self.call(run, "lsp_diagnostics", path="examples/lsp_demo.py")
        self.assertEqual(clean["diagnostic_count"], 0)
        self.assertGreater(clean["document_version"], result["document_version"])
        packets = [e["output"] for e in run.events if e["kind"] == "lsp.send"]
        self.assertTrue(all(p["header"].startswith("Content-Length:") for p in packets))

    def test_disabled_connections_do_not_launch_a_process(self):
        run = Run("plain", "no-servers")
        prepare(run, self.workspace)
        self.assertEqual(run.integration_info, [])
        self.assertEqual(run.extra_tools, [])
        self.assertEqual(run.events, [])

    def test_server_tool_list_does_not_override_host_allowlist(self):
        path = self.workspace / "integrations.json"
        config = json.loads(path.read_text())
        config["mcp"]["allow_tools"] = []
        path.write_text(json.dumps(config), encoding="utf-8")
        run = Run("blocked", "mcp-allowlist", features={"mcp": True})
        prepare(run, self.workspace)
        self.assertEqual(run.extra_tools, [])
        rejected = self.call(run, "mcp_math_add", a=1, b=2)
        self.assertFalse(rejected["executed_successfully"])
        self.assertFalse(
            any(
                e["kind"] == "mcp.send"
                and e["input"]["message"]["method"] == "tools/call"
                for e in run.events
            )
        )
