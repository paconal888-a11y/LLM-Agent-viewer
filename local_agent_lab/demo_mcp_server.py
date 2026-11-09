"""실제 별도 프로세스로 동작하는 학습용 MCP 계산 서버. 2026-07-28 도구 부분집합."""

import json
import sys

VERSION = "2026-07-28"
INFO = {"name": "agent-lens-math", "version": "0.8.0"}
TOOL = {
    "name": "add",
    "description": "Add two integers in the separate local MCP process.",
    "inputSchema": {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
        "additionalProperties": False,
    },
}


def handle(request):
    params = request.get("params", {})
    meta = params.get("_meta", {})
    if meta.get("io.modelcontextprotocol/protocolVersion") != VERSION:
        return {
            "error": {
                "code": -32022,
                "message": "Unsupported protocol version",
                "data": {"supportedVersions": [VERSION]},
            }
        }
    if "io.modelcontextprotocol/clientCapabilities" not in meta:
        return {"error": {"code": -32602, "message": "Missing clientCapabilities"}}
    method = request.get("method")
    if method == "server/discover":
        result = {"supportedVersions": [VERSION], "capabilities": {"tools": {}}}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        args = params.get("arguments", {})
        if (
            params.get("name") != "add"
            or set(args) != {"a", "b"}
            or any(type(v) is not int or abs(v) > 10**12 for v in args.values())
        ):
            return {
                "error": {"code": -32602, "message": "add expects a and b integers"}
            }
        data = {"a": args["a"], "b": args["b"], "result": args["a"] + args["b"]}
        result = {
            "content": [{"type": "text", "text": json.dumps(data)}],
            "structuredContent": data,
            "isError": False,
        }
    else:
        return {"error": {"code": -32601, "message": "Method not found"}}
    return {
        "result": {
            "resultType": "complete",
            **result,
            "_meta": {"io.modelcontextprotocol/serverInfo": INFO},
        }
    }


def main():
    for raw in sys.stdin.buffer:
        request = None
        try:
            request = json.loads(raw)
            if "id" not in request:
                continue
            response = {"jsonrpc": "2.0", "id": request["id"], **handle(request)}
        except Exception as error:
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id") if isinstance(request, dict) else None,
                "error": {"code": -32700, "message": str(error)},
            }
        sys.stdout.buffer.write(
            json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            + b"\n"
        )
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
