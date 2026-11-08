"""실행 기록의 본문 검색. 이벤트 제목이 아니라 실제 모델 요청·도구 결과·파일 원문 안을 찾는다.

결과는 "어느 이벤트의 어느 필드"에 문자열이 있는지와 짧은 발췌다. 모델 요청의 messages 안이면
메시지 번호·role·origin(작성 주체)·요청 번호를 함께 돌려주어 화면이 해당 요청·메시지로 이동할 수 있게 한다.
검색은 저장된 기록을 읽기만 하며 모델이나 도구를 실행하지 않는다.
"""

from __future__ import annotations

import re

MAX_HITS = 200
SNIPPET = 80
# 본문으로 취급하는 영역. 제목·설명 같은 하네스 문구는 포함하지 않는다.
BODY_ROOTS = ("input", "output")


def _snippet(text, start, end):
    left = max(0, start - SNIPPET)
    right = min(len(text), end + SNIPPET)
    return ("…" if left else "") + text[left:right] + ("…" if right < len(text) else "")


def _message_context(event, path):
    """messages[i] 안의 적중이면 메시지 번호·role·origin을 붙인다."""
    keys = list(path)
    if "messages" not in keys:
        return {}
    index = keys.index("messages")
    if index + 1 >= len(keys) or not isinstance(keys[index + 1], int):
        return {}
    message_index = keys[index + 1]
    container = event
    for key in keys[: index + 1]:
        container = container[key]
    message = container[message_index] if message_index < len(container) else {}
    context = {
        "message_index": message_index,
        "role": message.get("role") if isinstance(message, dict) else None,
    }
    segments = (event.get("output") or {}).get("segments") if isinstance(event.get("output"), dict) else None
    if isinstance(segments, list):
        segment = next(
            (s for s in segments if s.get("message_index") == message_index), None
        )
        if segment:
            context["origin"] = segment.get("origin")
            context["reason"] = segment.get("reason")
            context["label"] = segment.get("label")
            context["source_event_id"] = segment.get("source_event_id")
    return context


def search(snapshot, query, *, kinds=None, limit=MAX_HITS):
    """대소문자를 구분하지 않는 부분 문자열 검색. 정규식이 아니다."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("검색어를 입력하세요.")
    if len(query) > 200:
        raise ValueError("검색어는 200자까지입니다.")
    needle = re.compile(re.escape(query), re.IGNORECASE)
    hits = []
    truncated = False
    request_numbers = {}
    counter = 0
    for event in snapshot.get("events", []):
        if event.get("kind") == "model.request":
            counter += 1
            request_numbers[event["id"]] = counter

    def visit(event, value, path):
        nonlocal truncated
        if len(hits) >= limit:
            truncated = True
            return
        if path[:2] == ("output", "segments"):
            # segments는 messages를 설명하는 파생 보기다. 같은 본문을 두 번 세지 않는다.
            return
        if isinstance(value, str):
            for match in needle.finditer(value):
                if len(hits) >= limit:
                    truncated = True
                    return
                hits.append(
                    {
                        "event_id": event["id"],
                        "kind": event.get("kind"),
                        "title": event.get("title"),
                        "agent_id": event.get("agent_id"),
                        "agent_name": event.get("agent_name"),
                        "model_purpose": event.get("model_purpose"),
                        "request_number": request_numbers.get(event["id"]),
                        "field": ".".join(
                            str(p) if isinstance(p, str) else "[" + str(p) + "]"
                            for p in path
                        ).replace(".[", "["),
                        "offset": match.start(),
                        "snippet": _snippet(value, match.start(), match.end()),
                        **_message_context(event, path),
                    }
                )
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(event, item, path + (index,))
        elif isinstance(value, dict):
            for key, item in value.items():
                visit(event, item, path + (key,))

    for event in snapshot.get("events", []):
        if kinds and event.get("kind") not in kinds:
            continue
        for root in BODY_ROOTS:
            if root in event and event[root] is not None:
                visit(event, event[root], (root,))
        if truncated:
            break
    summary = {}
    for hit in hits:
        summary[hit["kind"]] = summary.get(hit["kind"], 0) + 1
    return {
        "query": query,
        "hits": hits,
        "count": len(hits),
        "truncated": truncated,
        "by_kind": summary,
        "scope": "이벤트의 input·output 본문(모델 요청 messages·tools, 도구 결과, 파일 원문, 스트림 조각 포함). 제목·설명 문구는 제외.",
    }
