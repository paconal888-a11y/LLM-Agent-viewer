"""실행 기록만으로 실패·완료의 근거를 층별로 정리한다. 모델 내부의 이유는 추정하지 않는다.

층을 아래 순서로 살핀다. 앞 층에 문제가 있으면 뒤 층의 관측은 믿기 어렵다. 예를 들어 필수 도구가
공개되지 않았는데 "모델이 도구를 안 썼다"고 말하는 것은 원인을 잘못 짚은 것이다.
"""

import json
import re

ORDER = ("runtime", "harness", "information", "model")
LABELS = {
    "runtime": "실행기·설정",
    "harness": "도구·하네스",
    "information": "모델 입력",
    "model": "모델의 선택",
}
NEXT_STEPS = {
    "runtime": "실행기 로그, 출력 토큰 한도, 컨텍스트 크기를 먼저 확인하세요. 이 층이 해결돼야 지시문이나 도구를 바꾼 비교가 의미 있습니다.",
    "harness": "도구 공개 스위치·allow_tools·도구 함수 구현 중 하나만 바꿔 다시 실행하세요.",
    "information": "규칙·완료 조건·대화 이력 스위치와 context.py의 조립을 확인하세요. 입력에 없는 내용은 모델이 따를 수 없습니다.",
    "model": "같은 조건을 반복 측정해 재현되는지 먼저 보세요. 재현되면 지시문·도구 설명·스킬 중 하나만 바꿔 비교하고, 그래도 차이가 없으면 이 작업이 이 모델의 능력 밖일 가능성을 검토하세요.",
}
NOTE = (
    "기록에서 센 사실로 살펴볼 층을 좁힌 결과입니다. 모델이 왜 그렇게 행동했는지, 고치면 해결되는지는 "
    "증명하지 않습니다. 한 번의 실행으로 결론 내리지 말고 같은 조건을 반복 측정해 재현되는지 확인하세요."
)


def _check(layer, check_id, question, result, detail, event=None):
    return {
        "id": check_id,
        "layer": layer,
        "question": question,
        "result": result,
        "detail": detail,
        "event_id": event["id"] if event else None,
    }


def _tool_names(request):
    return [
        (tool.get("function") or {}).get("name")
        for tool in (request.get("input") or {}).get("tools") or []
    ]


def _messages(request):
    return (request.get("input") or {}).get("messages") or []


def _finish_reason(event):
    choices = (event.get("output") or {}).get("choices") or [{}]
    return choices[0].get("finish_reason")


def _in_next_request(requests, event):
    """event가 추가한 메시지가 그 다음 모델 요청에 실제로 들어갔는지. 다음 요청이 없으면 None."""
    following = next((r for r in requests if r["id"] > event["id"]), None)
    if following is None:
        return None
    return (event.get("output") or {}).get("appended_message") in _messages(following)


def _last(events, kind):
    return next((e for e in reversed(events) if e["kind"] == kind), None)


def _runtime_checks(requests, responses, events, error_text, error_event, stopped):
    checks = []
    if responses:
        checks.append(
            _check("runtime", "model_replied", "실행기가 응답을 돌려줬는가", "pass",
                   f"모델 응답 {len(responses)}개", responses[-1])
        )
    elif requests and not stopped:
        checks.append(
            _check("runtime", "model_replied", "실행기가 응답을 돌려줬는가", "fail",
                   error_text or "모델 요청 뒤 응답 기록이 없습니다.", error_event or requests[-1])
        )
    truncated = next((r for r in responses if _finish_reason(r) == "length"), None)
    if truncated:
        limit = (requests[-1].get("input") or {}).get("max_tokens") if requests else None
        checks.append(
            _check("runtime", "output_limit", "출력이 토큰 한도 안에서 끝났는가", "fail",
                   f"finish_reason=length · 출력 한도 {limit}. 실행 설정에서 정한 값입니다.", truncated)
        )
    elif responses:
        checks.append(
            _check("runtime", "output_limit", "출력이 토큰 한도 안에서 끝났는가", "pass",
                   "finish_reason=length 없음", responses[-1])
        )
    if re.search(r"context size|exceed_context|n_ctx", error_text, re.I):
        checks.append(
            _check("runtime", "context_window", "입력이 컨텍스트 창 안에 들어갔는가", "fail",
                   error_text, error_event)
        )
    else:
        window = None
        for event in events:
            if event["kind"] == "runtime.slots":
                for slot in (event.get("output") or {}).get("slots") or []:
                    if isinstance(slot, dict) and isinstance(slot.get("n_ctx"), int):
                        window = slot["n_ctx"]
        used = [
            ((r.get("output") or {}).get("usage") or {}).get("prompt_tokens")
            for r in responses
        ]
        used = [value for value in used if isinstance(value, int)]
        if window and used:
            peak = max(used)
            checks.append(
                _check("runtime", "context_window", "입력이 컨텍스트 창 안에 들어갔는가",
                       "warn" if peak >= window * 0.9 else "pass",
                       f"가장 긴 입력 {peak:,}토큰 / 창 {window:,}토큰")
            )
    return checks


def _harness_checks(requests, events, requirements, error_text, error_event):
    checks = []
    if requirements and requests:
        exposed = _tool_names(requests[-1])
        hidden = sorted({r["tool"] for r in requirements if r["tool"] not in exposed})
        checks.append(
            _check("harness", "required_tools_exposed", "필수 도구가 모델에 공개됐는가",
                   "fail" if hidden else "pass",
                   "공개되지 않음: " + ", ".join(hidden) if hidden
                   else f"공개 도구 {len(exposed)}개에 필수 도구 포함", requests[-1])
        )
    validated = {e.get("parent") for e in events if e["kind"] == "tool.validate"}
    failed = [e for e in events if e["kind"] == "tool.error" and e.get("parent") in validated]
    dispatched = [e for e in events if e["kind"] == "tool.dispatch"]
    if dispatched:
        checks.append(
            _check("harness", "tool_execution", "검사를 통과한 도구가 실행에 성공했는가",
                   "fail" if failed else "pass",
                   (failed[-1].get("output") or {}).get("error", "") if failed
                   else f"도구 요청 {len(dispatched)}회 중 실행 실패 없음",
                   failed[-1] if failed else dispatched[-1])
        )
    if re.search(r"모델 호출 \d+회 제한", error_text):
        # 예산 소진은 실행을 끝낸 장치이지 그 자체로 원인이 아니다. 초점을 빼앗지 않도록 주의로만 표시한다.
        checks.append(
            _check("harness", "call_budget", "호출 예산 안에 끝났는가", "warn",
                   error_text + " 예산을 늘리기 전에 모델이 무엇을 반복했는지 확인하세요.", error_event)
        )
    if error_event and (error_event.get("output") or {}).get("type"):
        output = error_event["output"]
        checks.append(
            _check("harness", "unexpected_exception", "하네스 코드가 예외 없이 동작했는가", "fail",
                   f"{output.get('type')} · {output.get('file', '')}:{output.get('line', '')}", error_event)
        )
    for event in events:
        output = event.get("output") or {}
        if event["kind"] == "tool.result" and output.get("all_completed") is False:
            done = sum(1 for r in output.get("results") or [] if r.get("status") == "completed")
            checks.append(
                _check("harness", "subagents", "위임한 하위 에이전트가 모두 완료했는가", "warn",
                       f"완료 {done}/{len(output.get('results') or [])}. 주 에이전트는 하위 실패가 있어도 완료할 수 있습니다.", event)
            )
    return checks


def _information_checks(requests, events, requirements):
    checks = []
    first = requests[0] if requests else None
    planned = _last(events, "context.requirements")
    if requirements and first:
        included = planned and (planned.get("output") or {}).get("appended_message") in _messages(first)
        checks.append(
            _check("information", "requirements_in_input", "완료 조건이 모델 입력에 들어갔는가",
                   "pass" if included else "fail",
                   "첫 요청 messages에 완료 조건 메시지 있음" if included else "첫 요청 messages에서 찾지 못했습니다.",
                   planned or first)
        )
    wrap = _last(events, "rules.wrap")
    if wrap and first:
        block = (wrap.get("output") or {}).get("instruction_block") or ""
        included = bool(block) and any(
            block in (message.get("content") or "")
            for message in _messages(first)
            if isinstance(message.get("content"), str)
        )
        checks.append(
            _check("information", "rules_in_input", "프로젝트 규칙이 모델 입력에 들어갔는가",
                   "pass" if included else "fail",
                   "첫 요청 messages에 규칙 블록 있음" if included else "규칙을 읽었지만 첫 요청에서 찾지 못했습니다.", wrap)
        )
    elif _last(events, "rules.disabled"):
        checks.append(
            _check("information", "rules_in_input", "프로젝트 규칙이 모델 입력에 들어갔는가", "warn",
                   "규칙 스위치가 꺼져 있어 입력에 없습니다.", _last(events, "rules.disabled"))
        )
    appended = [e for e in events if e["kind"] in {"context.append", "context.repair"}]
    outcomes = [(e, _in_next_request(requests, e)) for e in appended]
    known = [(e, included) for e, included in outcomes if included is not None]
    if known:
        lost = [e for e, included in known if not included]
        checks.append(
            _check("information", "results_in_next_input", "도구 결과·보완 지시가 다음 입력에 들어갔는가",
                   "fail" if lost else "pass",
                   f"{len(lost)}개 누락" if lost else f"{len(known)}개 모두 다음 요청에 포함",
                   lost[0] if lost else known[-1][0])
        )
    selected = _last(events, "memory.select")
    omitted = (selected.get("output") or {}).get("omitted_turns") if selected else None
    count = len(omitted) if isinstance(omitted, list) else omitted
    if isinstance(count, int) and count > 0:
        checks.append(
            _check("information", "history_omitted", "이전 대화가 모두 입력에 들어갔는가", "warn",
                   f"완료 대화 {count}개가 입력에서 제외됐습니다.", selected)
        )
    return checks


def _arguments(event):
    raw = ((event.get("input") or {}).get("function") or {}).get("arguments")
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _model_checks(requests, events, requirements):
    checks = []
    dispatched = [e for e in events if e["kind"] == "tool.dispatch"]
    validated = {e.get("parent") for e in events if e["kind"] == "tool.validate"}
    rejected = [e for e in events if e["kind"] == "tool.error" and e.get("parent") not in validated]
    if dispatched:
        checks.append(
            _check("model", "tool_request_valid", "모델의 도구 요청이 인자·경로 검사를 통과했는가",
                   "fail" if rejected else "pass",
                   (rejected[-1].get("output") or {}).get("error", "") if rejected
                   else f"요청 {len(dispatched)}회 모두 통과",
                   rejected[-1] if rejected else dispatched[-1])
        )
    calls = {}
    for event in dispatched:
        name = ((event.get("input") or {}).get("function") or {}).get("name")
        calls.setdefault(name, []).append(event)
    if requirements and requests:
        exposed = _tool_names(requests[-1])
        uncalled = sorted(
            {r["tool"] for r in requirements if r["tool"] in exposed and r["tool"] not in calls}
        )
        checks.append(
            _check("model", "required_tool_called", "모델이 필수 도구를 호출했는가",
                   "fail" if uncalled else "pass",
                   "호출 0회: " + ", ".join(uncalled) if uncalled else "필수 도구 호출 기록 있음",
                   requests[-1])
        )
    gate = _last(events, "validation.requirements")
    if gate:
        missing = (gate.get("output") or {}).get("missing") or []
        detail = "남은 조건 없음"
        if missing:
            first = missing[0]
            seen = [_arguments(e) for e in calls.get(first.get("tool"), [])]
            detail = f"미충족 {len(missing)}개 · 조건 {json.dumps(first, ensure_ascii=False)}"
            if seen:
                detail += f" · 실제 인자 {json.dumps(seen[-1], ensure_ascii=False)}"
        checks.append(
            _check("model", "requirements_met", "실제 도구 결과가 완료 조건과 맞았는가",
                   "fail" if missing else "pass", detail, gate)
        )
    final = _last(events, "validation.final")
    if final and (final.get("input") or {}).get("changed_files"):
        unchecked = (final.get("output") or {}).get("unchecked_files") or []
        checks.append(
            _check("model", "files_checked", "수정한 파일을 검사했는가",
                   "fail" if unchecked else "pass",
                   "미검사: " + ", ".join(unchecked) if unchecked else "수정 파일 모두 검사 통과", final)
        )
    repairs = [e for e in events if e["kind"] == "context.repair"]
    if repairs:
        checks.append(
            _check("model", "claimed_early", "조건을 채우기 전에 완료를 주장하지 않았는가", "warn",
                   f"하네스가 답변을 {len(repairs)}회 돌려보냈습니다.", repairs[-1])
        )
    repeated = next(
        (e for e in reversed(events)
         if e["kind"] == "validation.repeat" and (e.get("output") or {}).get("blocked")),
        None,
    )
    if repeated:
        output = repeated.get("output") or {}
        checks.append(
            _check("model", "repeat_guard", "같은 미검증 답변을 반복하지 않았는가", "fail",
                   f"{output.get('streak')}/{output.get('limit')}회 반복해 하네스가 중단했습니다.", repeated)
        )
    return checks


def _layer_status(checks):
    results = {check["result"] for check in checks}
    if "fail" in results:
        return "issue"
    if "warn" in results:
        return "attention"
    return "clear" if "pass" in results else "unknown"


def _verification(events, requirements, status):
    by_code = []
    gate = _last(events, "validation.requirements")
    if status == "completed" and gate and (gate.get("output") or {}).get("passed"):
        with_result = sum("result" in item for item in requirements)
        by_code.append(
            f"완료 조건 {len(requirements)}개를 실제 도구 결과와 대조 "
            f"(결과 값까지 {with_result}개, 나머지는 도구·인자만)"
        )
    final = _last(events, "validation.final")
    changed = (final.get("input") or {}).get("changed_files") if final else None
    if status == "completed" and changed and (final.get("output") or {}).get("passed"):
        by_code.append(f"수정한 파일 {len(changed)}개의 검사 통과")
    not_checked = ["최종 답변 문장의 사실 여부"]
    if status == "completed" and not requirements:
        not_checked.insert(0, "요청한 작업을 실제로 수행했는지 (이 실행에는 완료 조건이 없습니다)")
    return {"by_code": by_code, "not_checked": not_checked}


def diagnose(snapshot):
    owner = (snapshot.get("sample") or {}).get("source_run_id", snapshot.get("id"))
    events = [
        event
        for event in snapshot.get("events", [])
        if event.get("agent_id", owner) == owner
    ]
    status = snapshot.get("status")
    requirements = snapshot.get("requirements") or []
    answer_events = [e for e in events if e.get("model_purpose", "answer") == "answer"]
    requests = [
        e for e in answer_events
        if e["kind"] == "model.request" and isinstance((e.get("input") or {}).get("messages"), list)
    ]
    responses = [e for e in answer_events if e["kind"] == "model.response"]
    error_event = _last(events, "run.error")
    error_text = str((error_event.get("output") or {}).get("error", "")) if error_event else ""
    grouped = {
        "runtime": _runtime_checks(requests, responses, events, error_text, error_event, status == "stopped"),
        "harness": _harness_checks(requests, events, requirements, error_text, error_event),
        "information": _information_checks(requests, events, requirements),
        "model": _model_checks(requests, events, requirements),
    }
    layers = [
        {
            "id": name,
            "label": LABELS[name],
            "status": _layer_status(grouped[name]),
            "checks": grouped[name],
        }
        for name in ORDER
    ]
    focus = None
    failed = status in {"failed", "interrupted"}
    if failed:
        focus = next((item["id"] for item in layers if item["status"] == "issue"), None)
    if status == "completed":
        verification = _verification(events, requirements, status)
        headline = (
            "완료됐고 코드가 확인한 범위는 아래와 같습니다."
            if verification["by_code"]
            else "완료 상태이지만 코드가 확인한 완료 조건은 없습니다. 모델이 답변을 냈다는 뜻입니다."
        )
    elif failed and focus:
        verification = _verification(events, requirements, status)
        headline = f"기록은 ‘{LABELS[focus]}’ 층을 먼저 살펴보라고 가리킵니다."
    elif failed:
        verification = _verification(events, requirements, status)
        headline = "기록만으로는 살펴볼 층을 좁히지 못했습니다. 오류 문구와 코드 위치를 확인하세요."
    elif status == "stopped":
        verification = _verification(events, requirements, status)
        headline = "사용자가 중지한 실행입니다. 원인 분석 대상이 아닙니다."
    else:
        verification = _verification(events, requirements, status)
        headline = "아직 끝나지 않은 실행입니다. 지금까지의 기록만 반영합니다."
    return {
        "run_id": snapshot.get("id"),
        "status": status,
        "focus": focus,
        "focus_label": LABELS.get(focus),
        "headline": headline,
        "next_step": NEXT_STEPS.get(focus),
        "layers": layers,
        "verification": verification,
        "note": NOTE,
    }
