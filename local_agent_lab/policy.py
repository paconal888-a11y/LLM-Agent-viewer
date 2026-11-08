"""모델의 요청과 별개로 하네스가 강제하는 검사."""

import json
import math
import hashlib
import posixpath
from .files import workspace_path


REPEATED_ANSWER_LIMIT = 3


def repeated_rejection(run, attempts, message, missing, outstanding):
    """같은 미검증 답변·미충족 조건·도구 호출 수의 연속 반복을 검사한다."""
    signature = hashlib.sha256(
        json.dumps(
            {
                "message": message,
                "missing": missing,
                "outstanding": sorted(outstanding),
                "tool_calls": run.tool_calls,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if attempts and attempts[-1]["signature"] != signature:
        attempts.clear()
    response = next(
        (
            event
            for event in reversed(run.events)
            if event["kind"] == "model.response"
            and event.get("agent_id", run.id) == run.id
        ),
        None,
    )
    attempts.append(
        {
            "signature": signature,
            "response_event_id": response["id"] if response else None,
        }
    )
    blocked = len(attempts) >= REPEATED_ANSWER_LIMIT
    run.emit(
        "validation.repeat",
        "미검증 답변 반복 확인",
        "답변·미충족 조건이 같고 새 도구 호출이 없는 연속 반복을 셉니다. 의미가 비슷한 답변이나 모든 종류의 반복을 탐지하는 기능은 아닙니다.",
        category="output",
        actor="validator",
        input={
            "candidate_message": message,
            "missing": missing,
            "unchecked_files": sorted(outstanding),
            "tool_calls": run.tool_calls,
        },
        output={
            "streak": len(attempts),
            "limit": REPEATED_ANSWER_LIMIT,
            "blocked": blocked,
            "signature": signature,
            "response_event_ids": [item["response_event_id"] for item in attempts],
        },
        status="error" if blocked else "ok",
    )
    return blocked


def validate_tool_call(run, workspace, allowed, call, parent):
    name = call.get("function", {}).get("name")
    args = json.loads(call.get("function", {}).get("arguments", ""))
    selected = next(
        (tool for tool in allowed if tool["function"]["name"] == name), None
    )
    if selected is None:
        raise ValueError("현재 구성에서 공개하지 않은 도구입니다.")
    if (
        name.startswith("mcp_")
        and not run.features.get("writes")
        and name not in getattr(run, "mcp_read_only_tools", set())
    ):
        # 공개 목록(tools_for)과 별개로 실행 경계에서 다시 검사한다. 계획 모드는 쓰기 권한이 꺼진
        # 실행의 한 경우이며, 서버의 readOnlyHint가 아니라 호스트의 read_only_tools가 기준이다.
        raise ValueError(
            "계획 모드에서는 호스트가 읽기·계산 전용으로 분류한 MCP 도구만 실행합니다."
            if run.mode == "plan"
            else "쓰기 권한이 꺼진 실행에서는 호스트가 읽기·계산 전용으로 분류한 MCP 도구만 실행합니다."
        )
    schema = selected["function"]["parameters"]
    if not isinstance(args, dict):
        raise ValueError("인자는 JSON 객체여야 합니다.")
    properties = schema.get("properties", {})
    if not set(schema.get("required", [])) <= set(args):
        raise ValueError("필수 인자가 빠졌습니다.")
    if set(args) - set(properties):
        raise ValueError("등록되지 않은 추가 인자가 있습니다.")
    for key, value in args.items():
        kind = properties[key].get("type")
        if kind == "string":
            valid = isinstance(value, str)
        elif kind == "integer":
            valid = type(value) is int and abs(value) <= 10**12
        elif kind == "number":
            valid = (
                type(value) in (int, float)
                and math.isfinite(value)
                and abs(value) <= 10**12
            )
        elif kind == "boolean":
            valid = type(value) is bool
        else:
            raise ValueError(
                "이 검증기가 지원하는 인자 형식: string, integer, number, boolean"
            )
        if not valid:
            raise ValueError("인자 형식이 올바르지 않습니다: " + key)
    path = workspace_path(workspace, args["path"]) if "path" in args else None
    if name == "write_file" and (
        path.name.casefold()
        in {
            "agents.md",
            "agent.md",
            "resume.md",
            "integrations.json",
            "agents.json",
            "memory.json",
        }
        or any(
            part.casefold() in {"skills", "commands"}
            for part in path.relative_to(workspace).parts
        )
    ):
        raise ValueError("에이전트가 규칙·스킬·초기 이력을 수정할 수 없습니다.")
    run.emit(
        "tool.validate",
        "도구 인자·경로 검사 통과",
        "공개 도구, 필수 키, 형식, 작업 공간 경로를 코드로 검사했습니다.",
        category="tools",
        actor="validator",
        parent=parent,
        input={"name": name, "arguments": args},
        output={"allowed": True, "resolved_path": str(path) if path else None},
    )
    return name, args


def final_gate(run, dirty, checked, finish_reason):
    outstanding = dirty - checked
    run.emit(
        "validation.final",
        "수정 파일의 검사 상태 확인",
        "수정한 파일마다 통과한 검사 결과가 있어야 완료합니다.",
        actor="validator",
        category="output",
        input={"changed_files": sorted(dirty), "checked_files": sorted(checked)},
        output={
            "passed": not outstanding,
            "unchecked_files": sorted(outstanding),
            "finish_reason": finish_reason,
        },
        status="ok" if not outstanding else "error",
    )
    return outstanding


def contains_value(actual, expected):
    """예상 객체의 키·값을 실제 결과에서 확인한다. 숫자 1과 True도 구분한다."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and contains_value(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(contains_value(a, b) for a, b in zip(actual, expected))
        )
    return type(actual) is type(expected) and actual == expected


PATH_TOOLS = {
    "read_file",
    "write_file",
    "check_file",
    "list_files",
    "lsp_diagnostics",
    "lsp_symbols",
    "lsp_definition",
}


def same_argument(tool, key, actual, expected):
    if (
        tool in PATH_TOOLS
        and key == "path"
        and isinstance(actual, str)
        and isinstance(expected, str)
    ):

        def normalized(value):
            value = value.replace("\\", "/")
            value = posixpath.normpath(value)
            if value.startswith(("/", "../")) or value == ".." or ":" in value:
                return None
            return value

        left, right = normalized(actual), normalized(expected)
        return left is not None and right is not None and left == right
    # 인자는 전체 값이 같아야 한다. 결과 객체의 부분 포함 검사와 구분한다.
    return (
        type(actual) is type(expected)
        and actual == expected
        and contains_value(actual, expected)
    )


def requirement_gate(run, successful):
    """지정한 도구와 인자가 실제 성공 결과에 있는지 검사한다. 답변 문장은 증거로 쓰지 않는다."""
    missing = []
    evidence = []
    for required in run.requirements:
        match = next(
            (
                item
                for item in reversed(successful)
                if item["name"] == required["tool"]
                and all(
                    same_argument(
                        required["tool"], key, item["arguments"].get(key), value
                    )
                    for key, value in required["arguments"].items()
                )
            ),
            None,
        )
        if match and not contains_value(
            match.get("result", {}), required.get("result", {})
        ):
            match = None
        if match:
            evidence.append(
                {"requirement": required, "tool_result_event": match["event_id"]}
            )
        else:
            missing.append(required)
    if run.requirements:
        run.emit(
            "validation.requirements",
            "지정한 작업의 실제 실행 여부 검사",
            "사용자가 명시한 도구·인자와 성공한 실제 결과를 대조합니다. 자연어 요청의 의미를 자동으로 완벽하게 판단하는 검사는 아닙니다.",
            category="output",
            actor="validator",
            input={"requirements": run.requirements},
            output={"passed": not missing, "missing": missing, "evidence": evidence},
            status="error" if missing else "ok",
        )
    return missing
