"""다운로드 없는 작은 실제 신경망으로 KV·샘플링·중단을 검사한다."""

import importlib.util
import json
import unittest
import time

from local_agent_lab.reference_engine import ReferenceRuntime, parse_tool_output
from local_agent_lab.runtime import RuntimeFailure
from local_agent_lab.trace import Run, RunStopped


class ProtocolTests(unittest.TestCase):
    def test_qwen_tools_are_preserved_and_parsed(self):
        result = parse_tool_output(
            '계산합니다.<tool_call>{"name":"add_numbers","arguments":{"a":2,"b":3}}</tool_call>'
        )
        self.assertEqual(result["content"], "계산합니다.")
        self.assertEqual(result["tool_calls"][0]["function"]["name"], "add_numbers")
        for text in (
            "<tool_call>{",
            "<tool_call>oops</tool_call>",
            "<tool_call>[]</tool_call>",
        ):
            with self.assertRaises(RuntimeFailure):
                parse_tool_output(text)


@unittest.skipUnless(
    importlib.util.find_spec("torch") and importlib.util.find_spec("transformers"),
    "reference 추가 의존성 필요",
)
class ReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import torch
        from transformers import Qwen3Config, Qwen3ForCausalLM

        cls.torch = torch
        torch.set_num_threads(2)
        torch.manual_seed(7)
        config = Qwen3Config(
            vocab_size=32,
            hidden_size=32,
            intermediate_size=64,
            num_hidden_layers=2,
            num_attention_heads=2,
            num_key_value_heads=1,
            head_dim=16,
        )
        config._attn_implementation = "eager"
        cls.model = Qwen3ForCausalLM(config).eval()

    def runtime(self):
        class Tokenizer:
            eos_token_id = 31

            def apply_chat_template(self, *a, **kw):
                return "ABC"

            def encode(self, *a, **kw):
                return [1, 2, 3]

            def decode(self, ids, **kw):
                return "".join(chr(65 + i) for i in ids)

            def convert_ids_to_tokens(self, i):
                return chr(65 + i)

        runtime = ReferenceRuntime(autoload=False)
        runtime.torch = self.torch
        runtime.model = self.model
        runtime.tokenizer = Tokenizer()
        runtime._attach_hooks()
        self.addCleanup(lambda: [hook.remove() for hook in runtime._hooks])
        return runtime

    def generate(self, runtime, **options):
        run = Run(
            "tiny model",
            "reference-unit",
            engine={"max_tokens": 3, "temperature": 0, **options},
        )
        response = runtime.generate_observed(
            run, {"messages": [], "max_tokens": 3, "temperature": 0, "top_k": 1}, 1
        )
        json.dumps(run.snapshot(), allow_nan=False)
        return response, run

    def test_cache_and_recompute_produce_same_ids(self):
        runtime = self.runtime()
        cached, run = self.generate(runtime, use_kv=True, cache_prompt=False)
        uncached, other = self.generate(runtime, use_kv=False)
        self.assertEqual(cached["__verbose"]["tokens"], uncached["__verbose"]["tokens"])
        self.assertGreater(
            uncached["timings"]["forward_tokens"], cached["timings"]["forward_tokens"]
        )
        writes = [
            e["output"]["cache"]
            for e in run.events
            if e["kind"] == "reference.cache.write"
        ]
        self.assertEqual(len(writes[0]["layers"]), 2)
        self.assertEqual(writes[0]["tokens"], 3)
        self.assertGreater(writes[-1]["bytes"], writes[0]["bytes"])
        self.assertTrue(
            all(
                e["output"]["cache"]["enabled"] is False
                for e in other.events
                if e["kind"] == "reference.cache.write"
            )
        )

    def test_prefix_reuse_and_stop_invalidates_cache(self):
        runtime = self.runtime()
        self.generate(runtime)
        response, _ = self.generate(runtime)
        self.assertEqual(response["timings"]["cache_n"], 2)

        class StopOnForward(Run):
            def emit(self, kind, *a, **kw):
                if kind == "reference.forward.start":
                    raise RunStopped()
                return super().emit(kind, *a, **kw)

        with self.assertRaises(RunStopped):
            runtime.generate_observed(StopOnForward("x", "stop"), {"messages": []}, 1)
        self.assertIsNone(runtime.cache)
        self.assertEqual(runtime.cache_ids, [])

    def test_forward_timing_explicitly_includes_observer_cost(self):
        class SlowObserver(Run):
            def emit(self, kind, *a, **kw):
                if kind in {
                    "reference.embedding",
                    "reference.projection",
                    "reference.attention",
                }:
                    time.sleep(0.02)
                return super().emit(kind, *a, **kw)

        runtime = self.runtime()
        run = SlowObserver("timing", "observer-time")
        result = runtime.generate_observed(
            run, {"messages": [], "max_tokens": 1, "temperature": 0}, 1
        )
        self.assertEqual(
            result["timings"]["timing_basis"],
            "forward_with_observer_hooks_excluding_user_pause",
        )
        measured = next(
            e["output"] for e in run.events if e["kind"] == "reference.cache.write"
        )
        self.assertGreater(measured["observed_forward_ms"], 80)
        self.assertNotIn("compute_ms", measured)
