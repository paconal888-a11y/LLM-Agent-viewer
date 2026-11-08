import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from local_agent_lab.runtime import LocalRuntime, RuntimeFailure, smoke


class ScriptedRuntime:
    """Protocol fixture: does not run a language model."""

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def request(self, route, payload):
        self.requests.append((route, payload))
        return next(self.responses)


def chat_call(name="add_numbers", arguments='{"a":17,"b":25}'):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": arguments,
                            },
                        }
                    ],
                }
            }
        ]
    }


class ToolLoopTests(unittest.TestCase):
    def test_chat_round_trip_sends_actual_tool_result(self):
        runtime = ScriptedRuntime(
            chat_call(),
            {"choices": [{"message": {"role": "assistant", "content": "42"}}]},
        )
        result = smoke(runtime, "fixture-model")
        self.assertEqual(result["tool_result"], 42)
        self.assertEqual(
            runtime.requests[1][1]["messages"][-1],
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "42",
            },
        )

    def test_responses_round_trip_preserves_call_and_response_ids(self):
        runtime = ScriptedRuntime(
            {
                "id": "resp-1",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call-2",
                        "name": "add_numbers",
                        "arguments": '{"a":17,"b":25}',
                    }
                ],
            },
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "42"}],
                    }
                ]
            },
        )
        self.assertEqual(
            smoke(runtime, "fixture-model", "responses")["status"], "passed"
        )
        second = runtime.requests[1][1]
        self.assertEqual(second["previous_response_id"], "resp-1")
        self.assertEqual(second["input"][0]["call_id"], "call-2")
        self.assertEqual(second["input"][0]["output"], "42")

    def test_correct_answer_without_tool_use_does_not_pass(self):
        runtime = ScriptedRuntime(
            {"choices": [{"message": {"role": "assistant", "content": "42"}}]}
        )
        with self.assertRaises(RuntimeFailure):
            smoke(runtime, "fixture-model")
        self.assertEqual(len(runtime.requests), 1)

    def test_unknown_tool_is_rejected_before_followup(self):
        runtime = ScriptedRuntime(chat_call(name="run_shell"))
        with self.assertRaises(RuntimeFailure):
            smoke(runtime, "fixture-model")
        self.assertEqual(len(runtime.requests), 1)

    def test_invalid_arguments_are_rejected_before_execution(self):
        for arguments in (
            '{"a":"17","b":25}',
            '{"a":true,"b":25}',
            '{"a":17,"b":25,"command":"extra"}',
            '{"a":16,"b":26}',
            "[]",
            "not-json",
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(RuntimeFailure):
                    smoke(
                        ScriptedRuntime(chat_call(arguments=arguments)), "fixture-model"
                    )

    def test_final_answer_must_match_tool_result(self):
        runtime = ScriptedRuntime(
            chat_call(),
            {"choices": [{"message": {"content": "43"}}]},
        )
        with self.assertRaises(RuntimeFailure):
            smoke(runtime, "fixture-model")

    def test_malformed_responses_report_protocol_failure(self):
        for api, response in (
            ("chat", {"choices": []}),
            ("responses", {"output": None}),
        ):
            with self.subTest(api=api):
                with self.assertRaises(RuntimeFailure):
                    smoke(ScriptedRuntime(response), "fixture-model", api)


class LocalTransportTests(unittest.TestCase):
    def test_remote_or_credentialed_endpoints_are_rejected(self):
        for url in (
            "https://example.com/v1",
            "http://192.168.0.1/v1",
            "file:///models",
            "http://user:pass@localhost:1234/v1",
            "http://127.0.0.1/v1?token=anything",
        ):
            with self.subTest(url=url):
                with self.assertRaises(RuntimeFailure):
                    LocalRuntime(url)

    def test_invalid_timeouts_are_rejected(self):
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(RuntimeFailure):
                    LocalRuntime(timeout=timeout)

    def test_malformed_urls_report_a_runtime_failure(self):
        for url in ("http://[invalid/v1", "http://127.0.0.1:not-a-port/v1"):
            with self.subTest(url=url):
                with self.assertRaises(RuntimeFailure):
                    LocalRuntime(url)

    def test_http_model_listing_and_redirect_rejection(self):
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                if self.path == "/redirect/models":
                    self.send_response(302)
                    self.send_header("Location", "/v1/models")
                    self.end_headers()
                    return
                payload = json.dumps({"data": [{"id": "local-fixture"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            self.assertEqual(LocalRuntime(origin + "/v1").models(), ["local-fixture"])
            with self.assertRaisesRegex(RuntimeFailure, "HTTP 302"):
                LocalRuntime(origin + "/redirect").models()
            self.assertEqual(paths, ["/v1/models", "/redirect/models"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
