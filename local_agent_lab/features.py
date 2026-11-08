"""실제로 실행 경로를 바꾸는 하네스 구성과 재현 조건."""

import hashlib
import json
import re
import math

DEFAULT_FEATURES = {
    "rules": True,
    "memory": True,
    "compaction": False,
    "skills": True,
    "tools": True,
    "commands": True,
    "writes": False,
    "mcp": False,
    "lsp": False,
    "feedback_recall": False,
    "planning": False,
}
BASELINE_FEATURES = {key: False for key in DEFAULT_FEATURES}


def normalize_requirements(values=None):
    """사용자가 명시한 완료 조건. 자연어 의도를 추측하거나 실행 권한을 늘리지 않는다."""
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 10:
        raise ValueError("완료 조건은 최대 10개 목록으로 지정하세요.")
    result = []
    for item in values:
        if not isinstance(item, dict) or set(item) - {"tool", "arguments", "result"}:
            raise ValueError("완료 조건에는 tool과 arguments를 사용하세요.")
        name = item.get("tool")
        args = item.get("arguments", {})
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", name)
            or not isinstance(args, dict)
            or len(args) > 8
        ):
            raise ValueError("완료 조건의 도구 이름과 인자를 확인하세요.")
        if any(
            not isinstance(key, str)
            or type(value) not in (str, int, float, bool)
            or len(str(value)) > 1000
            or isinstance(value, float)
            and not math.isfinite(value)
            for key, value in args.items()
        ):
            raise ValueError(
                "완료 조건의 인자는 짧은 문자열·유한한 숫자·불리언만 지원합니다."
            )
        # JSON 표기가 137.0이어도 정수 인자와 같은 값이다. integer 도구 인자와 영구히 어긋나지 않게 맞춘다.
        args = {
            key: int(value)
            if isinstance(value, float) and value.is_integer()
            else value
            for key, value in args.items()
        }
        expected = item.get("result")
        if expected is not None and (
            not isinstance(expected, dict)
            or len(json.dumps(expected, allow_nan=False)) > 8000
        ):
            raise ValueError("예상 결과는 작은 JSON 객체로 지정하세요.")
        result.append(
            {
                "tool": name,
                "arguments": dict(args),
                **({"result": expected} if expected is not None else {}),
            }
        )
    return result


def normalize_features(values=None):
    if values is None:
        return dict(DEFAULT_FEATURES)
    if not isinstance(values, dict) or set(values) - set(DEFAULT_FEATURES):
        raise ValueError("지원하지 않는 하네스 구성입니다.")
    if any(type(value) is not bool for value in values.values()):
        raise ValueError("하네스 구성 값은 true 또는 false여야 합니다.")
    result = {**DEFAULT_FEATURES, **values}
    if not result["memory"]:
        result["feedback_recall"] = False
        result["compaction"] = False
    if not result["tools"]:
        result["skills"] = False
        result["writes"] = False
        result["mcp"] = False
        result["lsp"] = False
        result["planning"] = False
    return result


def workspace_signature(workspace):
    files = []
    for path in sorted(workspace.rglob("*")):
        if path.is_file() and path.resolve().is_relative_to(workspace.resolve()):
            if len(files) >= 200 or path.stat().st_size > 65536:
                continue
            files.append(
                {
                    "path": path.relative_to(workspace).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    encoded = json.dumps(files, sort_keys=True).encode("utf-8")
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "files": files}


def run_metrics(snapshot):
    totals = snapshot.get("totals") or {}
    # 공개 도구 수는 답변용 요청 기준이다. 요약(compaction) 요청은 도구가 없어 먼저 오면 0으로 보였다.
    answer_requests = [
        e["input"]
        for e in snapshot["events"]
        if e["kind"] == "model.request" and e.get("model_purpose", "answer") == "answer"
    ]
    requests = answer_requests or [
        e["input"] for e in snapshot["events"] if e["kind"] == "model.request"
    ]
    names = (
        [t["function"]["name"] for t in requests[0].get("tools", [])]
        if requests
        else []
    )
    responses = [
        event["output"] or {}
        for event in snapshot["events"]
        if event["kind"] == "model.response"
    ]
    timings = [response.get("timings", {}) for response in responses]
    cached = [
        (response.get("usage", {}).get("prompt_tokens_details") or {}).get(
            "cached_tokens", response.get("timings", {}).get("cache_n")
        )
        for response in responses
    ]
    fresh = [timing.get("prompt_n") for timing in timings]
    complete_sum = (
        lambda values: sum(values)
        if values and all(isinstance(value, (int, float)) for value in values)
        else None
    )
    return {
        "active_seconds": snapshot.get(
            "active_seconds", snapshot.get("elapsed_seconds", 0)
        ),
        "elapsed_seconds": snapshot.get("elapsed_seconds", 0),
        "paused_seconds": snapshot.get("paused_seconds", 0),
        # 실행기가 usage를 보고하지 않은 호출이 있으면 None(미수집). 0은 실제 보고값일 때만 쓴다.
        "prompt_tokens": totals.get(
            "prompt_tokens", snapshot.get("usage", {}).get("prompt_tokens", 0)
        ),
        "completion_tokens": totals.get(
            "completion_tokens", snapshot.get("usage", {}).get("completion_tokens", 0)
        ),
        "usage_missing_calls": totals.get("usage_missing_calls", 0),
        "model_calls": totals.get("model_calls", snapshot.get("model_calls", 0)),
        "tool_calls": totals.get("tool_calls", snapshot.get("tool_calls", 0)),
        "engine_seconds": round(
            sum(t.get("prompt_ms", 0) + t.get("predicted_ms", 0) for t in timings)
            / 1000,
            3,
        ),
        "cached_tokens": complete_sum(cached),
        "fresh_prompt_tokens": complete_sum(fresh),
        "prefill_ms": complete_sum([timing.get("prompt_ms") for timing in timings]),
        "decode_ms": complete_sum([timing.get("predicted_ms") for timing in timings]),
        "forward_tokens": complete_sum(
            [timing.get("forward_tokens") for timing in timings]
        ),
        "tool_definitions": len(names),
        "tool_names": names,
        "subagents": len(snapshot.get("agents", [])),
        "model_queue_ms": round(
            sum(
                (e.get("output") or {}).get("wait_ms", 0)
                for e in snapshot["events"]
                if e["kind"] == "agent.compute"
            ),
            3,
        ),
        "mcp_messages": sum(
            e["kind"] in {"mcp.send", "mcp.receive"} for e in snapshot["events"]
        ),
        "lsp_messages": sum(
            e["kind"] in {"lsp.send", "lsp.receive"} for e in snapshot["events"]
        ),
        "events": len(snapshot["events"]),
    }
