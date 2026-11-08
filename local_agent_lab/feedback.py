"""지적 내용이 저장·선택·입력된 사실을 대조한다. 모델의 가중치 학습을 추정하지 않는다."""

import hashlib


def prompt_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def collect_feedback(run):
    return [
        {
            "run_id": run.id,
            "event_id": e["id"],
            "prompt_sha256": prompt_hash(run.effective_prompt),
            "text": e["output"]["appended_message"]["content"][:4000],
            "complete": len(e["output"]["appended_message"]["content"]) <= 4000,
        }
        for e in run.events
        if e["kind"] == "context.repair" and e.get("agent_id", run.id) == run.id
    ]


def recall_and_audit(run, history, messages):
    ledger = history.get("feedback_ledger", [])
    same = [
        item
        for item in ledger
        if item.get("prompt_sha256") == prompt_hash(run.effective_prompt)
    ]
    if run.features.get("feedback_recall") and same:
        selected = same[-3:]
        message = {
            "role": "user",
            "content": "[동일 요청의 과거 보완 지적 · 참고 자료, 현재 요청 우선]\n"
            + "\n\n".join(item["text"] for item in selected),
        }
        before = len(messages)
        messages.append(message)
        run.emit(
            "memory.feedback.recall",
            "동일 요청의 과거 지적을 다시 입력",
            "저장된 지적 중 같은 원본 요청 해시의 최근 세 개만 선택합니다. 가중치를 학습하지 않고 실제 메시지로 다시 전달합니다.",
            category="memory",
            input={
                "available_feedback": len(ledger),
                "same_prompt_feedback": len(same),
                "message_count_before": before,
            },
            output={
                "appended_message": message,
                "message_count_after": len(messages),
                "source_events": [
                    {"run_id": v["run_id"], "event_id": v["event_id"]} for v in selected
                ],
            },
        )
    rows = []
    for item in ledger:
        matches = [
            i
            for i, message in enumerate(messages)
            if item["text"] and item["text"] in (message.get("content") or "")
        ]
        rows.append(
            {
                **item,
                "in_model_input": bool(matches),
                "message_indices": matches,
                "reason": "실제 messages에서 저장한 지적 문자열 일치"
                if matches
                else "최근 두 대화에서 제외됐거나 별도 재포함을 끈 상태",
            }
        )
    run.emit(
        "memory.feedback.audit",
        "과거 지적이 이번 입력에 남아 있는지 확인",
        "저장된 사실과 모델 입력에 들어간 사실은 다릅니다. 실제 messages의 문자열을 대조합니다. 지적이 포함돼도 같은 실수를 하지 않는다고 보증하지 않습니다.",
        category="memory",
        input={
            "memory_enabled": run.features["memory"],
            "recall_enabled": run.features.get("feedback_recall", False),
            "selected_turn_ids": history.get("turn_ids", []),
        },
        output={
            "feedback": rows,
            "stored_feedback": len(ledger),
            "included_feedback": sum(row["in_model_input"] for row in rows),
            "weights_updated": False,
            "unavailable_reason": None
            if ledger
            else "저장된 지적 없음 또는 대화 이력 불러오기 꺼짐",
        },
    )


def report(snapshot):
    events = snapshot.get("events", [])
    owner_id = (snapshot.get("sample") or {}).get("source_run_id", snapshot.get("id"))
    repairs = []
    for index, event in enumerate(events):
        if event["kind"] != "context.repair":
            continue
        later = [
            e
            for e in events[index + 1 :]
            if e.get("agent_id", owner_id) == event.get("agent_id", owner_id)
        ]
        request = next((e for e in later if e["kind"] == "model.request"), None)
        check = next((e for e in later if e["kind"] == "validation.requirements"), None)
        correction = event["output"]["appended_message"]["content"]
        tools = []
        for item in later:
            if item["kind"] in {"context.repair", "output.answer"}:
                break
            if item["kind"] == "tool.result":
                tools.append(
                    {
                        "event_id": item["id"],
                        "title": item["title"],
                        "output": item["output"],
                    }
                )
        repairs.append(
            {
                "event_id": event["id"],
                "agent_name": event.get("agent_name", "주 에이전트"),
                "agent_id": event.get("agent_id", owner_id),
                "rejected_answer": event.get("input", {}).get("rejected_answer"),
                "correction": correction,
                "next_request_event": request["id"] if request else None,
                "feedback_in_next_request": any(
                    m.get("content") == correction
                    for m in (request or {}).get("input", {}).get("messages", [])
                ),
                "observed_tools": tools,
                "next_check": check["output"] if check else None,
            }
        )
    audit = next(
        (
            e["output"]
            for e in events
            if e["kind"] == "memory.feedback.audit"
            and e.get("agent_id", owner_id) == owner_id
        ),
        None,
    )
    return {
        "run_id": snapshot.get("id"),
        "sample": snapshot.get("sample"),
        "session_id": snapshot.get("session_id"),
        "prompt": snapshot.get("prompt"),
        "requirements": snapshot.get("requirements", []),
        "features": snapshot.get("features", {}),
        "model": snapshot.get("conditions", {}).get("model"),
        "engine": snapshot.get("engine", {}),
        "mode": snapshot.get("mode", "single"),
        "repairs": repairs,
        "memory": audit,
        "weights_updated": False,
        "note": "관측한 응답·입력·실행 결과입니다. 한 번의 개선은 재학습이나 지속적 재발 방지를 뜻하지 않습니다.",
    }
