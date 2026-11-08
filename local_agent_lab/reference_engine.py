"""학습용 추론 실행기.

모델의 forward는 Transformers/PyTorch가 계산한다. 입력 준비, KV 재사용,
토큰별 forward, 샘플링, 종료, 도구 표현 해석은 이 파일에서 직접 제어한다.
model.generate()를 호출하지 않는다. 이벤트 값은 실제 텐서에서 가져온다.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path

from .paths import reference_model_path
from .runtime import RuntimeFailure
from .model_files import verify_snapshot


@lru_cache(maxsize=1)
def byte_alphabet():
    """GPT-2 계열 ByteLevel 어휘의 문자 표기를 원래 바이트로 되돌린다."""
    direct = list(range(33, 127)) + list(range(161, 173)) + list(range(174, 256))
    mapping = {chr(value): value for value in direct}
    for index, value in enumerate(value for value in range(256) if value not in direct):
        mapping[chr(256 + index)] = value
    return mapping


def parse_tool_output(text):
    """이 어댑터가 지원하는 Qwen 도구 표현. 이름·권한 검사는 하네스의 다음 단계다."""
    matches = list(re.finditer(r"<tool_call>\s*(.*?)\s*</tool_call>", text, re.S))
    if "<tool_call>" in text and not matches:
        raise RuntimeFailure("도구 표현이 완성되지 않았습니다.")
    calls = []
    for match in matches:
        try:
            value = json.loads(match.group(1))
        except ValueError as exc:
            raise RuntimeFailure(
                "모델이 만든 도구 표현의 JSON이 올바르지 않습니다."
            ) from exc
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("name"), str)
            or not isinstance(value.get("arguments"), dict)
        ):
            raise RuntimeFailure(
                "도구 표현에는 name 문자열과 arguments 객체가 필요합니다."
            )
        calls.append(
            {
                "id": "ref-" + uuid.uuid4().hex[:12],
                "type": "function",
                "function": {
                    "name": value["name"],
                    "arguments": json.dumps(value["arguments"], ensure_ascii=False),
                },
            }
        )
    content = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.S).strip()
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return message


class ReferenceRuntime:
    backend = "reference"
    transport = "in_process"
    base_url = "in-process://reference"
    model_id = "Qwen3-0.6B"
    max_output_tokens = 256

    def __init__(self, model_path=None, *, autoload=True):
        self.path = Path(model_path or reference_model_path()).resolve()
        self.model = None
        self.tokenizer = None
        self.torch = None
        self.error = None
        self.loading = False
        self.cache = None
        self.cache_ids = []
        self.cache_version = 0
        self.context_limit = 4096
        self.projection_samples = {}
        self.attention_sample = None
        self.embedding_sample = None
        self._hooks = []
        self._hook_ms = 0.0
        self._capture_layer = 0
        self._run = None
        self._call = 0
        self._step = 0
        self._phase = "prefill"
        if autoload:
            self.start_loading()

    def start_loading(self):
        if self.loading or self.model is not None:
            return
        if (
            importlib.util.find_spec("torch") is None
            or importlib.util.find_spec("transformers") is None
        ):
            self.error = "교육용 실행기 설치가 필요합니다: uv sync --extra reference"
            return
        if not (self.path / "agent-lens-model.json").is_file():
            self.error = "교육용 모델을 준비하세요: python scripts/setup_reference.py"
            return
        self.loading = True
        self.error = None
        threading.Thread(
            target=self._load, name="reference-model-load", daemon=True
        ).start()

    def _load(self):
        try:
            verify_snapshot(self.path)
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer

            torch.set_num_threads(min(6, os.cpu_count() or 1))
            self.torch = torch
            self.tokenizer = AutoTokenizer.from_pretrained(
                self.path, local_files_only=True, trust_remote_code=False
            )
            self.model = AutoModelForCausalLM.from_pretrained(
                self.path,
                local_files_only=True,
                trust_remote_code=False,
                dtype=torch.float32,
                attn_implementation="eager",
            ).eval()
            self._attach_hooks()
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.loading = False

    def status(self):
        return {
            "connected": self.model is not None and not self.loading and not self.error,
            "loading": self.loading,
            "error": self.error,
            "model": self.model_id,
            "runtime": "Python / PyTorch",
            "backend": self.backend,
            "base_url": self.base_url,
            "model_path": str(self.path),
            "capabilities": {
                "tensor_values": True,
                "attention_values": True,
                "step_forward": True,
                "disable_decode_cache": True,
                "prefix_cache": True,
            },
        }

    def _ready(self):
        if self.model is None or self.tokenizer is None or self.loading or self.error:
            raise RuntimeFailure(self.error or "교육용 모델을 불러오는 중입니다.")

    def _piece(self, token_id):
        raw = self.tokenizer.convert_ids_to_tokens(int(token_id))
        if raw is None:
            return []
        alphabet = byte_alphabet()
        data = (
            bytes(alphabet[character] for character in raw)
            if all(character in alphabet for character in raw)
            else raw.encode("utf-8")
        )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return list(data)

    def request(self, route, payload=None):
        self._ready()
        if route == "/health":
            return {"status": "ok"}
        if route == "/v1/models":
            return {"data": [{"id": self.model_id}]}
        if route == "/props":
            config = self.model.config
            return {
                "backend": self.backend,
                "transport": self.transport,
                "model_alias": self.model_id,
                "model_path": str(self.path),
                "model_ftype": str(next(self.model.parameters()).dtype),
                "build_info": "Agent Lens Python reference",
                "default_generation_settings": {
                    "n_ctx": self.context_limit,
                    "params": {"samplers": ["temperature", "top_k", "top_p"]},
                },
                "chat_template": self.tokenizer.chat_template,
                "eos_token": self.tokenizer.eos_token,
                "model_config": {
                    key: getattr(config, key, None)
                    for key in (
                        "model_type",
                        "num_hidden_layers",
                        "hidden_size",
                        "num_attention_heads",
                        "num_key_value_heads",
                        "head_dim",
                        "vocab_size",
                        "max_position_embeddings",
                    )
                },
                "capabilities": self.status()["capabilities"],
            }
        if route == "/apply-template":
            prompt = self.tokenizer.apply_chat_template(
                payload["messages"],
                tools=payload.get("tools") or None,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
            return {
                "prompt": prompt,
                "operation": "tokenizer.apply_chat_template",
                "transport": self.transport,
            }
        if route == "/tokenize":
            ids = self.tokenizer.encode(
                payload["content"],
                add_special_tokens=bool(payload.get("add_special", False)),
            )
            return {
                "tokens": [
                    {"id": token_id, "piece": self._piece(token_id)} for token_id in ids
                ]
                if payload.get("with_pieces")
                else ids
            }
        if route == "/detokenize":
            return {
                "content": self.tokenizer.decode(
                    payload["tokens"],
                    skip_special_tokens=False,
                    clean_up_tokenization_spaces=False,
                )
            }
        raise RuntimeFailure("교육용 실행기가 지원하지 않는 동작입니다: " + route)

    def _text(self, ids):
        # EOS만 제외한다. tool_call 특수 토큰은 도구 표현 해석에 필요하다.
        visible = [
            token_id for token_id in ids if token_id != self.tokenizer.eos_token_id
        ]
        return self.tokenizer.decode(
            visible, skip_special_tokens=False, clean_up_tokenization_spaces=False
        )

    def observe(self, route, payload=None):
        self._ready()
        if route == "/slots":
            return [
                {
                    "id": 0,
                    "n_ctx": self.context_limit,
                    "is_processing": self._run is not None,
                    "n_prompt_tokens": len(self.cache_ids),
                    "cache_version": self.cache_version,
                }
            ]
        return self.request(route, payload)

    def _vector(self, tensor, limit=12):
        value = tensor.detach().float().flatten()
        return {
            "shape": list(tensor.shape),
            "dtype": str(tensor.dtype),
            "sample": value[:limit].tolist(),
            "min": float(value.min()),
            "max": float(value.max()),
            "norm": float(value.norm()),
        }

    def _timed(self, hook):
        """hook 본문(텐서 표본 추출·이벤트 기록)에 쓴 시간을 누적한다. 사용자 정지 시간은 뺀다."""

        def wrapper(*args, **kwargs):
            run = self._run
            if run is None:
                return hook(*args, **kwargs)
            pause_before = run.paused_seconds
            started = time.perf_counter()
            try:
                return hook(*args, **kwargs)
            finally:
                spent = (time.perf_counter() - started) * 1000 - (
                    run.paused_seconds - pause_before
                ) * 1000
                self._hook_ms += max(0.0, spent)

        return wrapper

    def _attach_hooks(self):
        def embedding(module, inputs, output):
            if self._run is not None:
                self.embedding_sample = self._vector(output[0, -1])
                self._run.emit(
                    "reference.embedding",
                    "토큰 ID를 임베딩 벡터로 조회",
                    "실제 임베딩 레이어 출력 중 마지막 입력 위치의 일부 값입니다. 전체 shape와 표본 범위를 구분합니다.",
                    category="model",
                    actor="runtime",
                    output={
                        "call": self._call,
                        "step": self._step,
                        "phase": self._phase,
                        "tensor_shape": list(output.shape),
                        "last_position": self.embedding_sample,
                    },
                )

        self._hooks.append(
            self.model.model.embed_tokens.register_forward_hook(self._timed(embedding))
        )
        for layer_index, layer in enumerate(self.model.model.layers):
            for name in ("q_proj", "k_proj", "v_proj"):

                def projection(module, inputs, output, index=layer_index, kind=name):
                    if self._run is not None and index == self._capture_layer:
                        sample = self._vector(output[0, -1])
                        self.projection_samples[kind] = sample
                        self._run.emit(
                            "reference.projection",
                            f"{kind} 선형 변환",
                            "선택한 레이어의 실제 projection 출력입니다. q_proj/k_proj 값은 head별 정규화와 RoPE 적용 전 값입니다.",
                            category="model",
                            actor="runtime",
                            output={
                                "call": self._call,
                                "step": self._step,
                                "layer": index,
                                "projection": kind,
                                "tensor_shape": list(output.shape),
                                "last_position": sample,
                            },
                        )

                self._hooks.append(
                    getattr(layer.self_attn, name).register_forward_hook(
                        self._timed(projection)
                    )
                )

            def attention(module, inputs, output, index=layer_index):
                if self._run is not None and index == self._capture_layer:
                    weights = output[1]
                    if weights is not None:
                        row = weights[0, 0, -1].detach().float()
                        count = min(32, row.numel())
                        top = row.topk(min(8, row.numel()))
                        self.attention_sample = {
                            "shape": list(weights.shape),
                            "layer": index,
                            "head": 0,
                            "query_position": int(weights.shape[-1] - 1),
                            "last_positions": list(
                                range(row.numel() - count, row.numel())
                            ),
                            "last_weights": row[-count:].tolist(),
                            "row_sum": float(row.sum()),
                            "top_positions": top.indices.tolist(),
                            "top_weights": top.values.tolist(),
                        }
                        self._run.emit(
                            "reference.attention",
                            "attention 계산 결과",
                            "선택한 레이어·head 0·마지막 query의 실제 attention 가중치입니다. 값이 큰 위치가 답변의 의미적 이유라는 뜻은 아닙니다.",
                            category="model",
                            actor="runtime",
                            output={
                                "call": self._call,
                                "step": self._step,
                                **self.attention_sample,
                            },
                        )

            self._hooks.append(
                layer.self_attn.register_forward_hook(self._timed(attention))
            )

    def _cache_snapshot(self, cache):
        if cache is None:
            return {"enabled": False, "tokens": 0, "bytes": 0, "layers": []}
        layers = []
        size = 0
        for index, layer in enumerate(cache.layers):
            key, value = layer.keys, layer.values
            if key is None or value is None:
                continue
            used = (
                key.numel() * key.element_size() + value.numel() * value.element_size()
            )
            size += used
            item = {
                "layer": index,
                "k_shape": list(key.shape),
                "v_shape": list(value.shape),
                "bytes": used,
                "dtype": str(key.dtype),
                "k_last_head0": key[0, 0, -1, :8].detach().float().tolist(),
                "v_last_head0": value[0, 0, -1, :8].detach().float().tolist(),
            }
            if index == self._capture_layer:
                item["k_slice"] = key[0, 0, -16:, :16].detach().float().tolist()
                item["v_slice"] = value[0, 0, -16:, :16].detach().float().tolist()
                item["slice_positions"] = list(
                    range(max(0, key.shape[-2] - 16), key.shape[-2])
                )
                item["slice_dimensions"] = list(range(min(16, key.shape[-1])))
            layers.append(item)
        return {
            "enabled": True,
            "tokens": int(cache.get_seq_length()),
            "bytes": size,
            "layers": layers,
            "version": self.cache_version,
            "captured_layer": self._capture_layer,
            "captured_head": 0,
        }

    def _sample(self, logits, options, generator):
        torch = self.torch
        raw = logits.float()
        filtered = raw.clone()
        temperature = float(options.get("temperature", 0.2))
        if temperature > 0:
            filtered /= temperature
        top_k = int(options.get("top_k", 40))
        top_p = float(options.get("top_p", 0.95))
        if top_k > 0:
            threshold = torch.topk(filtered, min(top_k, filtered.numel())).values[-1]
            filtered[filtered < threshold] = -float("inf")
        if top_p < 1:
            values, indices = torch.sort(filtered, descending=True)
            rejected = torch.cumsum(torch.softmax(values, dim=-1), dim=-1) > top_p
            rejected[1:] = rejected[:-1].clone()
            rejected[0] = False
            filtered[indices[rejected]] = -float("inf")
        probabilities = torch.softmax(filtered, dim=-1)
        # 모델이 낸 분포(temperature·top_k·top_p 적용 전 softmax). 절단 후 재정규화한 prob와 구분한다.
        raw_probabilities = torch.softmax(raw, dim=-1)
        selected = (
            int(torch.argmax(raw))
            if temperature <= 0
            else int(torch.multinomial(probabilities, 1, generator=generator))
        )
        top = probabilities.topk(min(5, probabilities.numel()))
        candidates = []
        for token_id, probability in zip(top.indices.tolist(), top.values.tolist()):
            if probability <= 0:
                continue
            candidates.append(
                {
                    "id": token_id,
                    "piece": self._piece(token_id),
                    "prob": probability,
                    "raw_prob": float(raw_probabilities[token_id]),
                    "raw_logit": float(raw[token_id]),
                    "filtered_logit": float(filtered[token_id]),
                }
            )
        raw_top = raw_probabilities.topk(min(5, raw_probabilities.numel()))
        return selected, {
            "id": selected,
            "piece": self._piece(selected),
            "prob": float(probabilities[selected]),
            "raw_prob": float(raw_probabilities[selected]),
            "prob_basis": "renormalized_after_temperature_top_k_top_p",
            "raw_prob_basis": "softmax_of_raw_logits_before_samplers",
            "raw_logit": float(raw[selected]),
            "candidates": candidates,
            "raw_top": [
                {
                    "id": token_id,
                    "piece": self._piece(token_id),
                    "raw_prob": probability,
                }
                for token_id, probability in zip(raw_top.indices.tolist(), raw_top.values.tolist())
            ],
            "temperature": temperature,
            "top_k": top_k,
            "top_p": top_p,
            "strategy": "argmax" if temperature <= 0 else "multinomial",
        }

    def generate_observed(self, run, payload, iteration):
        self._ready()
        from transformers import DynamicCache

        torch = self.torch
        prompt = self.request("/apply-template", payload)["prompt"]
        prompt_ids = self.tokenizer.encode(prompt, add_special_tokens=False)
        if len(prompt_ids) > self.context_limit:
            raise RuntimeFailure(
                f"입력 {len(prompt_ids)}토큰이 교육용 실행기의 한도 {self.context_limit}를 넘었습니다. 이력을 끄거나 새 세션으로 실행하세요."
            )
        options = run.engine
        use_kv = options.get("use_kv", True)
        self._capture_layer = min(
            int(options.get("capture_layer", 0)), len(self.model.model.layers) - 1
        )
        reuse = 0
        if use_kv and options.get("cache_prompt") and self.cache is not None:
            for old, new in zip(self.cache_ids, prompt_ids[:-1]):
                if old != new:
                    break
                reuse += 1
        if reuse:
            # Transformers 5의 crop은 제거할 위치 수를 음수로 받는다.
            remove = self.cache.get_seq_length() - reuse
            if remove > 0:
                self.cache.crop(-remove)
            self.cache_ids = self.cache_ids[:reuse]
        else:
            self.cache = DynamicCache(config=self.model.config) if use_kv else None
            self.cache_ids = []
        self._run = run
        self._call = iteration
        self._step = 0
        self._phase = "prefill"
        generated = []
        records = []
        prefill_ms = 0.0
        decode_ms = 0.0
        prefill_hook_ms = 0.0
        decode_hook_ms = 0.0
        forward_tokens = 0
        generator = torch.Generator(device="cpu").manual_seed(
            int(payload.get("seed", 42))
        )
        finish = "length"
        limit = min(
            int(payload.get("max_tokens", self.max_output_tokens)),
            self.max_output_tokens,
        )
        try:
            run.emit(
                "reference.cache.select",
                "입력에 쓸 계산 상태 준비",
                "이전 토큰 ID와 같은 접두부만 재사용합니다. 다음 토큰의 logits를 얻기 위해 마지막 입력 위치는 다시 계산합니다.",
                category="model",
                actor="runtime",
                output={
                    "call": iteration,
                    "prompt_tokens": len(prompt_ids),
                    "cached_tokens": reuse,
                    "use_kv": use_kv,
                    "cache_prompt": bool(options.get("cache_prompt")),
                    "capture_layer": self._capture_layer,
                },
            )
            with torch.inference_mode():
                for step in range(limit):
                    self._step = step
                    self._phase = "prefill" if step == 0 else "decode"
                    if step == 0:
                        ids = prompt_ids[reuse:] if use_kv else prompt_ids
                    else:
                        ids = [generated[-1]] if use_kv else prompt_ids + generated
                    total = len(prompt_ids) + len(generated)
                    if total > self.context_limit:
                        raise RuntimeFailure(
                            "출력 생성 중 컨텍스트 한도에 도달했습니다."
                        )
                    before = (
                        self._cache_snapshot(self.cache)
                        if step == 0
                        else {"tokens": len(self.cache_ids) if use_kv else 0}
                    )
                    run.emit(
                        "reference.forward.start",
                        "모델 forward 호출 전",
                        "이 줄 다음에서 실제 모델을 계산합니다. KV를 쓰면 새 위치만, 끄면 앞부분을 포함한 전체 입력을 다시 계산합니다.",
                        category="model",
                        actor="runtime",
                        input={
                            "call": iteration,
                            "step": step,
                            "phase": self._phase,
                            "input_ids": ids,
                            "input_shape": [1, len(ids)],
                            "attention_mask_shape": [1, total],
                            "past_tokens": len(self.cache_ids) if use_kv else 0,
                            "use_cache": use_kv,
                        },
                    )
                    pause_before = run.paused_seconds
                    self._hook_ms = 0.0
                    started = time.perf_counter()
                    result = self.model(
                        input_ids=torch.tensor([ids], dtype=torch.long),
                        attention_mask=torch.ones((1, total), dtype=torch.long),
                        past_key_values=self.cache,
                        use_cache=use_kv,
                        return_dict=True,
                        logits_to_keep=1,
                    )
                    elapsed = (time.perf_counter() - started) * 1000
                    # 사용자 정지는 제외한다. 텐서 추출·hook 기록 비용은 포함되므로 따로 측정해 둘 다 남긴다.
                    elapsed = max(
                        0.0, elapsed - (run.paused_seconds - pause_before) * 1000
                    )
                    hook_ms = min(self._hook_ms, elapsed)
                    if step == 0:
                        prefill_ms += elapsed
                        prefill_hook_ms += hook_ms
                    else:
                        decode_ms += elapsed
                        decode_hook_ms += hook_ms
                    forward_tokens += len(ids)
                    if use_kv:
                        self.cache = result.past_key_values
                        self.cache_ids = prompt_ids + generated
                        self.cache_version += 1
                    snapshot = self._cache_snapshot(self.cache)
                    run.emit(
                        "reference.cache.write",
                        "forward 완료·KV 상태 확인",
                        "실제 past_key_values 텐서의 shape·메모리 바이트·일부 수치를 읽었습니다. K/V 표본은 캐시에 저장된 RoPE 적용 후 값이며 projection 원본과 구분합니다.",
                        category="model",
                        actor="runtime",
                        output={
                            "call": iteration,
                            "step": step,
                            "phase": self._phase,
                            "observed_forward_ms": round(elapsed, 3),
                            "observer_hook_ms": round(hook_ms, 3),
                            "forward_ms_excluding_hooks": round(elapsed - hook_ms, 3),
                            "timing_basis": "forward_with_observer_hooks_excluding_user_pause",
                            "timing_note": "observed_forward_ms는 model() 벽시간에서 사용자 정지를 뺀 값으로 hook(텐서 표본·이벤트 기록) 비용을 포함한다. observer_hook_ms는 hook 본문에서 잰 시간이며, 둘의 차이가 hook 밖의 계산 시간에 가깝다. 순수 커널 시간의 측정은 아니다.",
                            "user_pause_ms": round(
                                (run.paused_seconds - pause_before) * 1000, 3
                            ),
                            "forward_input_tokens": len(ids),
                            "cumulative_forward_tokens": forward_tokens,
                            "before_tokens": before["tokens"],
                            "cache": snapshot,
                        },
                    )
                    token, record = self._sample(
                        result.logits[0, -1], payload, generator
                    )
                    generated.append(token)
                    records.append(record)
                    raw = self._text(generated)
                    run.emit(
                        "reference.sample",
                        "logits에서 다음 토큰 선택",
                        "우리 코드가 temperature·top_k·top_p를 적용한 실제 분포에서 선택했습니다. 이 토큰은 다음 forward의 입력으로 돌아갑니다.",
                        category="model",
                        actor="llm",
                        output={
                            "call": iteration,
                            "step": step,
                            **record,
                            "output_ids": list(generated),
                            "text_so_far": raw,
                            "eos": token == self.tokenizer.eos_token_id,
                        },
                    )
                    if token == self.tokenizer.eos_token_id:
                        finish = "stop"
                        break
            text = self._text(generated)
            message = parse_tool_output(text)
            run.emit(
                "reference.protocol",
                "생성 원문을 답변·도구 요청으로 구분",
                "Qwen의 tool_call 표현을 우리 어댑터가 JSON으로 해석합니다. 도구 ID는 하네스가 부여하며, 실제 실행 권한은 이후 검증합니다.",
                category="model",
                output={"call": iteration, "raw_text": text, "message": message},
            )
            if message.get("tool_calls") and finish != "length":
                finish = "tool_calls"
            probabilities = [
                {
                    "id": record["id"],
                    "token": self.tokenizer.decode(
                        [record["id"]], skip_special_tokens=True
                    ),
                    # llama.cpp와 같은 기준: 샘플러 적용 전 모델 분포의 로그 확률.
                    "logprob": math.log(max(record.get("raw_prob", record["prob"]), 1e-45)),
                    "renormalized_logprob": math.log(max(record["prob"], 1e-45)),
                    "top_logprobs": [
                        {
                            "id": item["id"],
                            "token": self.tokenizer.decode(
                                [item["id"]], skip_special_tokens=True
                            ),
                            "logprob": math.log(max(item.get("raw_prob", item["prob"]), 1e-45)),
                            "renormalized_logprob": math.log(max(item["prob"], 1e-45)),
                        }
                        for item in record["candidates"]
                    ],
                }
                for record in records
            ]
            timings = {
                "timing_basis": "forward_with_observer_hooks_excluding_user_pause",
                "cache_n": reuse,
                "prompt_n": len(prompt_ids) - reuse,
                "prompt_ms": round(prefill_ms, 3),
                "prompt_hook_ms": round(prefill_hook_ms, 3),
                "prompt_ms_excluding_hooks": round(prefill_ms - prefill_hook_ms, 3),
                "predicted_n": len(generated),
                "predicted_ms": round(decode_ms, 3),
                "predicted_hook_ms": round(decode_hook_ms, 3),
                "predicted_ms_excluding_hooks": round(decode_ms - decode_hook_ms, 3),
                # decode forward는 n-1회다(마지막 토큰은 forward하지 않음). 분모가 그 횟수임을 명시한다.
                "predicted_per_second": (len(generated) - 1) / (decode_ms / 1000)
                if decode_ms
                else 0.0,
                "decode_forwards": max(0, len(generated) - 1),
                "forward_tokens": forward_tokens,
            }
            return {
                "id": "ref-" + run.id + "-" + str(iteration),
                "object": "chat.completion",
                "model": self.model_id,
                "system_fingerprint": "reference-torch-" + torch.__version__,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": finish,
                        "message": message,
                        "logprobs": {"content": probabilities},
                    }
                ],
                "usage": {
                    "prompt_tokens": len(prompt_ids),
                    "completion_tokens": len(generated),
                    "total_tokens": len(prompt_ids) + len(generated),
                    "prompt_tokens_details": {"cached_tokens": reuse},
                },
                "timings": timings,
                "__verbose": {
                    "prompt": prompt,
                    "content": text,
                    "tokens": generated,
                    "stop_type": "limit" if finish == "length" else "eos",
                    "truncated": False,
                    "generation_settings": {
                        "seed": payload.get("seed", 42),
                        "temperature": payload.get("temperature", 0.2),
                        "top_k": payload.get("top_k", 40),
                        "top_p": payload.get("top_p", 0.95),
                        "samplers": ["temperature", "top_k", "top_p"],
                        "use_kv": use_kv,
                    },
                },
                "_assembly": {
                    "source": "python_reference_decoder",
                    "observed_token_ids": len(generated),
                    "observed_token_ids_basis": "생성한 토큰 수(logprobs 항목 수와 같음)",
                    "logprob_basis": "softmax(raw logits) 기준. renormalized_logprob는 temperature·top_k·top_p 절단 후 재정규화 값",
                },
            }
        except BaseException:
            # 중단된 forward는 일부 레이어만 갱신했을 수 있어 다음 요청에 재사용하지 않는다.
            self.cache = None
            self.cache_ids = []
            raise
        finally:
            self._run = None
