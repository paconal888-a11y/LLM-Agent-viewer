"""실행 중 컨텍스트 압축. 모델 응답은 픽스처이고 하네스 루프·도구·입력 조립은 실제로 실행한다."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from local_agent_lab.compaction import input_view, new_view, safe_end
from local_agent_lab.diagnosis import diagnose
from local_agent_lab.harness import Harness
from local_agent_lab.trace import ROOT, Run
from test_harness import FixtureRuntime, response, tool_call


def reply(message):
    """usage를 보고하지 않는 응답. 토큰 추정이 문자 수 기준으로 동작한다."""
    result = response(message)
    result.pop("usage")
    return result


def completions(runtime):
    return [p for route, p in runtime.requests if route.endswith("completions")]


class CompactionLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"
        shutil.copytree(ROOT / "demo_workspace", self.workspace)
        (self.workspace / "notes" / "big.md").write_text("큰 문서 " * 400, encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def settings(self, **context):
        (self.workspace / "agent-lens.json").write_text(
            json.dumps({"context": context}), encoding="utf-8"
        )

    def execute(self, *responses, **features):
        runtime = FixtureRuntime(*responses)
        harness = Harness(self.workspace, self.root / "state", runtime=runtime)
        run = Run("큰 문서를 읽어줘", "compact", features=features or None)
        harness.execute(run)
        return run, runtime

    def kinds(self, run, kind):
        return [e for e in run.events if e["kind"] == kind]

    def read(self):
        return reply({"tool_calls": [tool_call("read_file", path="notes/big.md")]})

    def add(self, a=1, b=2):
        return reply({"tool_calls": [tool_call("add_numbers", a=a, b=b)]})

    def test_small_input_only_records_the_check(self):
        run, runtime = self.execute(reply({"content": "끝"}))
        self.assertEqual(run.status, "completed", run.error)
        checks = self.kinds(run, "context.check")
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0]["output"]["needed"])
        self.assertIn("어림", checks[0]["output"]["estimate_basis"])
        self.assertFalse(self.kinds(run, "context.prune"))

    def test_switch_off_records_nothing(self):
        run, _ = self.execute(reply({"content": "끝"}), turn_compaction=False)
        self.assertFalse(self.kinds(run, "context.check"))

    def test_reported_tokens_set_the_estimate_for_the_next_call(self):
        run, _ = self.execute(
            response({"tool_calls": [tool_call("add_numbers", a=1, b=2)]}),
            reply({"content": "끝"}),
        )
        checks = self.kinds(run, "context.check")
        self.assertIn("어림", checks[0]["output"]["estimate_basis"])
        self.assertIn("보고 토큰", checks[1]["output"]["estimate_basis"])

    def test_old_tool_results_are_pruned_without_a_model_call(self):
        self.settings(compact_at_tokens=800, keep_recent_messages=2, prune_tool_characters=200, max_summaries=0)
        run, runtime = self.execute(self.read(), self.add(), reply({"content": "끝"}))
        self.assertEqual(run.status, "completed", run.error)
        requests = completions(runtime)
        self.assertEqual(len(requests), 3)
        first_result = next(m for m in requests[2]["messages"] if m["role"] == "tool")
        self.assertIn("omitted_by_harness", first_result["content"])
        self.assertNotIn("큰 문서 큰 문서", json.dumps(requests[2], ensure_ascii=False))
        self.assertEqual(len(self.kinds(run, "context.prune")), 1)

    def test_transcript_keeps_the_original_after_pruning(self):
        self.settings(compact_at_tokens=800, keep_recent_messages=2, prune_tool_characters=200, max_summaries=0)
        self.execute(self.read(), self.add(), reply({"content": "끝"}))
        state = json.loads((self.root / "state" / "sessions" / "compact.json").read_text(encoding="utf-8"))
        self.assertIn("큰 문서 큰 문서", json.dumps(state["turns"], ensure_ascii=False))

    def test_summary_replaces_old_messages_and_keeps_tool_pairs_valid(self):
        self.settings(compact_at_tokens=200, keep_recent_messages=2, prune_tool_characters=200)
        run, runtime = self.execute(
            self.read(), self.add(), reply({"content": "big.md를 읽음"}), reply({"content": "끝"})
        )
        self.assertEqual(run.status, "completed", run.error)
        requests = completions(runtime)
        self.assertEqual(len(requests), 4)
        self.assertEqual(requests[2]["tools"], [])
        self.assertEqual(requests[2]["temperature"], 0)
        answer = requests[3]["messages"]
        self.assertTrue(any("지금까지의 작업 요약" in (m.get("content") or "") for m in answer))
        self.assertFalse(any("notes/big.md" in json.dumps(m.get("tool_calls") or "", ensure_ascii=False) for m in answer))
        for index, message in enumerate(answer):
            if message["role"] == "tool":
                self.assertIn(answer[index - 1]["role"], {"assistant", "tool"})
        result = self.kinds(run, "context.compact.result")[0]
        self.assertTrue(result["output"]["transcript_preserved"])
        self.assertEqual(
            [e["output"].get("purpose") for e in self.kinds(run, "model.request")],
            [None, None, "compaction", None],
        )

    def test_summary_reads_the_original_tool_result_not_the_placeholder(self):
        # 실제 모델 기록에서 찾은 결함: 생략 자리표시자를 요약해 파일 내용이 영구히 사라졌다.
        self.settings(compact_at_tokens=200, keep_recent_messages=2, prune_tool_characters=200)
        run, runtime = self.execute(
            self.read(), self.add(), reply({"content": "요약"}), reply({"content": "끝"})
        )
        self.assertEqual(run.status, "completed", run.error)
        self.assertTrue(self.kinds(run, "context.prune"))
        source = completions(runtime)[2]["messages"][1]["content"]
        self.assertIn("큰 문서 큰 문서", source)
        self.assertNotIn("omitted_by_harness", source)

    def test_segments_describe_the_request_that_was_actually_sent(self):
        self.settings(compact_at_tokens=200, keep_recent_messages=2, prune_tool_characters=200)
        run, _ = self.execute(self.read(), self.add(), reply({"content": "요약"}), reply({"content": "끝"}))
        requests = [e for e in self.kinds(run, "model.request") if e["model_purpose"] == "answer"]
        last = requests[-1]
        count = len(last["input"]["messages"])
        indices = [s["message_index"] for s in last["output"]["segments"] if s["message_index"] is not None]
        self.assertTrue(all(0 <= i < count for i in indices))
        summary = next(s for s in last["output"]["segments"] if s["label"] == "작업 요약")
        self.assertEqual(summary["origin"], "harness")
        self.assertIn("지금까지의 작업 요약", last["input"]["messages"][summary["message_index"]]["content"])
        self.assertGreater(last["output"]["transcript_message_count"], count)

    def test_failed_summary_keeps_the_run_going(self):
        self.settings(compact_at_tokens=200, keep_recent_messages=2, prune_tool_characters=200)
        run, runtime = self.execute(self.read(), self.add(), reply({"content": "   "}), reply({"content": "끝"}))
        self.assertEqual(run.status, "completed", run.error)
        self.assertEqual(self.kinds(run, "context.compact.failed")[0]["status"], "warning")
        self.assertEqual(run.model_purpose, "answer")
        self.assertFalse(any("지금까지의 작업 요약" in (m.get("content") or "") for m in completions(runtime)[-1]["messages"]))

    def test_summary_budget_is_respected(self):
        self.settings(compact_at_tokens=100, keep_recent_messages=2, prune_tool_characters=200, max_summaries=1)
        run, _ = self.execute(
            self.read(), self.add(), reply({"content": "요약"}), self.add(3, 4), reply({"content": "끝"})
        )
        self.assertEqual(run.status, "completed", run.error)
        self.assertEqual(len(self.kinds(run, "context.compact.result")), 1)

    def test_diagnosis_points_out_that_the_input_was_reduced(self):
        self.settings(compact_at_tokens=200, keep_recent_messages=2, prune_tool_characters=200)
        run, _ = self.execute(self.read(), self.add(), reply({"content": "요약"}), reply({"content": "끝"}))
        report = diagnose(run.snapshot())
        check = next(c for layer in report["layers"] for c in layer["checks"] if c["id"] == "context_compacted")
        self.assertEqual(check["result"], "warn")


class ViewTests(unittest.TestCase):
    messages = [
        {"role": "system", "content": "규칙"},
        {"role": "user", "content": "요청"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "결과"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "2"}]},
        {"role": "tool", "content": "결과2"},
    ]

    def test_view_without_compaction_is_the_transcript(self):
        request, origin = input_view(self.messages, new_view(2))
        self.assertEqual(request, self.messages)
        self.assertEqual(origin, list(range(6)))

    def test_view_with_summary_skips_the_replaced_range(self):
        view = {**new_view(2), "summary": "요약", "summary_end": 4}
        request, origin = input_view(self.messages, view)
        self.assertEqual(origin, [0, 1, "summary", 4, 5])
        self.assertIn("요약", request[2]["content"])

    def test_cut_never_separates_a_tool_call_from_its_result(self):
        self.assertEqual(safe_end(self.messages, 5, 2), 4)
        self.assertEqual(safe_end(self.messages, 3, 2), 2)
        self.assertEqual(safe_end(self.messages, 4, 2), 4)


if __name__ == "__main__":
    unittest.main()
