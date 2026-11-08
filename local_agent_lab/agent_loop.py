"""사용자 입력부터 도구 실행·검사·완료까지의 에이전트 루프."""

import json
import time
from .trace import RunStopped
from .runtime import RuntimeFailure
from .features import workspace_signature
from .context import (
    expand_command,
    build_context,
    tools_for,
    describe_context,
    record_message,
)
from .policy import (
    final_gate,
    requirement_gate,
    repeated_rejection,
    REPEATED_ANSWER_LIMIT,
)
from .integrations import prepare as prepare_integrations
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


def execute(harness, run):
    try:
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
        reviewed = False
        for iteration in range(1, run.max_model_calls - run.model_calls + 1):
            if run.stopped:
                raise RunStopped()
            allowed_tools = tools_for(run)
            streaming = hasattr(harness.runtime, "stream_json") and run.engine["stream"]
            payload = {
                "model": harness.model,
                "messages": messages,
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
                    "segments": describe_context(run, messages, allowed_tools),
                    "message_count": len(messages),
                },
            )
            with inference_turn(harness, run, iteration):
                if iteration == 1:
                    observe_engine(run, harness.runtime, iteration)
                preview = preview_input(run, harness.runtime, payload, iteration)
                observe_slots(run, harness.runtime, iteration, "생성 전")
                response = generate(run, harness.runtime, payload, iteration)
                observe_slots(run, harness.runtime, iteration, "생성 후")
            run.model_calls += 1
            if response.get("system_fingerprint"):
                run.conditions["engine_fingerprint"] = response["system_fingerprint"]
            usage = response.get("usage", {})
            for key in run.usage:
                run.usage[key] += int(usage.get(key, 0) or 0)
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
        else:
            raise RuntimeFailure(
                f"모델 호출 {run.max_model_calls}회 제한에 도달했습니다. 최종 완료 조건을 만족하지 못했습니다."
            )
    except RunStopped:
        with run.lock:
            run.status = "stopped"
    except Exception as exc:
        with run.lock:
            run.error = str(exc)
        try:
            run.emit(
                "run.error",
                "실행을 완료하지 못했습니다",
                "실제 오류를 기록하고 중단합니다. 성공한 응답으로 대체하지 않습니다.",
                category="output",
                status="error",
                output={"error": str(exc)},
                pause=False,
            )
        except RunStopped:
            pass
        with run.lock:
            run.status = "failed"
    finally:
        run.finished = time.perf_counter()
        try:
            run.save(harness.state_dir / "traces")
        finally:
            with run.condition:
                run.condition.notify_all()
