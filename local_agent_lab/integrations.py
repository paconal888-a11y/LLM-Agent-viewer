"""교체 가능한 실제 MCP·LSP stdio 어댑터. 모델 도구와 외부 프로토콜의 경계."""

from __future__ import annotations
import atexit
import functools
import hashlib
import json
import os
import re
import sys
import threading
from .files import read_traced, workspace_path
from .runtime import RuntimeFailure
from .stdio_rpc import StdioRpc, RpcError
from .trace import ROOT

MCP_VERSION = "2026-07-28"
_clients = {}
_pool_lock = threading.Lock()


def close_all():
    for client in list(_clients.values()):
        client.close()
    _clients.clear()


atexit.register(close_all)


def client_for(config, protocol, workspace=None):
    command = config.get("command")
    args = config.get("args", [])
    if (
        not isinstance(command, str)
        or not isinstance(args, list)
        or not all(isinstance(v, str) for v in args)
        or len(args) > 30
    ):
        raise ValueError("서버 command와 args 목록을 확인하세요.")
    argv = [sys.executable if command == "{python}" else command, *args]
    key = json.dumps(
        [protocol, argv, str(workspace) if protocol == "lsp" else None],
        ensure_ascii=False,
    )
    with _pool_lock:
        if key not in _clients:
            _clients[key] = StdioRpc(argv, str(ROOT), protocol)
        return _clients[key]


def mcp_params(client, params=None):
    if getattr(client, "legacy_mcp", False):
        return params or {}
    return {
        **(params or {}),
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": MCP_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {
                "name": "Agent Lens",
                "version": "0.4.0",
            },
        },
    }


def prepare_mcp(run, config):
    client = client_for(config, "mcp")
    client.start(run)
    try:
        info = client.request(run, "server/discover", mcp_params(client))
        client.legacy_mcp = False
        if MCP_VERSION not in info.get("supportedVersions", []):
            raise RuntimeFailure("지원하는 MCP 버전이 없습니다.")
    except RpcError as error:
        if error.error.get("code") not in {-32601, -32002}:
            raise
        client.legacy_mcp = True
        info = client.request(
            run,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "Agent Lens", "version": "0.4.0"},
            },
        )
        if info.get("protocolVersion") != "2025-11-25":
            raise RuntimeFailure("지원하지 않는 이전 MCP 버전입니다.")
        client.notify(run, "notifications/initialized")
    response = client.request(run, "tools/list", mcp_params(client))
    if response.get("nextCursor"):
        raise RuntimeFailure(
            "이 학습용 어댑터는 도구 목록 한 페이지를 지원합니다. 서버 도구 범위를 줄이세요."
        )
    prefix = config.get("name", "external")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,24}", prefix):
        raise ValueError("MCP 서버 이름은 짧은 영문 식별자를 사용하세요.")
    exposed = []
    allowed = config.get("allow_tools")
    if allowed is not None and (
        not isinstance(allowed, list)
        or not all(isinstance(name, str) for name in allowed)
    ):
        raise ValueError("allow_tools는 서버 도구 이름의 목록이어야 합니다.")
    for tool in response.get("tools", []):
        remote = tool.get("name", "")
        if allowed is not None and remote not in allowed:
            run.emit(
                "mcp.tool.filtered",
                "MCP 도구 공개 범위에서 제외",
                "서버가 도구를 제공해도 하네스의 허용 목록에 없으면 모델에 공개하거나 실행하지 않습니다.",
                category="integrations",
                actor="mcp",
                input=tool,
                output={"exposed": False, "reason": "allow_tools"},
            )
            continue
        name = "mcp_" + prefix + "_" + remote
        schema = tool.get("inputSchema", {})
        if (
            not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", name)
            or schema.get("type") != "object"
        ):
            raise RuntimeFailure("지원하지 않는 MCP 도구 이름·schema입니다.")
        if any(
            v.get("type") not in {"string", "integer", "number", "boolean"}
            for v in schema.get("properties", {}).values()
        ):
            raise RuntimeFailure(
                "학습용 검증기는 단순한 스칼라 인자 schema를 지원합니다. policy.py 검증기를 확장하세요."
            )
        mapped = {
            "type": "function",
            "function": {
                "name": name,
                "description": tool.get("description", remote),
                "parameters": schema,
            },
        }
        run.extra_tools.append(mapped)
        run.tool_handlers[name] = functools.partial(
            _mcp_tool, client=client, remote=remote
        )
        exposed.append(name)
        run.emit(
            "mcp.schema",
            "MCP 도구를 모델용 schema로 연결",
            "서버의 inputSchema를 function.parameters로 매핑하고 충돌을 피할 접두부를 붙였습니다. 아직 도구를 실행한 것은 아닙니다.",
            category="integrations",
            actor="mcp",
            input=tool,
            output=mapped,
        )
    run.integration_info.append(
        {
            "protocol": "mcp",
            "server": prefix,
            "pid": client.process.pid,
            "version": "2025-11-25"
            if getattr(client, "legacy_mcp", False)
            else MCP_VERSION,
            "tools": exposed,
            "discovery": info,
        }
    )


def _mcp_tool(ctx, client, remote, **args):
    result = client.request(
        ctx.run,
        "tools/call",
        mcp_params(client, {"name": remote, "arguments": args}),
        ctx.parent,
    )
    if result.get("resultType", "complete") != "complete":
        raise ValueError("이 클라이언트는 complete 도구 결과만 지원합니다.")
    if result.get("isError"):
        return ctx.finish(
            {"error": "MCP 서버가 도구 실패를 반환했습니다.", "mcp_result": result}
        )
    data = result.get("structuredContent")
    return ctx.finish(
        {
            "server_tool": remote,
            "data": data,
            "content": result.get("content", []),
            "protocol": "mcp",
        }
    )


def prepare_lsp(run, config, workspace):
    client = client_for(config, "lsp", workspace)
    client.start(run)
    root = workspace.as_uri()
    if getattr(client, "initialized_pid", None) != client.process.pid:
        info = client.request(
            run,
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": root,
                "workspaceFolders": [{"uri": root, "name": "Agent Lens workspace"}],
                "capabilities": {
                    "textDocument": {"publishDiagnostics": {"versionSupport": True}}
                },
                "clientInfo": {"name": "Agent Lens", "version": "0.4.0"},
            },
        )
        client.notify(run, "initialized")
        client.notify(
            run,
            "workspace/didChangeConfiguration",
            {
                "settings": {
                    "pylsp": {
                        "plugins": {
                            "pyflakes": {"enabled": True},
                            "pycodestyle": {"enabled": False},
                            "mccabe": {"enabled": False},
                            "pydocstyle": {"enabled": False},
                            "pylint": {"enabled": False},
                        }
                    }
                }
            },
        )
        client.initialized_pid = client.process.pid
        client.documents = {}
        client.lsp_info = info
    for name, description, properties in [
        (
            "lsp_diagnostics",
            "Get actual Python language-server diagnostics. Use locations.line_1based for user-facing line numbers; raw diagnostics.range is zero-based.",
            {"path": {"type": "string"}},
        ),
        (
            "lsp_symbols",
            "Get document symbols from the actual language server.",
            {"path": {"type": "string"}},
        ),
        (
            "lsp_definition",
            "Get definitions from the language server. line and character are zero-based; character uses UTF-16 units.",
            {
                "path": {"type": "string"},
                "line": {"type": "integer"},
                "character": {"type": "integer"},
            },
        ),
    ]:
        run.extra_tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": list(properties),
                        "additionalProperties": False,
                    },
                },
            }
        )
        run.tool_handlers[name] = functools.partial(
            _lsp_tool, client=client, operation=name
        )
    run.integration_info.append(
        {
            "protocol": "lsp",
            "server": config.get("name", "language-server"),
            "pid": client.process.pid,
            "tools": ["lsp_diagnostics", "lsp_symbols", "lsp_definition"],
            "capabilities": client.lsp_info,
        }
    )


def open_document(ctx, client, path):
    target = workspace_path(ctx.workspace, path)
    if target.suffix != ".py":
        raise ValueError("현재 연결은 Python 파일을 지원합니다.")
    text = ctx.read(path)
    uri = target.as_uri()
    # 이전 버전의 미소비 진단을 새 버전의 결과로 섞지 않는다.
    client.notifications = [
        item
        for item in client.notifications
        if item["message"].get("params", {}).get("uri") != uri
    ]
    version = client.documents.get(uri, 0) + 1
    if uri in client.documents:
        client.notify(
            ctx.run,
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            },
            ctx.parent,
        )
    else:
        client.notify(
            ctx.run,
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "python",
                    "version": version,
                    "text": text,
                }
            },
            ctx.parent,
        )
    client.documents[uri] = version
    ctx.run.emit(
        "lsp.document",
        "작업 파일을 언어 서버 문서로 동기화",
        "실제 파일 원문을 URI와 문서 버전으로 전달했습니다. 모델 messages에 넣는 과정과 별개입니다.",
        category="integrations",
        actor="lsp",
        parent=ctx.parent,
        output={
            "uri": uri,
            "version": version,
            "characters": len(text),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        },
    )
    return uri, version


def _lsp_tool(ctx, client, operation, path, line=0, character=0):
    uri, version = open_document(ctx, client, path)
    if operation == "lsp_diagnostics":
        message = client.wait_notification(
            ctx.run,
            lambda m: m.get("method") == "textDocument/publishDiagnostics"
            and m.get("params", {}).get("uri") == uri
            and m.get("params", {}).get("version", version) == version,
            ctx.parent,
        )
        diagnostics = message["params"].get("diagnostics", [])
        locations = [
            {
                "path": path,
                "line_1based": d["range"]["start"]["line"] + 1,
                "column_utf16_1based": d["range"]["start"]["character"] + 1,
                "message": d["message"],
            }
            for d in diagnostics
        ]
        ctx.run.emit(
            "lsp.coordinates",
            "언어 서버 좌표를 화면용 행 번호로 변환",
            "원본 range는 0부터 시작합니다. 모델과 화면에 전달할 1부터 시작하는 위치를 별도 필드로 계산하며 원본을 보존합니다.",
            category="integrations",
            actor="lsp",
            parent=ctx.parent,
            input={"diagnostics": diagnostics},
            output={"locations": locations},
        )
        return ctx.finish(
            {
                "path": path,
                "document_version": version,
                "server_reported_version": message["params"].get("version"),
                "diagnostic_count": len(diagnostics),
                "diagnostics": diagnostics,
                "locations": locations,
                "protocol": "lsp",
            }
        )
    if operation == "lsp_symbols":
        method = "textDocument/documentSymbol"
        params = {"textDocument": {"uri": uri}}
    else:
        if not 0 <= line < 100000 or not 0 <= character < 100000:
            raise ValueError("문서 위치 범위를 확인하세요.")
        method = "textDocument/definition"
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        }
    result = client.request(ctx.run, method, params, ctx.parent)
    return ctx.finish(
        {"path": path, "document_version": version, "result": result, "protocol": "lsp"}
    )


def prepare(run, workspace):
    if not (run.features.get("mcp") or run.features.get("lsp")):
        return
    raw = read_traced(
        run,
        workspace / "integrations.json",
        "context",
        reason="사용자가 연결을 켠 서버의 실행 설정을 읽습니다.",
    )
    config = json.loads(raw)
    if run.features.get("mcp"):
        prepare_mcp(run, config["mcp"])
    if run.features.get("lsp"):
        prepare_lsp(run, config["lsp"], workspace)
