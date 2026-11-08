import contextlib
import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from local_agent_lab.features import run_metrics
from local_agent_lab.experiments import CASES
from local_agent_lab.harness import Harness
from local_agent_lab.inference import (
    ChatAssembly,
    audit_response,
    engine_options,
    preview_input,
)
from local_agent_lab.runtime import LocalRuntime, RuntimeFailure
from local_agent_lab.trace import ROOT, Run, expand_sources
from local_agent_lab.web import Application
from test_harness import FixtureRuntime, response, tool_call


def chunk(delta=None, finish=None, **extras):
    choice = {"index": 0, "delta": delta or {}, "finish_reason": finish}
    if "logprobs" in extras:
        choice["logprobs"] = extras.pop("logprobs")
    return {"choices": [choice], **extras}


class StreamingFixture(FixtureRuntime):
    """모델 없이 실제 하네스의 스트림 분기·조립·도구 게이트를 검증한다."""

    def observe(self, route, payload=None):
        return []

    def request(self, route, payload=None):
        if route == "/props":
            return {
                "default_generation_settings": {"n_ctx": 4096},
                "build_info": "fixture",
            }
        if route == "/detokenize":
            return {"content": "fixture prompt"}
        return super().request(route, payload)

    def stream_json(self, route, payload):
        self.requests.append((route, json.loads(json.dumps(payload))))
        final = next(self.responses)
        message = final["choices"][0]["message"]
        yield {
            "opened": True,
            "http_status": 200,
            "content_type": "text/event-stream",
            "arrival_ms": 1,
        }
        parts = [
            chunk(
                prompt_progress={"total": 10, "cache": 4, "processed": 4, "time_ms": 0}
            )
        ]
        if message.get("tool_calls"):
            original = message["tool_calls"][0]
            parts += [
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                **original,
                                "function": {
                                    "name": original["function"]["name"],
                                    "arguments": '{"path":',
                                },
                            }
                        ]
                    }
                ),
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": '"notes/agent-guide.md"}'},
                            }
                        ]
                    }
                ),
            ]
        else:
            parts += [chunk({"content": message.get("content", "")})]
        parts += [
            chunk(finish=final["choices"][0]["finish_reason"]),
            {
                "choices": [],
                "usage": final["usage"],
                "timings": {
                    "cache_n": 4,
                    "prompt_n": 6,
                    "prompt_ms": 11,
                    "predicted_ms": 13,
                },
            },
        ]
        for index, part in enumerate(parts):
            yield {
                "data": part,
                "raw": json.dumps(part),
                "arrival_ms": index + 2,
                "frame_bytes": len(json.dumps(part).encode()),
            }
        yield {"done": True, "raw": "[DONE]", "arrival_ms": 20, "frame_bytes": 14}


class InferenceTests(unittest.TestCase):
    def test_sse_tool_fragments_are_not_executed_until_complete_and_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            shutil.copytree(ROOT / "demo_workspace", workspace)
            runtime = StreamingFixture(
                response(
                    {
                        "tool_calls": [
                            tool_call("read_file", path="notes/agent-guide.md")
                        ]
                    }
                ),
                response({"content": "읽기 완료"}),
            )
            run = Run("read the guide", "stream-fixture")
            Harness(workspace, Path(temporary) / "state", runtime=runtime).execute(run)
            self.assertEqual(run.status, "completed")
            self.assertEqual(run.tool_calls, 1)
            kinds = [event["kind"] for event in run.events]
            self.assertLess(
                kinds.index("model.stream.assemble"), kinds.index("tool.dispatch")
            )
            self.assertLess(kinds.index("tool.validate"), kinds.index("tool.result"))
            requests = [
                payload
                for route, payload in runtime.requests
                if route.endswith("completions")
            ]
            self.assertFalse(
                requests[0]["logprobs"]
            )  # Real build rejects tools+stream+logprobs.
            self.assertEqual(requests[1]["messages"][-1]["role"], "tool")
            audit = next(
                event for event in run.events if event["kind"] == "runtime.cache"
            )
            self.assertEqual(audit["output"]["cached_tokens"], 4)
            self.assertTrue(audit["output"]["accounting_matches"])

    def test_full_response_mode_preserves_real_tool_token_probabilities(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = StreamingFixture(response({"content": "fixture answer"}))
            run = Run(
                "hello", "full-fixture", engine={"stream": False, "cache_prompt": False}
            )
            Harness(ROOT / "demo_workspace", Path(temporary), runtime=runtime).execute(
                run
            )
            payload = next(
                payload
                for route, payload in runtime.requests
                if route.endswith("completions")
            )
            self.assertFalse(payload["stream"])
            self.assertFalse(payload["cache_prompt"])
            self.assertTrue(payload["logprobs"])
            self.assertEqual(payload["top_logprobs"], 3)
            self.assertFalse(
                any(event["kind"] == "model.stream" for event in run.events)
            )

    def test_assembly_preserves_unicode_tools_usage_and_logprobs(self):
        result = ChatAssembly()
        result.add(
            chunk(
                {"content": "안"},
                logprobs={
                    "content": [
                        {
                            "id": 11,
                            "token": "안",
                            "logprob": -0.2,
                            "bytes": [236, 149, 136],
                        }
                    ]
                },
            ),
            12,
        )
        result.add(chunk({"content": "녕"}), 14)
        result.add(chunk(finish="stop"), 15)
        result.add(
            {
                "choices": [],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2},
                "timings": {"cache_n": 3, "prompt_n": 5},
            },
            16,
        )
        response = result.response()
        self.assertEqual(response["choices"][0]["message"]["content"], "안녕")
        self.assertEqual(response["choices"][0]["logprobs"]["content"][0]["id"], 11)
        self.assertEqual(response["usage"]["prompt_tokens"], 8)
        self.assertEqual(response["_assembly"]["first_payload_received_ms"], 12)

    def test_missing_finish_or_invalid_tool_index_never_becomes_success(self):
        with self.assertRaises(RuntimeFailure):
            ChatAssembly().response()
        for invalid in (-1, 6, True, "0"):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeFailure):
                ChatAssembly().add(chunk({"tool_calls": [{"index": invalid}]}), 1)

    def test_token_bytes_reconstruct_split_korean_without_invented_char_offsets(self):
        class TokenRuntime:
            def observe(self, *args):
                return {}

            def request(self, route, payload):
                return {
                    "/apply-template": {"prompt": "하"},
                    "/tokenize": {
                        "tokens": [
                            {"id": 1, "piece": [237, 149]},
                            {"id": 2, "piece": [152]},
                        ]
                    },
                    "/detokenize": {"content": "하"},
                }[route]

        run = Run("하", "bytes")
        preview = preview_input(run, TokenRuntime(), {}, 1)
        event = next(event for event in run.events if event["kind"] == "model.tokenize")
        self.assertTrue(event["output"]["pieces_match_utf8"])
        self.assertEqual(event["output"]["user_prompt_byte_range"], [0, 3])
        self.assertEqual(preview["tokens"][0]["piece"], [237, 149])

        class Repeated(TokenRuntime):
            def request(self, route, payload):
                value = super().request(route, payload)
                if route == "/apply-template":
                    return {"prompt": "하하"}
                if route == "/tokenize":
                    return {"tokens": value["tokens"] * 2}
                return {"content": "하하"}

        repeated = Run("하", "repeated")
        preview_input(repeated, Repeated(), {}, 1)
        repeated_event = next(
            e for e in repeated.events if e["kind"] == "model.tokenize"
        )
        self.assertIsNone(repeated_event["output"]["user_prompt_byte_range"])
        self.assertEqual(repeated_event["output"]["user_prompt_mapping"], "ambiguous")

    def test_missing_cache_data_is_unknown_not_a_zero_hit(self):
        run = Run("hello", "missing")
        audit_response(
            run,
            {"usage": {"prompt_tokens": 3, "completion_tokens": 1}},
            {"prompt": None, "tokens": []},
            1,
        )
        audit = run.events[-1]["output"]
        self.assertIsNone(audit["cached_tokens"])
        self.assertIsNone(audit["accounting_matches"])
        run.emit(
            "model.response",
            "fixture",
            "fixture",
            output={"usage": {"prompt_tokens": 3}},
        )
        self.assertIsNone(run_metrics(run.snapshot())["cached_tokens"])

    def test_compact_saved_sources_expand_to_the_exact_recorded_frames(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Run("source", "compact")
            for _ in range(3):
                run.emit("test", "test", "test")
            compact = run.snapshot(compact=True)
            original = run.snapshot()
            self.assertLess(len(json.dumps(compact)), len(json.dumps(original)))
            expanded = expand_sources(compact)
            self.assertEqual(
                expanded["events"][1]["source"]["code"],
                original["events"][1]["source"]["code"],
            )
            self.assertEqual(
                expanded["events"][1]["source"]["stack"][-1]["line"],
                original["events"][1]["source"]["stack"][-1]["line"],
            )
            directory = Path(temporary)
            run.save(directory / "traces")
            app = Application(Harness(state_dir=directory))
            self.assertEqual(
                app.get_run(run.id)["events"][0]["source"]["code"],
                original["events"][0]["source"]["code"],
            )
            incremental = app.get_run(run.id, after=2, compact=True)
            self.assertEqual(len(incremental["events"]), 1)
            self.assertIn(
                incremental["events"][0]["source"]["source_ref"],
                incremental["source_snapshots"],
            )

    def test_engine_options_are_explicit_booleans(self):
        self.assertTrue(engine_options()["cache_prompt"])
        self.assertFalse(engine_options({"stream": False})["stream"])
        for value in (
            {"cache_prompt": "false"},
            {"stream": 0},
            {"execute": "anything"},
            [],
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                engine_options(value)

    def test_cache_experiment_changes_reuse_without_changing_model_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = FixtureRuntime(
                response({"content": "first"}), response({"content": "second"})
            )
            app = Application(
                Harness(ROOT / "demo_workspace", Path(temporary), runtime=runtime)
            )
            experiment_id = app.experiments.start(
                CASES["kv_cache"]["prompt"], case_id="kv_cache"
            )
            done = threading.Event()

            def observe_finish():
                while app.experiment_active:
                    with app.lock:
                        runs = list(app.runs.values())
                    if runs:
                        with runs[-1].condition:
                            runs[-1].condition.wait(timeout=0.02)
                done.set()

            threading.Thread(target=observe_finish, daemon=True).start()
            self.assertTrue(done.wait(timeout=8))
            experiment = app.experiments.get(experiment_id)
            self.assertEqual(experiment["status"], "completed")
            self.assertTrue(experiment["controls"]["same_model_input"])
            requests = [
                payload
                for route, payload in runtime.requests
                if route.endswith("completions")
            ]
            self.assertFalse(requests[0]["cache_prompt"])
            self.assertTrue(requests[1]["cache_prompt"])
            for payload in requests:
                del payload["cache_prompt"]
            self.assertEqual(requests[0], requests[1])
            self.assertFalse(experiment["results"]["B"]["evaluation"]["available"])

    def test_real_http_sse_handles_comments_multiline_json_and_early_disconnect(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                body = ': ping\r\ndata: {"choices":\r\ndata: [{"index":0,"delta":{"content":"안"}}]}\r\n\r\n'
                if self.path != "/broken":
                    body += "data: [DONE]\r\n\r\n"
                self.wfile.write(body.encode("utf-8"))

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            runtime = LocalRuntime(f"http://127.0.0.1:{server.server_port}")
            frames = list(runtime.stream_json("/stream", {"stream": True}))
            self.assertTrue(frames[0]["opened"])
            self.assertEqual(frames[1]["data"]["choices"][0]["delta"]["content"], "안")
            # I-12: 선행 주석(: ping) 바이트는 프레임 크기에 넣지 않는다. data 줄 2개 + 종결 빈 줄.
            self.assertEqual(
                frames[1]["frame_bytes"],
                len('data: {"choices":\r\n'.encode()) + len('data: [{"index":0,"delta":{"content":"안"}}]}\r\n'.encode()) + 2,
            )
            self.assertTrue(frames[-1]["done"])
            with self.assertRaises(RuntimeFailure):
                list(runtime.stream_json("/broken", {"stream": True}))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


class StreamObservationTests(unittest.TestCase):
    """독립 검토(추론 계측)에서 재현한 표시 결함: 생성 토큰 수 대조, 부분 조립 기록, 출처 라벨."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        shutil.copytree(ROOT / "demo_workspace", self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def run_with(self, parts):
        class Fixture(StreamingFixture):
            def stream_json(self, route, payload):
                yield {"opened": True, "http_status": 200, "content_type": "text/event-stream", "arrival_ms": 1}
                for index, part in enumerate(parts):
                    if part == "ERROR":
                        raise RuntimeFailure("SSE 프레임에 유효한 응답 대신 오류가 있습니다: fixture")
                    yield {"data": part, "raw": json.dumps(part), "arrival_ms": index + 2, "frame_bytes": 10}
                yield {"done": True, "raw": "[DONE]", "arrival_ms": 99, "frame_bytes": 14}

        run = Run("stream", "stream-audit", features={"rules": False, "skills": False, "commands": False, "memory": False})
        Harness(self.workspace, self.root / "state", runtime=Fixture()).execute(run)
        return run

    def test_generation_count_mismatch_is_detected_and_sources_are_labelled(self):
        # I-04: llama.cpp는 불완전한 UTF-8 토큰을 보류·병합해 logprobs 항목이 completion_tokens보다 적어도 대조하지 않았다.
        probs = [{"token": "a", "logprob": -0.1}, {"token": "b", "logprob": -0.2}]
        parts = [
            chunk({"content": "ab"}, logprobs={"content": probs}),
            chunk(finish="stop"),
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 6}, "timings": {"cache_n": 4, "prompt_n": 6, "predicted_n": 6, "prompt_ms": 1, "predicted_ms": 2}},
        ]
        run = self.run_with(parts)
        self.assertEqual(run.status, "completed", run.error)
        cache = next(e["output"] for e in run.events if e["kind"] == "runtime.cache")
        self.assertEqual(cache["logprob_entries"], 2)
        self.assertEqual(cache["predicted_n"], 6)
        self.assertIs(cache["generation_count_matches"], False)
        self.assertIn("보류", cache["generation_count_note"])
        # I-07: cached_tokens가 어느 서버 필드에서 왔는지, 보고/진단/계산값이 무엇인지 적는다.
        self.assertEqual(cache["cached_tokens_source"], "timings.cache_n")
        self.assertIn("accounting_matches", cache["value_sources"]["derived"])
        self.assertIn("prompt_tokens", cache["value_sources"]["reported"])
        assembly = next(e["output"]["assembly"] for e in run.events if e["kind"] == "model.stream.assemble")
        self.assertEqual(assembly["logprob_entries"], 2)
        self.assertIn("timing_basis", assembly)
        self.assertEqual(assembly["first_payload_received_ms_excluding_pause"], assembly["first_payload_received_ms"])
        frame = next(e["output"] for e in run.events if e["kind"] == "model.stream")
        self.assertIn("arrival_ms_excluding_pause", frame)
        self.assertEqual(frame["assembled_so_far"]["content_characters"], 2)

    def test_stream_failure_keeps_the_partial_assembly(self):
        # I-08: 스트림이 중간에 실패하면 그때까지 합친 content·도구 조각이 어떤 이벤트에도 남지 않았다.
        parts = [
            chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":'}}]}),
            chunk({"content": "부분"}),
            "ERROR",
        ]
        run = self.run_with(parts)
        self.assertEqual(run.status, "failed")
        partial = next(e for e in run.events if e["kind"] == "model.stream.partial")
        self.assertEqual(partial["output"]["frames"], 2)
        self.assertEqual(partial["output"]["tool_calls_so_far"][0]["function"]["arguments"], '{"path":')
        self.assertEqual(partial["output"]["message_so_far"]["content"], "부분")
        self.assertEqual(partial["status"], "warning")


if __name__ == "__main__":
    unittest.main()
