"""추론 경계를 관찰한다. 진단·실제 SSE·실행기 보고값·계산한 설명을 구분한다."""

import contextlib
import hashlib
import json
import math

from .runtime import RuntimeFailure


def engine_options(value=None):
    value = {} if value is None else value
    defaults = {
        "cache_prompt": True,
        "stream": True,
        "use_kv": True,
        "capture_layer": 0,
        "max_tokens": 256,
        "temperature": 0.2,
        "top_k": 40,
        "top_p": 0.95,
        "seed": 42,
    }
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError("지원하지 않는 실행기 옵션입니다.")
    result = {**defaults, **value}
    for key in ("cache_prompt", "stream", "use_kv"):
        if type(result[key]) is not bool:
            raise ValueError(key + "는 true 또는 false여야 합니다.")
    for key, low, high in [
        ("capture_layer", 0, 127),
        ("max_tokens", 1, 1024),
        ("top_k", 0, 200),
        ("seed", 0, 2**32 - 1),
    ]:
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f"{key} 범위는 {low}~{high} 정수입니다.")
    for key, low, high in [("temperature", 0, 2), ("top_p", 1e-6, 1)]:
        item = result[key]
        if (
            type(item) not in (int, float)
            or not math.isfinite(item)
            or not low <= item <= high
        ):
            raise ValueError(f"{key} 범위는 {low}~{high}입니다.")
    if not result["use_kv"]:
        result["cache_prompt"] = False
    return result


def input_fingerprint(payload):
    value = {
        key: payload.get(key)
        for key in ("model", "messages", "tools", "chat_template_kwargs")
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def observe_engine(run, runtime, iteration):
    if not hasattr(runtime, "observe"):
        return
    try:
        props = runtime.request("/props")
        run.emit(
            "runtime.info",
            "실행기 설정 확인",
            "모델·컨텍스트·설정의 실제 값입니다. HTTP 실행기는 /props, 교육용 실행기는 Python 객체에서 읽습니다. 가중치·대화 이력·KV 캐시는 서로 다릅니다.",
            category="model",
            actor="runtime",
            input={
                "operation": "properties",
                "transport": getattr(runtime, "transport", "http"),
            },
            output={"call": iteration, "props": props},
        )
    except RuntimeFailure as exc:
        run.emit(
            "runtime.info.unavailable",
            "실행기 설정 진단 미지원",
            "생성 요청은 계속 진행하며 얻지 못한 진단값은 비워 둡니다.",
            category="model",
            actor="runtime",
            status="warning",
            output={"call": iteration, "error": str(exc)},
        )


def observe_slots(run, runtime, iteration, phase):
    if not hasattr(runtime, "observe"):
        return
    try:
        slots = runtime.observe("/slots")
        run.emit(
            "runtime.slots",
            f"실행 상태 · {phase}",
            "HTTP 실행기는 /slots, 교육용 실행기는 실제 캐시 객체의 상태입니다. n_prompt_tokens는 시퀀스 길이이며 메모리 바이트 수와 구분합니다.",
            category="model",
            actor="runtime",
            output={
                "call": iteration,
                "phase": phase,
                "slots": slots,
                "transport": getattr(runtime, "transport", "http"),
            },
        )
    except RuntimeFailure as exc:
        run.emit(
            "runtime.slots.unavailable",
            "슬롯 상태 진단 미지원",
            "추론은 계속하며 슬롯 상태를 만들지 않습니다.",
            category="model",
            actor="runtime",
            status="warning",
            output={"call": iteration, "error": str(exc)},
        )


def preview_input(run, runtime, payload, iteration):
    preview = {"call": iteration, "prompt": None, "tokens": []}
    try:
        rendered = runtime.request("/apply-template", payload)
        prompt = rendered.get("prompt")
        preview["prompt"] = prompt
        run.emit(
            "model.template",
            "메시지·도구 정의를 대화 템플릿으로 변환",
            "실행기의 실제 템플릿 변환 결과입니다. 교육용은 tokenizer.apply_chat_template를 직접 호출합니다. 생성 뒤 사용한 입력 문자열과도 비교합니다.",
            category="model",
            actor="runtime",
            input={
                "operation": "apply_template",
                "transport": getattr(runtime, "transport", "http"),
                "call": iteration,
            },
            output={
                **rendered,
                "call": iteration,
                "utf8_bytes": len(prompt.encode("utf-8"))
                if isinstance(prompt, str)
                else None,
            },
        )
        if not isinstance(prompt, str) or not prompt:
            return preview
        result = runtime.request(
            "/tokenize",
            {
                "content": prompt,
                "add_special": False,
                "parse_special": True,
                "with_pieces": True,
            },
        )
        tokens = result.get("tokens", [])
        preview["tokens"] = tokens
        pieces = []
        for token in tokens:
            piece = token.get("piece") if isinstance(token, dict) else None
            if isinstance(piece, str):
                pieces.append(piece.encode("utf-8"))
            elif isinstance(piece, list) and all(
                type(byte) is int and 0 <= byte <= 255 for byte in piece
            ):
                pieces.append(bytes(piece))
            else:
                pieces = []
                break
        rebuilt = b"".join(pieces)
        prompt_bytes = prompt.encode("utf-8")
        exact = bool(pieces) and rebuilt == prompt_bytes
        requested = run.effective_prompt.encode("utf-8")
        first = prompt_bytes.find(requested) if requested else -1
        ambiguous = first >= 0 and prompt_bytes.find(requested, first + 1) >= 0
        position = first if not ambiguous else -1
        # 요약 호출(call 0)은 답변 입력과 접두부가 달라 비교 대상에서 뺀다.
        answers = [item for item in run.token_previews if item.get("call") != 0]
        previous = answers[-1]["tokens"] if answers else []
        ids = lambda items: [
            item.get("id") if isinstance(item, dict) else item for item in items
        ]
        prefix = 0
        for a, b in zip(ids(previous), ids(tokens)):
            if a != b:
                break
            prefix += 1
        preview.update(
            {
                "pieces_match_utf8": exact,
                "previous_input_common_prefix": prefix if previous else None,
            }
        )
        run.emit(
            "model.tokenize",
            "UTF-8 문자열을 토큰 조각과 숫자 ID로 변환",
            "실행기의 /tokenize 결과입니다. 한글 한 글자가 여러 토큰의 바이트에 나뉠 수 있으며, 숫자 ID는 단어 번호나 문장의 의미 점수가 아닙니다. 이전 입력의 공통 접두부 계산은 실행기의 실제 캐시 히트와 별도입니다.",
            category="model",
            actor="runtime",
            input={
                "call": iteration,
                "characters": len(prompt),
                "utf8_bytes": len(prompt_bytes),
                "first_96_bytes_hex": prompt_bytes[:96].hex(" "),
                "add_special": False,
                "parse_special": True,
            },
            output={
                **result,
                "call": iteration,
                "token_count": len(tokens),
                "pieces_match_utf8": exact,
                "user_prompt_byte_range": [position, position + len(requested)]
                if exact and position >= 0
                else None,
                "user_prompt_mapping": "ambiguous"
                if ambiguous
                else "unique"
                if first >= 0
                else "absent",
                "previous_input_common_prefix": preview["previous_input_common_prefix"],
            },
        )
        run.token_previews.append(preview)
        if hasattr(runtime, "observe"):
            try:
                reverse = runtime.request("/detokenize", {"tokens": ids(tokens)})
                run.emit(
                    "model.detokenize",
                    "같은 토큰 ID를 다시 문자열로 복원",
                    "진단용 /detokenize의 실제 결과입니다. 특수 기호 보존 방식에 따라 원문과 다를 수 있으며 일치 여부를 그대로 표시합니다.",
                    category="model",
                    actor="runtime",
                    input={"call": iteration, "token_ids": ids(tokens)},
                    output={
                        "call": iteration,
                        **reverse,
                        "matches_template": reverse.get("content") == prompt,
                    },
                )
            except RuntimeFailure as exc:
                run.emit(
                    "model.detokenize.unavailable",
                    "역토큰화 진단 미지원",
                    "생성은 계속합니다.",
                    category="model",
                    actor="runtime",
                    status="warning",
                    output={"call": iteration, "error": str(exc)},
                )
    except RuntimeFailure as exc:
        run.emit(
            "model.preview.unavailable",
            "템플릿·토큰 진단 API 미지원",
            "생성 요청은 계속 기록하며 토큰 값을 추정하지 않습니다.",
            category="model",
            actor="runtime",
            status="warning",
            output={"call": iteration, "error": str(exc)},
        )
    return preview


class ChatAssembly:
    """실제 delta들을 합친 메시지. 원본 프레임은 이벤트에 별도로 남긴다."""

    def __init__(self):
        self.message = {"role": "assistant", "content": ""}
        self.calls = {}
        self.finish = None
        self.metadata = {}
        self.logprobs = []
        self.frames = 0
        self.first_payload_ms = None
        self.first_payload_excluding_pause_ms = None

    def add(self, frame, arrival_ms, arrival_excluding_pause_ms=None):
        self.frames += 1
        for key in (
            "id",
            "created",
            "model",
            "system_fingerprint",
            "usage",
            "timings",
            "__verbose",
        ):
            if key in frame:
                self.metadata[key] = frame[key]
        choices = frame.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            raise RuntimeFailure("단일 선택 SSE 응답이 필요합니다.")
        for choice in choices:
            if choice.get("index", 0) != 0:
                raise RuntimeFailure("알 수 없는 SSE 선택 인덱스입니다.")
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise RuntimeFailure("SSE delta가 객체가 아닙니다.")
            for key in ("content", "reasoning_content"):
                text = delta.get(key)
                if text is not None:
                    if not isinstance(text, str):
                        raise RuntimeFailure("SSE 텍스트 조각이 문자열이 아닙니다.")
                    self.message[key] = self.message.get(key, "") + text
            fragments = delta.get("tool_calls") or []
            if not isinstance(fragments, list) or len(fragments) > 6:
                raise RuntimeFailure("SSE 도구 조각 형식이 올바르지 않습니다.")
            for fragment in fragments:
                index = fragment.get("index")
                if type(index) is not int or not 0 <= index < 6:
                    raise RuntimeFailure("SSE 도구 인덱스가 범위를 벗어났습니다.")
                call = self.calls.setdefault(
                    index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if fragment.get("id"):
                    if call["id"] and call["id"] != fragment["id"]:
                        raise RuntimeFailure("SSE 도구 ID가 중간에 바뀌었습니다.")
                    call["id"] = fragment["id"]
                if fragment.get("type") and fragment["type"] != "function":
                    raise RuntimeFailure("지원하지 않는 SSE 도구 형식입니다.")
                for key in ("name", "arguments"):
                    text = (fragment.get("function") or {}).get(key)
                    if text is not None:
                        if not isinstance(text, str):
                            raise RuntimeFailure("SSE 도구 조각은 문자열이어야 합니다.")
                        call["function"][key] += text
            probabilities = (choice.get("logprobs") or {}).get("content") or []
            self.logprobs.extend(probabilities)
            if self.first_payload_ms is None and (
                delta.get("content") or fragments or probabilities
            ):
                self.first_payload_ms = arrival_ms
                self.first_payload_excluding_pause_ms = (
                    arrival_excluding_pause_ms if arrival_excluding_pause_ms is not None else arrival_ms
                )
            if choice.get("finish_reason") is not None:
                self.finish = choice["finish_reason"]

    def response(self):
        if self.finish is None:
            raise RuntimeFailure("종료 사유 없이 SSE 응답이 끝났습니다.")
        if self.calls:
            self.message["tool_calls"] = [
                self.calls[index] for index in sorted(self.calls)
            ]
        return {
            **self.metadata,
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": self.finish,
                    "message": self.message,
                    "logprobs": {"content": self.logprobs},
                }
            ],
            "_assembly": {
                "source": "original_sse_frames",
                "frame_count": self.frames,
                "first_payload_received_ms": self.first_payload_ms,
                "first_payload_received_ms_excluding_pause": self.first_payload_excluding_pause_ms,
                "timing_basis": "하네스가 프레임을 읽은 시각. _excluding_pause는 단계 정지 시간을 뺀 값이며 서버 계산 시간은 timings에 따로 있다.",
                "logprob_entries": len(self.logprobs),
                "observed_token_ids": len(self.logprobs),
                "observed_token_ids_basis": "logprobs 항목 수(llama.cpp는 불완전한 UTF-8 토큰을 보류·병합해 생성 토큰 수보다 적을 수 있음)",
            },
        }


def generate(run, runtime, payload, iteration):
    if hasattr(runtime, "generate_observed"):
        return runtime.generate_observed(run, payload, iteration)
    if hasattr(runtime, "stream_json"):
        run.emit(
            "http.inference.send",
            "실제 생성 HTTP 요청 시작",
            "이제 진단 API가 아니라 /v1/chat/completions로 POST합니다. 응답 미디어 타입과 계산 결과는 이후 이벤트에 기록합니다.",
            category="model",
            output={
                "call": iteration,
                "url": runtime.base_url + "/v1/chat/completions",
                "body_bytes": len(json.dumps(payload).encode("utf-8")),
            },
        )
    if not hasattr(runtime, "stream_json") or not payload.get("stream"):
        result = runtime.request("/v1/chat/completions", payload)
        if hasattr(runtime, "last_http"):
            run.emit(
                "http.inference.json",
                "실행기의 전체 JSON 응답 수신",
                "실제 HTTP 응답 상태와 Content-Type입니다. 이 방식은 생성 중간 조각이 아니라 완료된 응답을 한 번 받습니다.",
                category="model",
                actor="runtime",
                output={"call": iteration, **runtime.last_http},
            )
        probabilities = result.get("choices", [{}])[0].get("logprobs") or {}
        if probabilities.get("content"):
            native = result.get("__verbose") or {}
            run.emit(
                "model.decode.report",
                "전체 응답에서 생성 토큰·후보 확률 관측",
                "하나의 실제 JSON 응답에서 생성 관측값을 꺼낸 것입니다. 생성이 이 이벤트 시각에 토큰마다 진행된 것처럼 재구성하지 않습니다. 다음 단계에서 역할·도구 메시지를 사용합니다.",
                category="model",
                actor="llm",
                output={
                    "call": iteration,
                    "logprobs": probabilities,
                    "tokens": native.get("tokens"),
                    "usage": result.get("usage"),
                    "timings": result.get("timings"),
                    "generation_settings": native.get("generation_settings"),
                },
            )
        return result
    assembly = ChatAssembly()
    pause_at_start = run.paused_seconds
    try:
        _consume_stream(run, runtime, payload, iteration, assembly, pause_at_start)
    except Exception as error:
        # 스트림이 중간에 실패해도 그 시점까지 합친 메시지·도구 조각을 남긴다. 성공한 응답처럼 쓰지는 않는다.
        if assembly.frames:
            run.record_interrupted(
                "model.stream.partial",
                "스트림 실패 시점까지 합친 부분 응답",
                "스트림이 끝나기 전에 오류가 났습니다. 지금까지 받은 조각을 합친 상태이며 모델의 최종 응답이 아닙니다.",
                category="model",
                output={
                    "call": iteration,
                    "error": str(error),
                    "frames": assembly.frames,
                    "message_so_far": assembly.message,
                    "tool_calls_so_far": [assembly.calls[i] for i in sorted(assembly.calls)],
                    "finish_reason": assembly.finish,
                },
            )
        raise
    response = assembly.response()
    response["_assembly"]["pause_during_stream_seconds"] = round(
        run.paused_seconds - pause_at_start, 3
    )
    run.emit(
        "model.stream.assemble",
        "전송 조각을 모델 메시지·도구 요청으로 복원",
        "content는 문자열 조각을 순서대로 합치고 tool_calls는 index별로 id·이름·인자를 복원했습니다. 이 객체는 하네스가 원본 SSE를 합친 결과입니다.",
        category="model",
        output={
            "call": iteration,
            "message": response["choices"][0]["message"],
            "assembly": response["_assembly"],
        },
    )
    return response


def _consume_stream(run, runtime, payload, iteration, assembly, pause_at_start):
    with contextlib.closing(
        runtime.stream_json("/v1/chat/completions", payload)
    ) as frames:
        for received in frames:
            if received.get("opened"):
                run.emit(
                    "http.inference.open",
                    "실행기의 SSE HTTP 응답 연결 확인",
                    "서버가 실제로 반환한 HTTP 상태와 미디어 타입입니다. 아직 최종 모델 답변을 받은 상태는 아닙니다.",
                    category="model",
                    actor="runtime",
                    output={"call": iteration, **received},
                )
                continue
            if received.get("done"):
                run.emit(
                    "model.stream.done",
                    "SSE 종료 표식 [DONE] 수신",
                    "HTTP 스트림의 종료 표식입니다. 모델의 종료 사유와 구분하며, 도구 인자는 전체 조각을 합친 뒤 검사합니다.",
                    category="model",
                    actor="runtime",
                    output={"call": iteration, **received},
                )
                break
            frame = received["data"]
            # 수신 시각은 하네스가 읽은 시각이라 단계 정지를 포함한다. 정지를 뺀 값도 함께 남긴다.
            paused_ms = round((run.paused_seconds - pause_at_start) * 1000, 3)
            assembly.add(frame, received["arrival_ms"], arrival_excluding_pause_ms=received["arrival_ms"] - paused_ms)
            if "prompt_progress" in frame:
                kind, title, actor = (
                    "runtime.prefill",
                    "캐시 접두부 재사용·새 입력 프리필 진행",
                    "runtime",
                )
            elif frame.get("usage"):
                kind, title, actor = (
                    "model.stream.usage",
                    "스트림의 최종 토큰 사용량·시간 수신",
                    "runtime",
                )
            elif any(
                choice.get("delta", {}).get("content")
                or choice.get("delta", {}).get("tool_calls")
                or choice.get("logprobs")
                for choice in frame.get("choices", [])
            ):
                kind, title, actor = (
                    "model.stream",
                    "모델 출력 조각·토큰 후보 수신",
                    "llm",
                )
            else:
                kind, title, actor = (
                    "model.stream.control",
                    "역할·종료 사유 등 스트림 제어 조각 수신",
                    "runtime",
                )
            run.emit(
                kind,
                title,
                "실제 SSE data 프레임입니다. 전송 조각과 토큰은 일대일이 아닙니다. 수신 시간은 하네스에서 측정하며 단계 정지·버퍼링의 영향을 받을 수 있고, 실행기 계산 시간은 timings에 따로 있습니다.",
                category="model",
                actor=actor,
                input={"raw_sse_data": received["raw"]},
                output={
                    "call": iteration,
                    "frame": frame,
                    "arrival_ms": received["arrival_ms"],
                    "arrival_ms_excluding_pause": round(received["arrival_ms"] - paused_ms, 3),
                    "frame_bytes": received["frame_bytes"],
                    "frame_index": assembly.frames,
                    "assembled_so_far": {
                        "content_characters": len(assembly.message.get("content", "")),
                        "tool_call_fragments": len(assembly.calls),
                    },
                },
            )
        else:
            raise RuntimeFailure("SSE 종료 표식을 받지 못했습니다.")


def audit_response(run, response, preview, iteration, payload=None):
    usage = response.get("usage") or {}
    timings = response.get("timings") or {}
    details = usage.get("prompt_tokens_details") or {}
    if "cached_tokens" in details:
        cached, cached_source = details.get("cached_tokens"), "usage.prompt_tokens_details.cached_tokens"
    elif "cache_n" in timings:
        cached, cached_source = timings.get("cache_n"), "timings.cache_n"
    else:
        cached, cached_source = None, None
    total = usage.get("prompt_tokens")
    fresh = timings.get("prompt_n")
    actual_prompt = (response.get("__verbose") or {}).get("prompt")
    # 생성 쪽 대조: usage.completion_tokens, timings.predicted_n, logprobs 항목 수. EOS 1개 차이는 흔하다.
    completion = usage.get("completion_tokens")
    predicted_n = timings.get("predicted_n")
    logprob_entries = len(
        ((response.get("choices") or [{}])[0].get("logprobs") or {}).get("content") or []
    )
    generation_count_matches = None
    if type(completion) is int and logprob_entries:
        generation_count_matches = logprob_entries in (completion, completion - 1)
    stream_assembly = response.get("_assembly") or {}
    run.emit(
        "runtime.cache",
        "이번 호출의 계산량·캐시 요약",
        "cached_tokens/cache_n은 요청 간 재사용 계수, prompt_n은 새 입력 계산량입니다. cache_prompt와 생성 중 use_kv는 다른 설정입니다. 교육용의 실제 텐서 값은 reference.cache.write에, 외부 실행기의 관측값은 해당 응답에 기록됩니다.",
        category="model",
        actor="runtime",
        output={
            "call": iteration,
            "cache_prompt": (payload or run.engine)["cache_prompt"],
            "prompt_tokens": total,
            "cached_tokens": cached,
            "cached_tokens_source": cached_source,
            "fresh_prompt_tokens": fresh,
            "completion_tokens": usage.get("completion_tokens"),
            "predicted_n": predicted_n,
            "logprob_entries": logprob_entries or None,
            "generation_count_matches": generation_count_matches,
            "generation_count_note": "logprobs 항목 수가 completion_tokens보다 적으면 실행기가 불완전한 UTF-8 토큰을 보류해 병합했거나 EOS가 빠진 것이다. 생성 토큰을 토큰별 화면이 모두 보여주지는 못한다."
            if generation_count_matches is False
            else None,
            "pause_during_stream_seconds": stream_assembly.get("pause_during_stream_seconds"),
            "value_sources": {
                "reported": ["prompt_tokens", "cached_tokens", "fresh_prompt_tokens", "completion_tokens", "predicted_n", "prompt_ms", "decode_ms", "decode_tokens_per_second", "effective_generation_settings", "stop_type", "truncated"],
                "diagnostic": ["diagnostic_token_count"],
                "derived": ["accounting_matches", "diagnostic_count_matches", "template_matches_generation_prompt", "generation_count_matches", "logprob_entries"],
            },
            "accounting_matches": cached + fresh == total
            if all(type(item) is int for item in (cached, fresh, total))
            else None,
            "prompt_ms": timings.get("prompt_ms"),
            "decode_ms": timings.get("predicted_ms"),
            "decode_tokens_per_second": timings.get("predicted_per_second"),
            "diagnostic_token_count": len(preview["tokens"])
            if preview["tokens"]
            else None,
            "diagnostic_count_matches": len(preview["tokens"]) == total
            if preview["tokens"] and total is not None
            else None,
            "template_matches_generation_prompt": preview["prompt"] == actual_prompt
            if actual_prompt is not None and preview["prompt"] is not None
            else None,
            "actual_generation_prompt": actual_prompt,
            "effective_generation_settings": (response.get("__verbose") or {}).get(
                "generation_settings"
            ),
            "stop_type": (response.get("__verbose") or {}).get("stop_type"),
            "truncated": (response.get("__verbose") or {}).get("truncated"),
            "reported_timings": timings,
            "reported_usage": usage,
        },
    )
