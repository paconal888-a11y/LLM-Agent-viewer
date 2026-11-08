"""A small, inspectable tool loop with no third-party dependencies."""

import ipaddress
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request


class RuntimeFailure(Exception):
    """A runtime connection, response, or smoke-test failure."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_failure(error):
    """서버가 본문에 적은 이유까지 전달한다. 컨텍스트 초과 같은 원인을 구분하는 근거가 된다."""
    try:
        detail = error.read(2000).decode("utf-8", "replace").strip()
    except OSError:
        detail = ""
    return RuntimeFailure(
        f"런타임이 HTTP {error.code}을 반환했습니다." + (" " + detail if detail else "")
    )


class LocalRuntime:
    def __init__(self, base_url="http://127.0.0.1:1234/v1", timeout=120.0):
        try:
            parsed = urllib.parse.urlsplit(base_url)
            parsed.port
        except ValueError as exc:
            raise RuntimeFailure(
                "런타임 URL 또는 포트 형식이 올바르지 않습니다."
            ) from exc
        try:
            is_loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            is_loopback = parsed.hostname == "localhost"
        if (
            parsed.scheme not in {"http", "https"}
            or not is_loopback
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeFailure("로컬 루프백 HTTP(S) 주소만 사용할 수 있습니다.")
        if not math.isfinite(timeout) or timeout <= 0:
            raise RuntimeFailure("timeout은 유한한 양수여야 합니다.")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Local inference traffic does not need a system-configured HTTP proxy.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def request(self, route, payload=None):
        value = self.observe(route, payload)
        if not isinstance(value, dict):
            raise RuntimeFailure("런타임 응답이 JSON 객체가 아닙니다.")
        return value

    def observe(self, route, payload=None):
        """진단 API의 JSON 객체 또는 배열. request()의 객체 검사는 유지한다."""
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + route,
            data=body,
            headers={"Content-Type": "application/json"},
            method="GET" if payload is None else "POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                self.last_http = {
                    "url": request.full_url,
                    "method": request.method,
                    "status": response.status,
                    "content_type": response.headers.get("Content-Type"),
                    "declared_content_length": response.headers.get("Content-Length"),
                }
                value = json.load(response)
        except urllib.error.HTTPError as exc:
            raise http_failure(exc) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RuntimeFailure(f"런타임 연결 또는 JSON 응답 실패: {exc}") from exc
        if not isinstance(value, (dict, list)):
            raise RuntimeFailure("런타임 응답이 JSON 객체 또는 배열이 아닙니다.")
        return value

    def stream_json(self, route, payload):
        """실제 SSE 프레임을 원문·수신 시각과 함께 전달한다. 모델을 재호출하지 않는다."""
        request = urllib.request.Request(
            self.base_url + route,
            json.dumps(payload).encode("utf-8"),
            {"Content-Type": "application/json", "Accept": "text/event-stream"},
        )
        started = time.perf_counter()
        data = []
        size = 0
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if "text/event-stream" not in response.headers.get("Content-Type", ""):
                    raise RuntimeFailure("스트리밍 요청에 SSE 응답이 오지 않았습니다.")
                yield {
                    "opened": True,
                    "http_status": response.status,
                    "content_type": response.headers.get("Content-Type"),
                    "arrival_ms": round((time.perf_counter() - started) * 1000, 3),
                }
                while True:
                    line = response.readline(2097153)
                    if len(line) > 2097152:
                        raise RuntimeFailure("SSE 줄의 크기 제한을 초과했습니다.")
                    if not line:
                        raise RuntimeFailure("[DONE] 전에 SSE 연결이 끝났습니다.")
                    stripped = line.rstrip(b"\r\n")
                    # frame_bytes는 data 줄과 종결 빈 줄만 센다. 주석(: ping)·event 줄은 프레임 크기에 넣지 않는다.
                    if not stripped or stripped.startswith(b"data:"):
                        size += len(line)
                    if not stripped:
                        if not data:
                            size = 0
                            continue
                        raw = b"\n".join(data).decode("utf-8")
                        elapsed = round((time.perf_counter() - started) * 1000, 3)
                        if raw == "[DONE]":
                            yield {
                                "done": True,
                                "raw": raw,
                                "arrival_ms": elapsed,
                                "frame_bytes": size,
                            }
                            return
                        value = json.loads(raw)
                        if not isinstance(value, dict) or "error" in value:
                            raise RuntimeFailure(
                                "SSE 프레임에 유효한 응답 대신 오류가 있습니다: "
                                + str(value)
                            )
                        yield {
                            "data": value,
                            "raw": raw,
                            "arrival_ms": elapsed,
                            "frame_bytes": size,
                        }
                        data, size = [], 0
                    elif stripped.startswith(b"data:"):
                        data.append(stripped[5:].lstrip(b" "))
                        if sum(map(len, data)) > 2097152:
                            raise RuntimeFailure(
                                "SSE 프레임의 크기 제한을 초과했습니다."
                            )
                    # SSE 주석·ping·event 이름은 모델 출력으로 취급하지 않는다.
        except urllib.error.HTTPError as exc:
            raise http_failure(exc) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RuntimeFailure(f"SSE 수신 또는 JSON 해석 실패: {exc}") from exc

    def models(self):
        data = self.request("/models").get("data")
        if not isinstance(data, list):
            raise RuntimeFailure("/models 응답에 data 목록이 없습니다.")
        return [
            item["id"]
            for item in data
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]


TOOL = {
    "name": "add_numbers",
    "description": "Return the exact sum of two integer operands.",
    "parameters": {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
        "additionalProperties": False,
    },
}
PROMPT = (
    "Call add_numbers with a=17 and b=25 exactly once. "
    "After receiving its result, reply with only that result. Do not calculate it yourself."
)


def execute_smoke_tool(name, arguments):
    if name != "add_numbers":
        raise RuntimeFailure(f"허용되지 않은 도구입니다: {name}")
    try:
        args = json.loads(arguments)
    except (TypeError, ValueError) as exc:
        raise RuntimeFailure("도구 인자가 유효한 JSON이 아닙니다.") from exc
    if (
        not isinstance(args, dict)
        or set(args) != {"a", "b"}
        or type(args["a"]) is not int
        or type(args["b"]) is not int
        or args["a"] != 17
        or args["b"] != 25
    ):
        raise RuntimeFailure("모델이 요청한 도구 인자가 지정한 17, 25와 다릅니다.")
    return args["a"] + args["b"]


def smoke(runtime, model, api="chat"):
    """Verify one real tool request, execution, and final answer on the selected API."""
    if api not in {"chat", "responses"}:
        raise RuntimeFailure("지원 API는 chat 또는 responses입니다.")
    started = time.perf_counter()
    if api == "chat":
        route = "/chat/completions"
        messages = [{"role": "user", "content": PROMPT}]
        first = runtime.request(
            route,
            {
                "model": model,
                "messages": messages,
                "stream": False,
                "max_tokens": 1024,
                "tools": [{"type": "function", "function": TOOL}],
            },
        )
        try:
            message = first["choices"][0]["message"]
            calls = message.get("tool_calls", [])
            if len(calls) != 1:
                raise RuntimeFailure("정확히 한 번의 도구 호출이 필요합니다.")
            call = calls[0]
            if call.get("type") != "function" or not call.get("id"):
                raise RuntimeFailure("유효한 function 도구 호출 ID가 없습니다.")
            result = execute_smoke_tool(
                call["function"]["name"], call["function"]["arguments"]
            )
            messages += [
                message,
                {"role": "tool", "tool_call_id": call["id"], "content": str(result)},
            ]
            second = runtime.request(
                route,
                {
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "max_tokens": 1024,
                },
            )
            final_message = second["choices"][0]["message"]
            if final_message.get("tool_calls"):
                raise RuntimeFailure("도구 결과 이후 추가 도구 호출이 발생했습니다.")
            final = final_message.get("content")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise RuntimeFailure(
                "Chat Completions 응답 구조가 올바르지 않습니다."
            ) from exc
    else:
        route = "/responses"
        first = runtime.request(
            route,
            {
                "model": model,
                "input": PROMPT,
                "stream": False,
                "max_output_tokens": 1024,
                "tools": [{"type": "function", **TOOL}],
            },
        )
        try:
            output = first["output"]
            calls = [item for item in output if item.get("type") == "function_call"]
            if len(calls) != 1:
                raise RuntimeFailure("정확히 한 번의 도구 호출이 필요합니다.")
            call = calls[0]
            if not call.get("call_id") or not first.get("id"):
                raise RuntimeFailure("Responses 응답 ID 또는 도구 호출 ID가 없습니다.")
            result = execute_smoke_tool(call["name"], call["arguments"])
            second = runtime.request(
                route,
                {
                    "model": model,
                    "previous_response_id": first["id"],
                    "input": [
                        {
                            "type": "function_call_output",
                            "call_id": call["call_id"],
                            "output": str(result),
                        }
                    ],
                    "stream": False,
                    "max_output_tokens": 1024,
                },
            )
            if any(item.get("type") == "function_call" for item in second["output"]):
                raise RuntimeFailure("도구 결과 이후 추가 도구 호출이 발생했습니다.")
            final = "".join(
                part.get("text", "")
                for item in second["output"]
                if item.get("type") == "message"
                for part in item.get("content", [])
                if part.get("type") == "output_text"
            )
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise RuntimeFailure("Responses 응답 구조가 올바르지 않습니다.") from exc
    if not isinstance(final, str) or re.fullmatch(r"\s*42\s*", final) is None:
        raise RuntimeFailure("최종 답변이 지정한 도구 결과 42와 일치하지 않습니다.")
    return {
        "status": "passed",
        "api": api,
        "model": model,
        "tool": "add_numbers",
        "arguments": {"a": 17, "b": 25},
        "tool_result": result,
        "final_response": final.strip(),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "scope": "One tool-call round trip; not an agent quality or GPU benchmark.",
    }
