"""사용자 입력부터 도구 실행·검사·완료까지의 에이전트 루프."""

import json
import time
import traceback
from pathlib import Path
from .trace import RunStopped
from .runtime import RuntimeFailure
from .features import workspace_signature
from .context import (
    expand_command,
    build_context,
    tools_for,
    describe_context,
    record_message,
    resolve_settings,
    load_directory_rules,
)
from .policy import (
    final_gate,
    requirement_gate,
    repeated_rejection,
    REPEATED_ANSWER_LIMIT,
)
from .integrations import prepare as prepare_integrations
from .compaction import new_view, prepare_input, remember_measure, view_segments
from .checkpoint import discard_checkpoint, save_checkpoint, trim_checkpoints
from .feedback import recall_and_audit
from .context_memory import prepare as prepare_memory
from .orchestration import prepare as prepare_agents, inference_turn
from .inference import (
    preview_input,
    observe_engine,
    observe_slots,
    generate,
    audit_response,
    input_fingerprint,
)


def unexpected_location(exc):
    """예상한 실행 실패가 아닌 예외의 종류와 마지막 코드 위치. 편집한 하네스 코드의 버그를 찾는 근거다."""
    if isinstance(exc, RuntimeFailure):
        return {}
    frames = traceback.extract_tb(exc.__traceback__)
    location = {"type": type(exc).__name__}
    if frames:
        location.update(
            file=Path(frames[-1].filename).name,
            line=frames[-1].lineno,
            function=frames[-1].name,
        )
    return location


def close_open_tool_calls(run, messages):
    """결과를 받기 전에 중단된 도구 호출에 '결과 모름'을 채운다. 같은 호출을 조용히 다시 실행하지 않는다."""
    last = next(
        (i for i in range(len(messages) - 1, -1, -1) if messages[i]["role"] == "assistant"),
        None,
    )
    if last is None or not messages[last].get("tool_calls"):
        return []
    answered = {m.get("tool_call_id") for m in messages[last + 1 :]}
    open_calls = [c for c in messages[last]["tool_calls"] if c.get("id") not in answered]
    for call in open_calls:
        before = len(messages)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": json.dumps(
                    {
                        "error": "이전 실행이 이 도구의 결과를 받기 전에 중단됐습니다. 실행됐는지 알 수 없으니 필요하면 상태를 먼저 확인하세요.",
                        "executed_successfully": False,
                        "side_effects": "unknown",
                    },
                    ensure_ascii=False,
                ),
            }
        )
        record_message(run, messages, before, "harness", "중단으로 결과를 모르는 도구 호출")
    return open_calls


def resume_turn(harness, run):
    """저장된 대화와 진행 상태를 복원한다. 규칙·이력을 다시 읽지 않고 저장 당시의 입력에서 이어간다."""
    saved = run.checkpoint
    run.emit(
        "input.receive",
        "중단된 실행을 이어서 시작",
        "사용자가 이어서 실행을 눌렀습니다. 새 요청이 아니라 저장된 messages에서 계속합니다.",
        category="input",
        actor="user",
        input={"prompt": run.prompt, "resumed_from": run.resumed_from},
        output={"type": "resume"},
    )
    run.effective_prompt = saved["effective_prompt"]
    # 도구 핸들러와 외부 서버 연결은 파일에 저장할 수 없어 다시 준비한다.
    prepare_agents(run, harness)
    prepare_integrations(run, harness.workspace)
    for key in ("requirements", "conditions", "settings", "max_model_calls", "model_calls",
                "tool_calls", "usage", "usage_reports", "plan", "context_segments", "turn_message_offset"):
        setattr(run, key, saved[key])
    run.delegation_used = saved["delegation_used"]
    messages = saved["messages"]
    open_calls = close_open_tool_calls(run, messages)
    current = workspace_signature(harness.workspace)
    known = {item["path"]: item["sha256"] for item in saved["workspace"]["files"]}
    now = {item["path"]: item["sha256"] for item in current["files"]}
    changed = sorted(path for path in set(known) | set(now) if known.get(path) != now.get(path))
    if changed:
        before = len(messages)
        messages.append(
            {
                "role": "user",
                "content": "[이어서 실행] 마지막 저장 이후 작업 공간의 파일이 바뀌었습니다. 이전에 읽은 내용과 다를 수 있습니다: "
                + ", ".join(changed[:20]),
            }
        )
        record_message(run, messages, before, "harness", "재개 시 파일 변경 안내")
    code_changed = bool(
        saved["implementation"]
        and run.implementation
        and saved["implementation"] != run.implementation.get("version")
    )
    run.emit(
        "run.resume",
        "저장된 상태에서 이어서 실행",
        "하네스가 가진 대화와 진행 상태만 복원했습니다. 모델의 내부 계산 상태는 저장하지 않으므로 다음 응답이 중단 전에 나올 응답과 같다고 보장하지 않습니다.",
        category="memory",
        status="warning" if open_calls or changed or code_changed else "ok",
        input={"resumed_from": run.resumed_from, "saved_at": saved["saved_at"]},
        output={
            "restored_messages": len(messages),
            "model_calls_so_far": saved["model_calls"],
            "tool_calls_so_far": saved["tool_calls"],
            "next_call": saved["next_iteration"],
            "last_call": saved["last_iteration"],
            "unanswered_tool_calls": [c.get("function", {}).get("name") for c in open_calls],
            "workspace_changed_files": changed[:20],
            "harness_code_changed": code_changed,
        },
    )
    return {
        "messages": messages,
        "catalog": saved["catalog"],
        "history": harness.load_history(run),
        "initial_count": saved["initial_count"],
        "active_skills": set(saved["active_skills"]),
        "rule_folders": set(saved["rule_folders"]),
        "dirty": set(saved["dirty"]),
        "checked": set(saved["checked"]),
        "successful": saved["successful"],
        # 반복 검사는 새로 시작한다. 이전에 막힌 실행을 이어가자마자 다시 막히지 않게 한다.
        "rejected": [],
        "reviewed": saved["reviewed"],
        "view": saved["view"],
        "next_iteration": saved["next_iteration"],
        "last_iteration": saved["last_iteration"],
    }


def start_turn(harness, run):
    """새 요청: 설정·규칙·이력을 읽고 첫 모델 입력을 만든다."""
    run.emit(
        "input.receive",
        "프롬프트를 하네스가 수신",
        "위임 도구가 별도 작업 문자열을 Python 하네스에 전달했습니다. 아직 이 하위 모델을 호출하지 않았습니다."
        if run.parent_run
        else "브라우저의 POST /api/runs가 입력 문자열을 Python 하네스에 전달했습니다. 아직 모델을 호출하지 않았습니다.",
        category="input",
        actor="user",
        input={"prompt": run.prompt, "session_id": run.session_id},
        output={"type": "str", "characters": len(run.prompt)},
    )
    run.emit(
        "request.route",
        "요청 처리 함수로 진입",
        "delegate_tasks → 별도 Run → 작업자 스레드 → Harness.execute() 순서입니다."
        if run.parent_run
        else "HTTP 핸들러 → Run 생성 → 작업 스레드 → Harness.execute() 순서로 호출되었습니다.",
        category="input",
        output={
            "handler": "delegate_tasks" if run.parent_run else "Handler.do_POST",
            "target": "Harness.execute",
            "workspace": str(harness.workspace),
        },
    )
    limit = min(
        run.engine["max_tokens"],
        getattr(harness.runtime, "max_output_tokens", 1536),
    )
    run.conditions = {
        "model": harness.model,
        "base_url": harness.runtime.base_url,
        "backend": getattr(harness.runtime, "backend", "llama"),
        "temperature": run.engine["temperature"],
        "seed": run.engine["seed"],
        "max_tokens": limit,
        "effective_generation_config": {
            **run.engine,
            "max_tokens": limit,
            "parallel_tool_calls": False,
            "enable_thinking": False,
        },
        "workspace": workspace_signature(harness.workspace),
    }
    if not run.engine["use_kv"] and not hasattr(
        harness.runtime, "generate_observed"
    ):
        raise ValueError(
            "생성 중 KV를 끄는 실험은 교육용 Python 실행기에서 지원합니다."
        )
    prepare_agents(run, harness)
    settings = resolve_settings(
        run,
        harness.workspace,
        # 집중 모드·하위 에이전트처럼 실행 자체가 정한 예산은 설정 파일보다 우선한다.
        {"loop.max_model_calls": ("mode", run.max_model_calls)}
        if run.parent_run or run.mode == "deep" or run.max_model_calls != 8
        else None,
    )
    run.max_model_calls = settings["loop.max_model_calls"]
    run.emit(
        "config.apply",
        "실제 실행에 적용할 하네스 구성",
        "각 스위치가 파일 로딩, 메시지 구성, 공개 도구와 외부 연결의 실제 코드 분기를 바꿉니다.",
        category="context",
        output={"features": run.features, "conditions": run.conditions},
    )
    expand_command(run, harness.workspace)
    history = harness.load_history(run)
    history = prepare_memory(harness, run, history)
    prepare_integrations(run, harness.workspace)
    messages, catalog = build_context(run, harness.workspace, history)
    initial_count = len(messages) - 1
    run.turn_message_offset = initial_count
    recall_and_audit(run, history, messages)
    active_skills = set()
    rule_folders = set()
    dirty = set()
    checked = set()
    successful = []
    rejected = []
    run.conditions["repeat_answer_limit"] = REPEATED_ANSWER_LIMIT
    if run.requirements:
        run.emit(
            "validation.plan",
            "완료 조건 연결",
            "예제 또는 사용자가 지정한 조건입니다. 해당 도구의 실제 성공 결과가 있어야 답변을 확정합니다.",
            category="context",
            output={"requirements": run.requirements},
        )
        before = len(messages)
        constraint = {
            "role": "user",
            "content": "이 요청의 완료 조건입니다. 도구를 실제 실행하고 결과가 조건과 일치해야 합니다. "
            + json.dumps(run.requirements, ensure_ascii=False),
        }
        messages.append(constraint)
        event_id = run.emit(
            "context.requirements",
            "완료 조건을 모델 입력에 추가",
            "명시한 조건을 모델에도 전달합니다. 코드 검사는 이 지시를 따랐는지 실제 결과로 다시 확인합니다.",
            category="context",
            input={"message_count_before": before},
            output={
                "appended_message": constraint,
                "message_count_after": len(messages),
            },
        )
        record_message(run, messages, before, "harness", "완료 조건", event_id)
    return {
        "messages": messages,
        "catalog": catalog,
        "history": history,
        "initial_count": initial_count,
        "active_skills": active_skills,
        "rule_folders": rule_folders,
        "dirty": dirty,
        "checked": checked,
        "successful": successful,
        "rejected": rejected,
        "reviewed": False,
        "view": new_view(len(messages)),
        "next_iteration": 1,
        "last_iteration": run.max_model_calls - run.model_calls,
    }


def execute(harness, run):
    try:
        turn = resume_turn(harness, run) if run.checkpoint else start_turn(harness, run)
        messages, catalog, history = turn["messages"], turn["catalog"], turn["history"]
        initial_count, view = turn["initial_count"], turn["view"]
        active_skills, rule_folders = turn["active_skills"], turn["rule_folders"]
        dirty, checked = turn["dirty"], turn["checked"]
        successful, rejected = turn["successful"], turn["rejected"]
        reviewed, last_iteration = turn["reviewed"], turn["last_iteration"]
        limit = min(
            run.engine["max_tokens"],
            getattr(harness.runtime, "max_output_tokens", 1536),
        )
        workspace_state = {"files": []}

        def checkpoint(next_iteration):
            """다음에 이어갈 지점을 저장한다. 저장 실패가 실행을 멈추지는 않는다."""
            if run.parent_run:
                return None
            try:
                saved = save_checkpoint(
                    harness.state_dir,
                    run,
                    {
                        "messages": messages,
                        "catalog": catalog,
                        "initial_count": initial_count,
                        "view": view,
                        "active_skills": sorted(active_skills),
                        "rule_folders": sorted(rule_folders),
                        "dirty": sorted(dirty),
                        "checked": sorted(checked),
                        "successful": successful,
                        "reviewed": reviewed,
                        "next_iteration": next_iteration,
                        "last_iteration": last_iteration,
                        "workspace": workspace_state,
                    },
                )
            except OSError as error:
                run.emit(
                    "checkpoint.failed",
                    "실행 상태 저장 실패",
                    "실행은 계속하지만 이 지점에서 이어갈 수 없습니다.",
                    category="memory",
                    status="warning",
                    output={"error": str(error)},
                    pause=False,
                )
                return None
            if run.resumed_from:
                discard_checkpoint(harness.state_dir, run.resumed_from)
            return saved

        engine_observed = False
        for iteration in range(turn["next_iteration"], last_iteration + 1):
            if run.stopped:
                raise RunStopped()
            allowed_tools = tools_for(run)
            if not engine_observed:
                # 입력 크기 판단에 컨텍스트 창이 필요하므로 첫 호출을 준비하기 전에 읽는다.
                observe_engine(run, harness.runtime, iteration)
                engine_observed = True
            # messages는 전체 기록이다. 모델에는 필요하면 줄인 입력만 보낸다.
            request_messages, request_origin = prepare_input(
                harness, run, messages, allowed_tools, view
            )
            if not run.parent_run:
                # 이 호출을 시작하기 전의 상태를 저장한다. 여기서 끊기면 같은 호출부터 이어간다.
                workspace_state = workspace_signature(harness.workspace)
                saved = checkpoint(iteration)
                if saved:
                    run.emit(
                        "checkpoint.save",
                        "이어서 실행할 상태 저장",
                        "대화와 진행 상태를 파일에 저장했습니다. 모델의 내부 계산 상태는 포함하지 않습니다.",
                        category="memory",
                        output={
                            "path": str(saved[0]),
                            "bytes": saved[1],
                            "messages": len(messages),
                            "next_call": iteration,
                        },
                        pause=False,
                    )
                try:
                    run.save(harness.state_dir / "traces")
                except OSError:
                    pass  # 다음 저장 지점에서 다시 시도한다. 기록 저장 실패가 실행을 멈추면 안 된다.
            streaming = hasattr(harness.runtime, "stream_json") and run.engine["stream"]
            payload = {
                "model": harness.model,
                "messages": request_messages,
                "tools": allowed_tools,
                "stream": streaming,
                "temperature": run.engine["temperature"],
                "max_tokens": limit,
                "seed": run.engine["seed"],
                "top_k": run.engine["top_k"],
                "top_p": run.engine["top_p"],
                "parallel_tool_calls": False,
                "chat_template_kwargs": {"enable_thinking": False},
                "cache_prompt": run.engine["cache_prompt"],
            }
            if hasattr(harness.runtime, "stream_json"):
                probabilities = not (streaming and allowed_tools)
                payload.update(
                    {
                        "timings_per_token": True,
                        "logprobs": probabilities,
                        "verbose": True,
                        "return_tokens": True,
                    }
                )
                if probabilities:
                    payload["top_logprobs"] = 3
                if streaming:
                    payload.update(
                        {
                            "stream_options": {"include_usage": True},
                            "return_progress": True,
                        }
                    )
                if not probabilities:
                    run.emit(
                        "runtime.capability",
                        "도구+스트림 모드의 후보 확률 제한",
                        "현재 llama.cpp는 tools+stream+logprobs를 동시에 지원하지 않습니다. 실제 도구 실행과 스트림을 유지하며 후보를 만들어 채우지 않습니다. 전체 응답 모드로 실행하면 도구 생성의 후보 확률도 관찰할 수 있습니다.",
                        category="model",
                        actor="runtime",
                        output={
                            "call": iteration,
                            "stream": streaming,
                            "tools": len(allowed_tools),
                            "logprobs": False,
                            "reason": "llama.cpp: logprobs is not supported with tools + stream",
                        },
                    )
            run.conditions.setdefault("model_input_sha256", []).append(
                input_fingerprint(payload)
            )
            direct = hasattr(harness.runtime, "generate_observed")
            run.emit(
                "model.request",
                f"모델 호출 #{iteration} · 입력 확정",
                "교육용 실행기의 Python 함수에 이 객체를 전달합니다. HTTP 요청을 보내지 않습니다."
                if direct
                else "이 JSON을 로컬 추론 API에 전달합니다. 템플릿·토큰 진단 뒤 실제 생성 요청을 시작하며 messages와 tools 전체를 확인할 수 있습니다.",
                actor="harness",
                category="model",
                input=payload,
                output={
                    "method": "Python call" if direct else "POST",
                    "url": "ReferenceRuntime.generate_observed"
                    if direct
                    else harness.runtime.base_url + "/v1/chat/completions",
                    "serialized_body_bytes": len(json.dumps(payload).encode("utf-8")),
                    "active_skills": sorted(active_skills),
                    "segments": describe_context(
                        run,
                        request_messages,
                        allowed_tools,
                        view_segments(run, request_messages, request_origin, view),
                    ),
                    "message_count": len(request_messages),
                    "transcript_message_count": len(messages),
                },
            )
            with inference_turn(harness, run, iteration):
                preview = preview_input(run, harness.runtime, payload, iteration)
                observe_slots(run, harness.runtime, iteration, "생성 전")
                response = generate(run, harness.runtime, payload, iteration)
                observe_slots(run, harness.runtime, iteration, "생성 후")
            run.model_calls += 1
            remember_measure(view, response, payload)
            if response.get("system_fingerprint"):
                run.conditions["engine_fingerprint"] = response["system_fingerprint"]
            run.add_usage(response.get("usage"))
            response_event = run.emit(
                "model.response",
                f"모델 호출 #{iteration} · {'SSE를 합친 응답' if streaming else '원본 응답'}",
                "원본 SSE 프레임을 합쳐 복원한 응답입니다. 각 원본 조각은 별도 이벤트에 있으며 여기서 도구 요청과 최종 답변을 구분합니다."
                if streaming
                else "로컬 실행기의 실제 JSON 응답입니다.",
                actor="llm",
                category="model",
                output=response,
            )
            audit_response(run, response, preview, iteration)
            try:
                choice = response["choices"][0]
                message = choice["message"]
                calls = message.get("tool_calls") or []
            except (KeyError, IndexError, TypeError, AttributeError) as exc:
                raise RuntimeFailure("모델 응답에 유효한 message가 없습니다.") from exc
            if choice.get("finish_reason") == "length":
                raise RuntimeFailure(
                    f"모델이 출력 토큰 한도 {limit}에 도달했습니다. 실행 설정에서 한도를 늘리거나 요청을 짧게 나누세요. 이 실행을 완료로 표시하지 않습니다."
                )
            if not calls:
                answer = message.get("content") or ""
                if not answer.strip():
                    raise RuntimeFailure(
                        "모델이 최종 답변이나 도구 요청을 반환하지 않았습니다."
                    )
                outstanding = final_gate(
                    run, dirty, checked, choice.get("finish_reason")
                )
                missing = requirement_gate(run, successful)
                if outstanding or missing:
                    if repeated_rejection(run, rejected, message, missing, outstanding):
                        raise RuntimeFailure(
                            f"같은 미검증 답변이 {REPEATED_ANSWER_LIMIT}회 반복됐고 새 도구 호출이 없어 중단했습니다. "
                            "필수 작업 결과는 아직 없습니다. 요청·도구 설정을 확인하고 다시 실행하세요."
                        )
                    correction = (
                        "아직 실제 도구 결과로 확인하지 못한 조건이 있습니다. 아래 목록은 실행 결과가 아니라 남은 요구 사항입니다. "
                        "지금 필요한 도구를 실제로 호출하고 반환값을 확인하세요. 수행했다고 말하는 것으로 대체할 수 없습니다: "
                        + json.dumps(missing, ensure_ascii=False)
                    )
                    if outstanding:
                        correction += (
                            " 수정한 파일의 check_file 검사도 필요합니다: "
                            + ", ".join(sorted(outstanding))
                        )
                    before = len(messages)
                    messages.extend(
                        [
                            message,
                            {
                                "role": "user",
                                "content": correction,
                            },
                        ]
                    )
                    record_message(
                        run, messages, before, "model", "모델 응답", response_event
                    )
                    event_id = run.emit(
                        "context.repair",
                        "완료 조건 미충족 → 모델에 보완 요청",
                        "답변을 최종 확정하지 않고 부족한 실제 행동을 다음 모델 입력에 추가했습니다.",
                        category="context",
                        input={
                            "message_count_before": before,
                            "rejected_answer": answer,
                        },
                        output={
                            "appended_message": messages[-1],
                            "message_count_after": len(messages),
                        },
                    )
                    record_message(
                        run,
                        messages,
                        before + 1,
                        "harness",
                        "미충족 조건 보완 요청",
                        event_id,
                    )
                    continue
                if run.mode == "deep" and not reviewed:
                    reviewed = True
                    before = len(messages)
                    messages.extend(
                        [message, {"role": "user", "content": run.review_instruction}]
                    )
                    record_message(
                        run, messages, before, "model", "모델 응답", response_event
                    )
                    event_id = run.emit(
                        "agent.review",
                        "집중 모드 · 답변과 근거 다시 점검",
                        "같은 에이전트의 실제 대화에 점검 지시를 넣고 한 번 더 모델을 호출합니다. 숨겨진 사고를 표시하거나 모델 종류를 바꾸는 기능이 아닙니다.",
                        category="context",
                        input={"candidate_answer": answer},
                        output={
                            "appended_message": messages[-1],
                            "message_count_after": len(messages),
                        },
                    )
                    record_message(
                        run,
                        messages,
                        before + 1,
                        "harness",
                        "집중 모드 점검 지시",
                        event_id,
                    )
                    continue
                messages.append({"role": "assistant", "content": answer})
                record_message(
                    run,
                    messages,
                    len(messages) - 1,
                    "model",
                    "모델 응답",
                    response_event,
                )
                run.answer = answer
                run.emit(
                    "output.answer",
                    "화면에 표시할 최종 답변 확정",
                    "모델의 최종 content를 API 응답에 담습니다. 브라우저는 실행 기록을 수신한 뒤 본문을 표시합니다.",
                    actor="llm",
                    category="output",
                    output={"answer": answer},
                )
                harness.save_history(run, history, messages[initial_count:])
                run.emit(
                    "run.complete",
                    "하네스 실행 완료",
                    "완료 검사와 대화 이력 저장을 마쳤습니다. 실행 결과와 관찰 기록을 보존합니다.",
                    category="output",
                    output={
                        "status": "completed",
                        "model_calls": run.model_calls,
                        "tool_calls": run.tool_calls,
                        "answer_characters": len(run.answer),
                    },
                    pause=False,
                )
                with run.lock:
                    run.status = "completed"
                discard_checkpoint(harness.state_dir, run.id)
                break
            if not isinstance(calls, list) or len(calls) > 6:
                raise RuntimeFailure(
                    "한 응답의 도구 호출 수가 허용 범위를 벗어났습니다."
                )
            messages.append(message)
            record_message(
                run,
                messages,
                len(messages) - 1,
                "model",
                "모델 도구 요청",
                response_event,
            )
            checkpoint(iteration + 1)
            seen_ids = [c.get("id") for c in calls if isinstance(c, dict)]
            if len(seen_ids) != len(set(seen_ids)):
                raise RuntimeFailure(
                    "모델이 같은 tool_call id를 두 번 사용했습니다. 결과와 요청을 짝지을 수 없어 실행하지 않습니다."
                )
            touched = []
            for call in calls:
                if (
                    not isinstance(call, dict)
                    or not isinstance(call.get("id"), str)
                    or call.get("type") != "function"
                    or not isinstance(call.get("function"), dict)
                ):
                    raise RuntimeFailure("모델의 도구 호출 형식이 올바르지 않습니다.")
                run.tool_calls += 1
                result_message = harness.dispatch_tool(
                    run, catalog, call, active_skills
                )
                result = json.loads(result_message["content"])
                name = call.get("function", {}).get("name")
                if (
                    "error" not in result
                    and result.get("executed_successfully", True)
                    and (name != "check_file" or result.get("passed"))
                ):
                    successful.append(
                        {
                            "name": name,
                            "arguments": json.loads(call["function"]["arguments"]),
                            "event_id": run.events[-1]["id"],
                            "result": result,
                        }
                    )
                if (
                    name in {"read_file", "write_file", "check_file"}
                    and "error" not in result
                    and isinstance(result.get("path"), str)
                ):
                    touched.append(result["path"])
                if name == "write_file" and "written_bytes" in result:
                    dirty.add(result["path"])
                    checked.discard(result["path"])
                if name == "check_file" and result.get("passed"):
                    checked.add(result["path"])
                before = len(messages)
                messages.append(result_message)
                record_message(
                    run, messages, before, "tool", "도구 결과", run.events[-1]["id"]
                )
                event_id = run.emit(
                    "context.append",
                    "도구 결과를 다음 모델 입력에 추가",
                    "assistant의 도구 요청과 tool 결과를 함께 보존합니다. 다음 호출의 messages가 이만큼 늘어납니다.",
                    category="context",
                    input={"message_count_before": before},
                    output={
                        "appended_message": result_message,
                        "message_count_after": len(messages),
                        "segments": describe_context(run, messages, allowed_tools),
                    },
                )
                checkpoint(iteration + 1)
            load_directory_rules(
                run, harness.workspace, touched, rule_folders, messages
            )
        else:
            raise RuntimeFailure(
                f"모델 호출 {run.max_model_calls}회 제한에 도달했습니다. 최종 완료 조건을 만족하지 못했습니다."
            )
    except RunStopped:
        with run.lock:
            run.status = "stopped"
    except Exception as exc:
        with run.lock:
            # str(exc)가 빈 문자열인 예외(StopIteration 등)도 실패 원인을 남긴다.
            run.error = (
                str(exc) or type(exc).__name__
                if isinstance(exc, (RuntimeFailure, ValueError))
                else f"{type(exc).__name__}: {exc}"
            )
        try:
            run.emit(
                "run.error",
                "실행을 완료하지 못했습니다",
                "실제 오류를 기록하고 중단합니다. 성공한 응답으로 대체하지 않습니다.",
                category="output",
                status="error",
                output={"error": run.error, **unexpected_location(exc)},
                pause=False,
            )
        except RunStopped:
            pass
        with run.lock:
            run.status = "failed"
    finally:
        run.finished = time.perf_counter()
        try:
            if not run.parent_run:
                trim_checkpoints(harness.state_dir)
            run.save(harness.state_dir / "traces")
        finally:
            with run.condition:
                run.condition.notify_all()
