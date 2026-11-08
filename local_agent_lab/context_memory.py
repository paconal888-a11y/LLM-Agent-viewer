"""원본 대화 보존, 실제 로컬 모델 요약, 다음 입력 선택을 구분한다."""

from __future__ import annotations
import hashlib
import json
import os
import time
import uuid
from .files import read_traced
from .runtime import RuntimeFailure
from .orchestration import inference_turn
from .inference import preview_input, generate, audit_response, input_fingerprint

STATE_LIMIT = 524288


def bounded_state(value):
    """원문은 archive에 남기고 활성 상태는 읽기와 같은 UTF-8 예산에 맞춘다."""
    value = json.loads(json.dumps(value, ensure_ascii=False))
    omitted = 0
    size = lambda: len(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))
    while size() > STATE_LIMIT:
        if value.get("turns"):
            value["turns"].pop(0)
            if value.get("turn_ids"):
                value["turn_ids"].pop(0)
            if value.get("turn_origins"):
                value["turn_origins"].pop(0)
            omitted += 1
        elif value.get("feedback_ledger"):
            value["feedback_ledger"].pop(0)
        else:
            raise ValueError("활성 세션의 요약·메타데이터가 512 KiB 한도를 넘었습니다.")
        value["state_budget"] = {
            "max_bytes": STATE_LIMIT,
            "omitted_recent_turns": omitted,
            "raw_messages_preserved_in_archive": True,
        }
    return value


def archive_path(harness, session_id):
    harness.history_path(session_id)
    return harness.state_dir / "conversations" / (session_id + ".jsonl")


def legacy_records(history):
    ids = history.get("turn_ids", [])
    return [
        {
            "run_id": ids[index] if index < len(ids) else "legacy-" + str(index),
            "messages": turn,
            "recovered_recent_only": True,
        }
        for index, turn in enumerate(history.get("turns", []))
    ]


ARCHIVE_LIMIT = 4 * 1024 * 1024
# JSON 안에 그대로 쓰면 str.splitlines()가 줄로 오인하는 문자. 쓸 때 이스케이프하고 읽을 때는 '\n'으로만 나눈다.
_LINE_BREAKERS = {"\u2028": "\\u2028", "\u2029": "\\u2029", "\u0085": "\\u0085"}


def archive_line(record):
    text = json.dumps(record, ensure_ascii=False, allow_nan=False)
    for raw, escaped in _LINE_BREAKERS.items():
        text = text.replace(raw, escaped)
    return text + "\n"


def parse_archive(text):
    """줄 단위 JSON을 읽되 손상된 줄은 건너뛰고 위치·원인을 돌려준다."""
    records, corrupt = [], []
    for number, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict) or not isinstance(record.get("messages"), list):
                raise ValueError("레코드 형식이 아닙니다.")
            records.append(record)
        except ValueError as error:
            corrupt.append({"line": number, "error": str(error)[:200], "characters": len(line)})
    return records, corrupt


def load_archive(harness, session_id, run=None, history=None, *, tail_bytes=None):
    """(records, path, corrupt_lines). archive가 없으면 세션 상태의 최근 원문(legacy)을 돌려준다."""
    path = archive_path(harness, session_id)
    if not path.exists():
        return legacy_records(history or {}), None, []
    size = path.stat().st_size
    if size > ARCHIVE_LIMIT and tail_bytes is None:
        raise ValueError("대화 원본이 4 MiB 한도를 넘었습니다. 새 대화를 시작하세요.")
    if tail_bytes is not None and size > tail_bytes:
        with path.open("rb") as handle:
            handle.seek(size - tail_bytes)
            raw = handle.read()
        text = raw.decode("utf-8", errors="replace").split("\n", 1)[-1]
    elif run:
        text = read_traced(
            run,
            path,
            "memory",
            reason="아직 요약하지 않은 원본 대화를 선택합니다.",
            max_bytes=ARCHIVE_LIMIT,
        )
    else:
        text = path.read_text(encoding="utf-8")
    records, corrupt = parse_archive(text)
    if corrupt and run is not None:
        run.emit(
            "memory.archive.corrupt",
            "대화 원본에 읽을 수 없는 줄",
            "JSONL의 일부 줄을 해석하지 못해 건너뛰었습니다. 나머지 레코드는 사용하며 파일은 바꾸지 않습니다.",
            category="memory",
            status="warning",
            output={"path": str(path), "corrupt_lines": corrupt, "records": len(records)},
            pause=False,
        )
    return records, str(path), corrupt


def read_archive(harness, session_id, run=None, history=None):
    records, path, _ = load_archive(harness, session_id, run, history)
    return records, path


def write_state(path, value):
    value = bounded_state(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex[:8] + ".tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def append_archive(harness, run, previous, turn):
    path = archive_path(harness, run.session_id)
    records = [] if path.exists() else legacy_records(previous)
    records.append(
        {
            "run_id": run.id,
            "created_at": run.created_at,
            "messages": turn,
            # API role과 별개로 각 메시지를 만든 주체. 다음 턴 복원과 요약 입력에서 하네스 지시를 사용자 발화와 구분한다.
            "origins": message_origins(run, turn),
        }
    )
    payload = "".join(archive_line(record) for record in records)
    encoded = payload.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(payload)
    count = previous.get("archive_turn_count", len(legacy_records(previous))) + 1
    run.emit(
        "memory.archive.append",
        "대화 원본 저장",
        "요약이나 최근 대화 선택과 별도로, 완료한 대화의 원문을 JSONL에 추가합니다.",
        category="memory",
        input={"records": records},
        output={
            "path": str(path),
            "written_bytes": len(encoded),
            "file_bytes": path.stat().st_size,
            "sha256_appended": hashlib.sha256(encoded).hexdigest(),
            "archive_turn_count": count,
        },
        pause=False,
    )
    return count


def message_origins(run, turn):
    """저장할 턴의 각 메시지에 대해 record_message가 남긴 작성 주체를 찾는다. 없으면 unknown."""
    segments = getattr(run, "context_segments", []) or []
    offset = getattr(run, "turn_message_offset", None)
    result = []
    for index, message in enumerate(turn):
        origin, reason, source = "unknown", None, None
        if offset is not None:
            segment = next(
                (
                    s
                    for s in reversed(segments)
                    if s.get("message_index") == offset + index
                ),
                None,
            )
            if segment:
                origin = segment.get("origin") or "unknown"
                reason = segment.get("reason") or segment.get("label")
                source = segment.get("source_event_id")
        result.append(
            {"index": index, "role": message.get("role"), "origin": origin, "reason": reason, "source_event_id": source}
        )
    return result


def labelled_messages(record):
    """요약기에 넘길 메시지. 하네스가 만든 user 메시지는 origin 필드로 표시해 사용자 발화와 구분한다."""
    origins = {o.get("index"): o for o in record.get("origins", []) if isinstance(o, dict)}
    labelled = []
    for index, message in enumerate(record.get("messages", [])):
        origin = origins.get(index, {}).get("origin", "unknown")
        item = dict(message)
        if origin == "harness":
            item["origin"] = "harness"
            item["note"] = "하네스가 추가한 지시 · 사용자 발화 아님"
        labelled.append(item)
    return labelled


def shrink_record(record, budget):
    """한 레코드가 요약 예산보다 크면 큰 도구 결과·본문을 자리표시자로 바꾼다. archive 원문은 그대로다."""
    messages = [dict(m) for m in labelled_messages(record)]
    omitted = []

    def size():
        return len(json.dumps(messages, ensure_ascii=False))

    # 긴 메시지부터 줄인다. 사용자 메시지는 가능한 한 마지막에 줄인다.
    order = sorted(
        range(len(messages)),
        key=lambda i: (messages[i].get("role") == "user", -len(str(messages[i].get("content") or ""))),
    )
    for index in order:
        if size() <= budget:
            break
        content = messages[index].get("content")
        if not isinstance(content, str) or len(content) <= 300:
            continue
        kept = content[:200]
        messages[index]["content"] = kept + f"… [생략 {len(content) - len(kept)}자 · 원문은 대화 원본 저장소에 보존]"
        omitted.append({"index": index, "role": messages[index].get("role"), "omitted_characters": len(content) - len(kept)})
    return messages, omitted


def prepare(harness, run, history):
    if not run.features.get("compaction"):
        return history
    config = json.loads(
        read_traced(
            run,
            harness.workspace / "memory.json",
            "memory",
            reason="대화 선택과 요약 기준을 읽습니다.",
        )
    )
    bounds = {
        "trigger_characters": (100, 50000),
        "keep_recent_turns": (1, 3),
        "chunk_characters": (500, 6000),
        "max_summary_calls": (1, 3),
        "summary_max_tokens": (32, 256),
    }
    for key, (low, high) in bounds.items():
        if type(config.get(key)) is not int or not low <= config[key] <= high:
            raise ValueError(f"memory.json {key}: {low}~{high} 정수입니다.")
    if (
        not isinstance(config.get("instruction"), str)
        or not 1 <= len(config["instruction"]) <= 2000
    ):
        raise ValueError("요약 지침을 1~2000자로 작성하세요.")
    records, path = read_archive(harness, run.session_id, run, history)
    if records and path is None:
        target = archive_path(harness, run.session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(archive_line(record) for record in records)
        target.write_text(payload, encoding="utf-8", newline="")
        path = str(target)
        run.emit(
            "memory.archive.bootstrap",
            "이전 버전의 대화 원문 이관",
            "기존 세션이 보관한 최근 원문만 옮깁니다. 이미 빠진 과거 대화는 복원했다고 표시하지 않습니다.",
            category="memory",
            output={
                "path": path,
                "recovered_turns": len(records),
                "written_bytes": len(payload.encode("utf-8")),
            },
        )
    through = history.get("summary_through", 0)
    if type(through) is not int or through < 0:
        raise ValueError("대화 요약의 원본 범위가 맞지 않습니다.")
    summarized_ids = history.get("summary_source_run_ids")
    if isinstance(summarized_ids, list):
        # 정수 오프셋 대신 run_id로 범위를 다시 계산한다. archive가 지워졌다 다시 만들어지면 오프셋은 새 턴을 가리킨다.
        matched = 0
        while matched < min(len(summarized_ids), len(records)) and records[matched].get("run_id") == summarized_ids[matched]:
            matched += 1
        if matched != len(summarized_ids) or through != matched:
            run.emit(
                "memory.compaction.range.mismatch",
                "요약 범위와 대화 원본이 어긋남",
                "저장된 요약이 가리키는 run_id 목록과 현재 archive의 앞부분이 다릅니다. 일치하는 앞부분까지만 요약된 것으로 보고 나머지는 원문으로 다룹니다. 새 턴을 요약된 것으로 간주해 입력에서 빼지 않습니다.",
                category="memory",
                status="warning",
                output={"summary_through": through, "summary_source_run_ids": summarized_ids, "archive_run_ids": [r.get("run_id") for r in records[: len(summarized_ids) + 1]], "matched": matched},
            )
            through = matched
    elif through > len(records):
        # run_id 목록이 없는 이전 형식의 세션. archive가 다시 만들어졌을 가능성이 크므로 실패 대신 전부 원문으로 다룬다.
        run.emit(
            "memory.compaction.range.mismatch",
            "요약 범위가 대화 원본보다 큼",
            "저장된 요약 범위(정수 오프셋)가 현재 archive의 레코드 수를 넘습니다. 어떤 레코드가 요약됐는지 확인할 수 없어 모든 레코드를 원문으로 다룹니다.",
            category="memory",
            status="warning",
            output={"summary_through": through, "archive_turns": len(records)},
        )
        through = 0
    remaining = records[through:]
    summary = history.get("summary", "")
    raw = json.dumps([record["messages"] for record in remaining], ensure_ascii=False)
    content_characters = sum(len(str(m.get("content") or "")) for r in remaining for m in r["messages"])
    keep = config["keep_recent_turns"]
    candidates = remaining[:-keep] if len(remaining) > keep else []
    due = len(raw) + len(summary) > config["trigger_characters"] and bool(candidates)
    run.model_purpose = "compaction" if due else run.model_purpose
    run.emit(
        "memory.compaction.check",
        "요약 필요 여부 확인",
        "기준은 JSON으로 직렬화한 문자 수이며 토큰 수가 아닙니다. 최근 대화는 원문으로 남기고 오래된 대화만 요약 대상으로 고릅니다.",
        category="memory",
        input={
            "config": config,
            "archive_path": path,
            "archive_turns": len(records),
            "summary_through": through,
            "context_json_characters": len(raw) + len(summary),
            "context_characters": len(raw) + len(summary),
            "content_characters": content_characters,
            "unit_note": "context_json_characters는 messages를 JSON으로 직렬화한 길이(따옴표·이스케이프 포함), content_characters는 content 문자열 길이의 합",
        },
        output={
            "needed": due,
            "candidate_run_ids": [r["run_id"] for r in candidates],
            "keep_recent_turns": keep,
            "existing_summary": summary,
        },
    )
    if due:
        # 레코드 단위로 묶는다. 예산보다 큰 레코드는 긴 본문을 자리표시자로 줄여 요약 호출이 영구 실패하지 않게 한다.
        budget = config["chunk_characters"]
        prepared, shrunk = [], []
        for record in candidates:
            messages = labelled_messages(record)
            if len(json.dumps(messages, ensure_ascii=False)) > budget:
                messages, omitted = shrink_record(record, budget)
                shrunk.append({"run_id": record["run_id"], "omitted": omitted})
            prepared.append((record["run_id"], messages))
        batches, current, current_size = [], [], 0
        for run_id, messages in prepared:
            piece = len(json.dumps(messages, ensure_ascii=False)) + 2
            if current and current_size + piece > budget:
                batches.append(current)
                current, current_size = [], 0
            current.append((run_id, messages))
            current_size += piece
        if current:
            batches.append(current)
        allowed_calls = max(1, min(config["max_summary_calls"], run.max_model_calls - 1))
        deferred = []
        if len(batches) > allowed_calls:
            deferred = batches[allowed_calls:]
            batches = batches[:allowed_calls]
            run.emit(
                "memory.compaction.partial",
                "요약 호출 예산을 넘어 일부만 요약",
                "이번 요청에서는 예산 안의 묶음만 요약하고 나머지 원문은 다음 요청의 요약 대상으로 남깁니다. 실행을 실패시키지 않습니다.",
                category="memory",
                status="warning",
                output={"batches_now": len(batches), "batches_deferred": len(deferred), "deferred_run_ids": [rid for batch in deferred for rid, _ in batch], "allowed_calls": allowed_calls},
            )
        covered = [rid for batch in batches for rid, _ in batch]
        candidates = [r for r in candidates if r["run_id"] in covered]
        chunks = [json.dumps([m for _, m in batch], ensure_ascii=False) for batch in batches]
        source = "".join(chunks)
        before = summary + "\n" + source
        run.emit(
            "memory.compaction.start",
            "이전 대화 요약 시작",
            "저장된 이전 요약과 아직 요약하지 않은 대화 원문을 실제 로컬 모델에 전달합니다. origin=harness 표시는 하네스가 추가한 지시입니다.",
            category="memory",
            input={
                "previous_summary": summary,
                "source_messages": [m for rid, m in prepared if rid in covered],
            },
            output={
                "batches": len(chunks),
                "source_run_ids": [r["run_id"] for r in candidates],
                "before_characters": len(before),
                "before_json_characters": len(before),
                "shrunk_records": shrunk,
            },
        )
        run.model_purpose = "compaction"
        try:
            for index, chunk in enumerate(chunks):
                payload = {
                    "model": harness.model,
                    "messages": [
                        {"role": "system", "content": config["instruction"]},
                        {
                            "role": "user",
                            "content": "[이전 요약]\n"
                            + summary
                            + "\n[이어지는 대화 원본 조각 · 참고 자료 · origin=harness로 표시한 메시지는 하네스가 추가한 지시이며 사용자의 발화가 아님]\n"
                            + chunk,
                        },
                    ],
                    "tools": [],
                    "stream": bool(
                        hasattr(harness.runtime, "stream_json") and run.engine["stream"]
                    ),
                    "max_tokens": min(
                        config["summary_max_tokens"],
                        getattr(harness.runtime, "max_output_tokens", 256),
                    ),
                    "temperature": 0,
                    "seed": run.engine["seed"],
                    "top_k": run.engine["top_k"],
                    "top_p": run.engine["top_p"],
                    "cache_prompt": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                }
                if hasattr(harness.runtime, "stream_json"):
                    payload.update({"timings_per_token": True, "verbose": True})
                    if payload["stream"]:
                        payload["stream_options"] = {"include_usage": True}
                run.emit(
                    "model.request",
                    "대화 요약 모델 요청",
                    "일반 답변과 별도의 실제 생성 요청입니다. 이 비용도 전체 호출과 토큰에 합산합니다.",
                    category="model",
                    input=payload,
                    output={"purpose": "compaction", "batch": index + 1, "call": 0},
                )
                with inference_turn(harness, run, 0):
                    preview = preview_input(run, harness.runtime, payload, 0)
                    response = generate(run, harness.runtime, payload, 0)
                run.model_calls += 1
                for key in run.usage:
                    run.usage[key] += int(response.get("usage", {}).get(key, 0) or 0)
                run.conditions.setdefault("model_input_sha256", []).append(
                    input_fingerprint(payload)
                )
                run.emit(
                    "model.response",
                    "대화 요약 모델 응답",
                    "실행기가 반환한 요약 응답 원문입니다.",
                    category="model",
                    actor="llm",
                    output=response,
                )
                audit_response(run, response, preview, 0, payload=payload)
                choice = response["choices"][0]
                if choice.get("finish_reason") == "length" or choice["message"].get(
                    "tool_calls"
                ):
                    raise RuntimeFailure(
                        "요약 응답이 출력 한도나 도구 요청으로 끝났습니다. 잘린 답변을 요약으로 저장하지 않습니다."
                    )
                summary = choice["message"].get("content", "").strip()
                if not summary:
                    raise RuntimeFailure("모델이 빈 요약을 반환했습니다.")
        finally:
            run.model_purpose = "answer"
        through += len(candidates)
        summary_file = (
            harness.state_dir / "summaries" / run.session_id / (run.id + ".json")
        )
        record = {
            "summary": summary,
            "source_run_ids": [r["run_id"] for r in records[:through]],
            "summary_through": through,
            "method": "local_model",
            "model": harness.model,
            "before_characters": len(before),
            "before_json_characters": len(before),
            "after_characters": len(summary),
            "unit_note": "before_json_characters는 요약 입력(JSON 직렬화)의 길이, after_characters는 요약 문자열 길이",
        }
        write_state(summary_file, record)
        checkpoint = {
            **history,
            "summary": summary,
            "summary_through": through,
            "summary_source_run_ids": record["source_run_ids"],
            "summary_path": str(summary_file),
            "archive_turn_count": len(records),
        }
        write_state(harness.history_path(run.session_id), checkpoint)
        run.emit(
            "memory.compaction.result",
            "요약과 원본 범위 저장",
            "원본 대화는 삭제하지 않습니다. 요약 문장에 모든 사실이 보존됐다고 보증하지 않으며, 원본과 대조할 수 있습니다.",
            category="memory",
            input={"before_text": before},
            output={
                **record,
                "path": str(summary_file),
                "checkpoint_path": str(harness.history_path(run.session_id)),
            },
        )
        remaining = records[through:]
        history = checkpoint
    run.model_purpose = "answer"
    # 요약 모드에서도 원문 입력에 상한을 둔다. 오래된 레코드부터 제외하고 제외한 run_id를 남긴다.
    kept = list(remaining)
    omitted_ids = []
    while kept and len(json.dumps([r["messages"] for r in kept], ensure_ascii=False).encode("utf-8")) > STATE_LIMIT:
        omitted_ids.append(kept.pop(0)["run_id"])
    selected = {
        **history,
        "turns": [r["messages"] for r in kept],
        "turn_ids": [r["run_id"] for r in kept],
        "turn_origins": [r.get("origins") for r in kept],
        "summary": summary,
        "summary_through": through,
        "archive_turn_count": len(records),
    }
    run.emit(
        "memory.context.select",
        "이번 입력의 대화 선택",
        "대화 원본 저장소, 요약, 이번 messages에 넣을 최근 원문을 구분합니다. 원문 입력은 512 KiB를 넘지 않게 오래된 레코드부터 제외합니다.",
        category="memory",
        input={"archive_path": path, "archive_turn_count": len(records), "unsummarized_turns": len(remaining)},
        output={
            "summary": summary,
            "summary_path": selected.get("summary_path"),
            "summary_through": through,
            "selected_run_ids": selected["turn_ids"],
            "omitted_run_ids": omitted_ids,
            "selected_messages": [m for t in selected["turns"] for m in t],
            "weights_updated": False,
        },
    )
    return selected


def inspect(harness, session_id):
    path = harness.history_path(session_id)
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    archive_file = archive_path(harness, session_id)
    archive_bytes = archive_file.stat().st_size if archive_file.exists() else 0
    truncated = archive_bytes > ARCHIVE_LIMIT
    records, archive, corrupt = load_archive(
        harness, session_id, history=state, tail_bytes=ARCHIVE_LIMIT if truncated else None
    )
    return {
        "session_id": session_id,
        "state_path": str(path),
        "archive_path": archive,
        "archive_bytes": archive_bytes,
        "records_truncated": truncated,
        "corrupt_lines": corrupt,
        "state": state,
        "records": records,
        "summary": {
            "text": state.get("summary", ""),
            "through_turn": state.get("summary_through", 0),
            "source_run_ids": state.get("summary_source_run_ids"),
            "path": state.get("summary_path"),
        },
        "weights_updated": False,
    }
