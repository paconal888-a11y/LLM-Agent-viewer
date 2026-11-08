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


def read_archive(harness, session_id, run=None, history=None):
    path = archive_path(harness, session_id)
    if not path.exists():
        return legacy_records(history or {}), None
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("대화 원본이 4 MiB 한도를 넘었습니다. 새 대화를 시작하세요.")
    text = (
        read_traced(
            run,
            path,
            "memory",
            reason="아직 요약하지 않은 원본 대화를 선택합니다.",
            max_bytes=4 * 1024 * 1024,
        )
        if run
        else path.read_text(encoding="utf-8")
    )
    return [json.loads(line) for line in text.splitlines() if line.strip()], str(path)


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
    records.append({"run_id": run.id, "created_at": run.created_at, "messages": turn})
    payload = "".join(
        json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
        for record in records
    )
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
        payload = "".join(
            json.dumps(record, ensure_ascii=False) + "\n" for record in records
        )
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
    if type(through) is not int or not 0 <= through <= len(records):
        raise ValueError("대화 요약의 원본 범위가 맞지 않습니다.")
    remaining = records[through:]
    summary = history.get("summary", "")
    raw = json.dumps([record["messages"] for record in remaining], ensure_ascii=False)
    keep = config["keep_recent_turns"]
    candidates = remaining[:-keep] if len(remaining) > keep else []
    due = len(raw) + len(summary) > config["trigger_characters"] and bool(candidates)
    run.emit(
        "memory.compaction.check",
        "요약 필요 여부 확인",
        "기준은 문자 수이며 토큰 수가 아닙니다. 최근 대화는 원문으로 남기고 오래된 대화만 요약 대상으로 고릅니다.",
        category="memory",
        input={
            "config": config,
            "archive_path": path,
            "archive_turns": len(records),
            "summary_through": through,
            "context_characters": len(raw) + len(summary),
        },
        output={
            "needed": due,
            "candidate_run_ids": [r["run_id"] for r in candidates],
            "keep_recent_turns": keep,
            "existing_summary": summary,
        },
    )
    if due:
        source = json.dumps(
            [record["messages"] for record in candidates], ensure_ascii=False
        )
        chunks = [
            source[index : index + config["chunk_characters"]]
            for index in range(0, len(source), config["chunk_characters"])
        ]
        if len(chunks) > min(config["max_summary_calls"], run.max_model_calls - 1):
            raise RuntimeFailure(
                "이번 대화의 요약 호출 예산을 넘었습니다. memory.json의 기준·예산을 조정하거나 새 대화를 시작하세요. 원본은 보존했습니다."
            )
        before = summary + "\n" + source
        run.emit(
            "memory.compaction.start",
            "이전 대화 요약 시작",
            "저장된 이전 요약과 아직 요약하지 않은 대화 원문을 실제 로컬 모델에 전달합니다.",
            category="memory",
            input={
                "previous_summary": summary,
                "source_messages": [r["messages"] for r in candidates],
            },
            output={
                "batches": len(chunks),
                "source_run_ids": [r["run_id"] for r in candidates],
                "before_characters": len(before),
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
                            + "\n[이어지는 대화 원본 조각 · 참고 자료]\n"
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
            "after_characters": len(summary),
        }
        write_state(summary_file, record)
        checkpoint = {
            **history,
            "summary": summary,
            "summary_through": through,
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
    selected = {
        **history,
        "turns": [r["messages"] for r in remaining],
        "turn_ids": [r["run_id"] for r in remaining],
        "summary": summary,
        "summary_through": through,
        "archive_turn_count": len(records),
    }
    run.emit(
        "memory.context.select",
        "이번 입력의 대화 선택",
        "대화 원본 저장소, 요약, 이번 messages에 넣을 최근 원문을 구분합니다.",
        category="memory",
        input={"archive_path": path, "archive_turn_count": len(records)},
        output={
            "summary": summary,
            "summary_path": selected.get("summary_path"),
            "summary_through": through,
            "selected_run_ids": selected["turn_ids"],
            "selected_messages": [m for t in selected["turns"] for m in t],
            "weights_updated": False,
        },
    )
    return selected


def inspect(harness, session_id):
    path = harness.history_path(session_id)
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    records, archive = read_archive(harness, session_id, history=state)
    return {
        "session_id": session_id,
        "state_path": str(path),
        "archive_path": archive,
        "state": state,
        "records": records,
        "summary": {
            "text": state.get("summary", ""),
            "through_turn": state.get("summary_through", 0),
            "path": state.get("summary_path"),
        },
        "weights_updated": False,
    }
