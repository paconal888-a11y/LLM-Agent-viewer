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

    def write_external_server(self, tools):
        script = self.workspace / "server.py"
        script.write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            "TOOLS = " + json.dumps(tools) + "\n"
            "for line in sys.stdin:\n"
            "    m = json.loads(line)\n"
            "    method = m['method']\n"
            "    if method == 'server/discover':\n"
            "        result = {'supportedVersions': ['2026-07-28']}\n"
            "    elif method == 'tools/list':\n"
            "        result = {'tools': TOOLS}\n"
            "    else:\n"
            "        Path(__file__).with_name('marker.txt').write_text(m['params']['name'])\n"
            "        result = {'structuredContent': {'written': True}}\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': result}), flush=True)\n",
            encoding="utf-8",
        )
        return script

    def write_external_config(self, script, **profile):
        (self.workspace / "integrations.json").write_text(
            json.dumps(
                {
                    "mcp": {
                        "name": "external",
                        "command": "{python}",
                        "args": ["-u", str(script)],
                        **profile,
                    }
                }
            ),
            encoding="utf-8",
        )

    def test_missing_allow_tools_exposes_nothing_and_records_the_policy(self):
        script = self.write_external_server(
            [
                {"name": "write_marker", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "read_marker", "inputSchema": {"type": "object", "properties": {}}},
            ]
        )
        self.write_external_config(script, read_only_tools=["read_marker"])
        run = Run("no allowlist", "mcp-missing-allowlist", features={"mcp": True, "writes": True})
        prepare(run, self.workspace)
        self.assertEqual(run.extra_tools, [])
        self.assertFalse(
            any(n.startswith("mcp_") for n in (t["function"]["name"] for t in tools_for(run)))
        )
        policy = next(e for e in run.events if e["kind"] == "mcp.policy")
        self.assertEqual(policy["output"]["policy"], "deny_without_allow_tools")
        self.assertEqual(
            sorted(policy["input"]["advertised"]), ["read_marker", "write_marker"]
        )
        filtered = [e["output"]["reason"] for e in run.events if e["kind"] == "mcp.tool.filtered"]
        self.assertEqual(filtered, ["allow_tools", "allow_tools"])
        self.assertEqual(run.integration_info[0]["policy"], "deny_without_allow_tools")
        self.assertEqual(run.integration_info[0]["allow_tools"], [])
        for name in ("mcp_external_write_marker", "mcp_external_read_marker"):
            self.assertFalse(self.call(run, name)["executed_successfully"])
        self.assertFalse((self.workspace / "marker.txt").exists())

    def test_writes_off_hides_external_tools_not_declared_read_only(self):
        script = self.write_external_server(
            [
                {"name": "write_marker", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "read_marker", "inputSchema": {"type": "object", "properties": {}}},
            ]
        )
        self.write_external_config(
            script,
            allow_tools=["write_marker", "read_marker"],
            read_only_tools=["read_marker"],
        )
        run = Run("single, writes off", "mcp-writes-off", features={"mcp": True, "writes": False})
        prepare(run, self.workspace)
        names = [t["function"]["name"] for t in tools_for(run)]
        self.assertIn("mcp_external_read_marker", names)
        self.assertNotIn("mcp_external_write_marker", names)
        self.assertNotIn("write_file", names)
        reasons = {
            e["input"]["name"]: e["output"]["reason"]
            for e in run.events
            if e["kind"] == "mcp.tool.filtered"
        }
        self.assertEqual(reasons, {"write_marker": "writes_off_requires_host_read_only"})
        # 공개 목록이 우회돼도 실행 경계에서 같은 기준을 다시 적용한다.
        run.extra_tools.append(
            {
                "type": "function",
                "function": {
                    "name": "mcp_external_write_marker",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        )
        result = self.call(run, "mcp_external_write_marker")
        self.assertFalse(result["executed_successfully"])
        self.assertFalse((self.workspace / "marker.txt").exists())
        close_all()
        # 쓰기 권한을 켜면 허용 목록의 쓰기 도구가 공개되고 실제로 실행된다.
        run = Run("single, writes on", "mcp-writes-on", features={"mcp": True, "writes": True})
        prepare(run, self.workspace)
        names = [t["function"]["name"] for t in tools_for(run)]
        self.assertIn("mcp_external_write_marker", names)
        self.assertIn("write_file", names)
        self.assertTrue(self.call(run, "mcp_external_write_marker")["data"]["written"])
        self.assertEqual((self.workspace / "marker.txt").read_text(), "write_marker")

    def test_plan_rejects_external_write_despite_server_read_only_hint(self):
        script = self.workspace / "server.py"
        script.write_text(
            """import json, sys
from pathlib import Path
for line in sys.stdin:
    m = json.loads(line)
    method = m["method"]
    if method == "server/discover":
        result = {"supportedVersions":["2026-07-28"]}
    elif method == "tools/list":
        result = {"tools":[{"name":"write_marker", "inputSchema":{"type":"object", "properties":{}}, "annotations":{"readOnlyHint":True}}]}
    else:
        Path(__file__).with_name("marker.txt").write_text("written")
        result = {"structuredContent":{"written":True}}
    print(json.dumps({"jsonrpc":"2.0", "id":m["id"], "result":result}), flush=True)
""",
            encoding="utf-8",
        )
        path = self.workspace / "integrations.json"
        path.write_text(
            json.dumps(
                {
                    "mcp": {
                        "name": "external",
                        "command": "{python}",
                        "args": ["-u", str(script)],
                        "allow_tools": ["write_marker"],
                    }
                }
            ),
            encoding="utf-8",
        )
        run = Run("plan", "external-write", mode="plan", features={"mcp": True})
        prepare(run, self.workspace)
        self.assertEqual(run.extra_tools, [])
        # 공개 목록이 잘못 추가돼도 실행 경계에서 모드 정책을 다시 검사한다.
        run.extra_tools.append(
            {
                "type": "function",
                "function": {
                    "name": "mcp_external_write_marker",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        )
        result = self.call(run, "mcp_external_write_marker")
        self.assertFalse(result["executed_successfully"])
        self.assertFalse((self.workspace / "marker.txt").exists())
        self.assertFalse(
            any(
                e["kind"] == "mcp.send"
                and e["input"]["message"]["method"] == "tools/call"
                for e in run.events
            )
        )
