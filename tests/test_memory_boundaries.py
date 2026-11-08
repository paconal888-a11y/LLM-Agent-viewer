"""독립 검토에서 재현한 대화 저장·요약 경계 결함의 회귀 검사. 모두 FixtureRuntime(스크립트된 응답)이다."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from local_agent_lab.context_memory import (
    ARCHIVE_LIMIT,
    archive_line,
    archive_path,
    inspect,
    parse_archive,
    read_archive,
)
from local_agent_lab.harness import Harness
from local_agent_lab.trace import ROOT, Run
from test_harness import FixtureRuntime, response, tool_call

QUIET = {"rules": False, "skills": False, "commands": False}


class MemoryBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        shutil.copytree(ROOT / "demo_workspace", self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def harness(self, *responses):
        return Harness(self.workspace, self.root / "state", runtime=FixtureRuntime(*responses))

    def requests(self, run):
        return [e for e in run.events if e["kind"] == "model.request"]

    def test_one_large_tool_result_does_not_poison_a_compaction_session(self):
        # M-01: 기본 memory.json에서 8 KB 파일 1회 읽기만으로 이후 요약 켠 요청이 전부 영구 실패였다.
        (self.workspace / "notes" / "big.txt").write_text("x" * 65536, encoding="utf-8")
        harness = self.harness(
            response({"tool_calls": [tool_call("read_file", path="notes/big.txt")]}),
            response({"content": "읽었습니다."}),
            *[response({"content": "요약 " + str(i)}) for i in range(6)],
            *[response({"content": "답 " + str(i)}) for i in range(6)],
        )
        first = Run("큰 파일 읽기", "big-turn", features={**QUIET, "compaction": True})
        harness.execute(first)
        self.assertEqual(first.status, "completed", first.error)
        archive = archive_path(harness, "big-turn")
        before = archive.read_bytes()
        statuses = []
        for index in range(3):
            run = Run("계속 " + str(index), "big-turn", features={**QUIET, "compaction": True})
            harness.execute(run)
            statuses.append((run.status, run.error))
            self.assertTrue(archive.read_bytes().startswith(before), "원본 archive는 그대로 보존된다")
            before = archive.read_bytes()
        self.assertEqual([s for s, _ in statuses], ["completed"] * 3, statuses)
        starts = [e for run in [first] for e in run.events if e["kind"] == "memory.compaction.start"]
        self.assertEqual(starts, [], "첫 실행에는 요약할 이전 대화가 없다")

    def test_compaction_shrinks_oversized_records_and_records_the_placeholder(self):
        (self.workspace / "notes" / "big.txt").write_text("y" * 30000, encoding="utf-8")
        harness = self.harness(
            response({"tool_calls": [tool_call("read_file", path="notes/big.txt")]}),
            response({"content": "첫 답"}),
            response({"content": "두 번째 답"}),
            response({"content": "요약문"}),
            response({"content": "세 번째 답"}),
        )
        for prompt in ("첫 요청 ORBIT-731", "두 번째 요청"):
            run = Run(prompt, "shrink", features={**QUIET, "compaction": True})
            harness.execute(run)
            self.assertEqual(run.status, "completed", run.error)
        third = Run("세 번째", "shrink", features={**QUIET, "compaction": True})
        harness.execute(third)
        self.assertEqual(third.status, "completed", third.error)
        start = next(e for e in third.events if e["kind"] == "memory.compaction.start")
        self.assertTrue(start["output"]["shrunk_records"], "예산보다 큰 레코드는 줄여서 요약한다")
        summary_request = next(e for e in self.requests(third) if e["model_purpose"] == "compaction")
        body = summary_request["input"]["messages"][1]["content"]
        self.assertIn("생략", body)
        self.assertIn("ORBIT-731", body, "사용자 메시지는 보존된다")
        self.assertLess(len(body), 4000 + 1000)

    def test_line_separator_characters_do_not_break_the_archive(self):
        # M-02: U+2028/U+2029/U+0085 한 글자로 archive JSONL이 깨져 요약·대화 저장소가 영구 실패였다.
        (self.workspace / "notes" / "web.txt").write_text("첫 줄 둘째 줄 셋째\u0085 ORBIT-731", encoding="utf-8")
        harness = self.harness(
            response({"tool_calls": [tool_call("read_file", path="notes/web.txt")]}),
            response({"content": "읽음"}),
            response({"content": "다음 답"}),
        )
        first = Run("web.txt 읽기", "lines", features=QUIET)
        harness.execute(first)
        self.assertEqual(first.status, "completed", first.error)
        archive = archive_path(harness, "lines")
        text = archive.read_text(encoding="utf-8")
        self.assertEqual(text.count("\n"), 1)
        self.assertNotIn(" ", text)
        records, _ = read_archive(harness, "lines")
        self.assertEqual(len(records), 1)
        self.assertIn(" ", json.dumps(records, ensure_ascii=False))
        self.assertEqual(len(inspect(harness, "lines")["records"]), 1)
        second = Run("계속", "lines", features={**QUIET, "compaction": True})
        harness.execute(second)
        self.assertEqual(second.status, "completed", second.error)

    def test_corrupt_archive_lines_are_skipped_and_reported(self):
        harness = self.harness(response({"content": "답"}))
        archive = archive_path(harness, "corrupt")
        archive.parent.mkdir(parents=True)
        good = {"run_id": "r1", "messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]}
        archive.write_text(archive_line(good) + '{"run_id": "broken", "messages": [{"role": "us', encoding="utf-8")
        records, corrupt = parse_archive(archive.read_text(encoding="utf-8"))
        self.assertEqual(len(records), 1)
        self.assertEqual(corrupt[0]["line"], 2)
        state = inspect(harness, "corrupt")
        self.assertEqual(len(state["records"]), 1)
        self.assertEqual(state["corrupt_lines"][0]["line"], 2)
        run = Run("계속", "corrupt", features={**QUIET, "compaction": True})
        harness.execute(run)
        self.assertEqual(run.status, "completed", run.error)
        self.assertTrue(any(e["kind"] == "memory.archive.corrupt" for e in run.events))

    def test_summary_range_follows_run_ids_after_archive_regeneration(self):
        # M-03: 정수 오프셋만 믿어 archive 재생성 뒤 새 턴을 "이미 요약됨"으로 조용히 제외했다.
        harness = self.harness(response({"content": "새 답"}), response({"content": "요약"}), response({"content": "최종"}))
        state_path = harness.history_path("regen")
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "turns": [],
                    "turn_ids": [],
                    "summary": "이전 요약",
                    "summary_through": 3,
                    "summary_source_run_ids": ["old0", "old1", "old2"],
                    "archive_turn_count": 3,
                }
            ),
            encoding="utf-8",
        )
        # archive는 삭제된 상태(재생성 전). 요약을 끄고 새 사실을 말한다.
        first = Run("NEW-FACT-ZETA 기억해", "regen", features=QUIET)
        harness.execute(first)
        self.assertEqual(first.status, "completed", first.error)
        second = Run("아까 말한 사실은?", "regen", features={**QUIET, "compaction": True})
        harness.execute(second)
        self.assertEqual(second.status, "completed", second.error)
        self.assertTrue(any(e["kind"] == "memory.compaction.range.mismatch" for e in second.events))
        answer_request = next(e for e in self.requests(second) if e["model_purpose"] == "answer")
        self.assertIn("NEW-FACT-ZETA", json.dumps(answer_request["input"], ensure_ascii=False))

    def test_harness_authored_messages_keep_their_origin_across_turns(self):
        # M-04: 하네스가 넣은 user 메시지(완료 조건·보완 요청)가 다음 턴에 출처 없이 복원되고 요약기에 사용자 발화로 전달됐다.
        harness = self.harness(
            response({"content": "읽지 않고 답합니다."}),
            response({"tool_calls": [tool_call("read_file", path="notes/agent-guide.md")]}),
            response({"content": "읽었습니다."}),
            response({"content": "두 번째 답"}),
            response({"content": "요약"}),
            response({"content": "세 번째 답"}),
        )
        first = Run(
            "파일을 읽어줘",
            "origins",
            features=QUIET,
            requirements=[{"tool": "read_file", "arguments": {"path": "notes/agent-guide.md"}}],
        )
        harness.execute(first)
        self.assertEqual(first.status, "completed", first.error)
        record, _ = read_archive(harness, "origins")
        origins = record[-1]["origins"]
        self.assertEqual(len(origins), len(record[-1]["messages"]))
        harness_user = [o for o in origins if o["origin"] == "harness" and o["role"] == "user"]
        self.assertGreaterEqual(len(harness_user), 2, origins)
        self.assertIn("완료 조건", harness_user[0]["reason"])
        second = Run("이어서", "origins", features=QUIET)
        harness.execute(second)
        segments = self.requests(second)[0]["output"]["segments"]
        restored = [s for s in segments if s.get("restored_from_turn")]
        self.assertTrue(restored)
        self.assertTrue(any(s["origin"] == "harness" and s["role"] == "user" for s in restored))
        self.assertTrue(any(s["origin"] == "user" and s["role"] == "user" for s in restored))
        self.assertFalse(any(s["origin"] == "unknown" for s in restored))
        third = Run("요약해서 이어서", "origins", features={**QUIET, "compaction": True})
        (self.workspace / "memory.json").write_text(
            json.dumps({**json.loads((self.workspace / "memory.json").read_text(encoding="utf-8")), "trigger_characters": 100}),
            encoding="utf-8",
        )
        harness.execute(third)
        self.assertEqual(third.status, "completed", third.error)
        summary_request = next(e for e in self.requests(third) if e["model_purpose"] == "compaction")
        body = summary_request["input"]["messages"][1]["content"]
        self.assertIn('"origin": "harness"', body)
        self.assertIn("사용자 발화 아님", body)

    def test_compaction_mode_caps_raw_turns_and_names_the_omitted_ones(self):
        # M-05: 요약 모드에서는 미요약 원문 전체가 상한 없이 messages에 들어갔다.
        (self.workspace / "notes" / "huge.txt").write_text("z" * 60000, encoding="utf-8")
        calls = []
        for index in range(10):
            call = tool_call("read_file", path="notes/huge.txt")
            call["id"] = "c" + str(index)
            calls.append(call)
        harness = self.harness(
            response({"tool_calls": calls[:6]}),
            response({"tool_calls": calls[6:]}),
            response({"content": "다 읽음"}),
            response({"content": "다음"}),
        )
        first = Run("많이 읽기", "cap", features={**QUIET, "compaction": True})
        harness.execute(first)
        self.assertEqual(first.status, "completed", first.error)
        second = Run("이어서", "cap", features={**QUIET, "compaction": True})
        harness.execute(second)
        self.assertEqual(second.status, "completed", second.error)
        select = next(e for e in second.events if e["kind"] == "memory.context.select")
        self.assertEqual(select["output"]["omitted_run_ids"], [first.id])
        request = next(e for e in self.requests(second) if e["model_purpose"] == "answer")
        self.assertLess(len(json.dumps(request["input"], ensure_ascii=False).encode("utf-8")), 524288)

    def test_memory_view_survives_an_archive_over_the_limit(self):
        # M-07: archive가 4 MiB를 넘거나 손상되면 대화 저장소 화면 전체가 400이었다.
        harness = self.harness()
        archive = archive_path(harness, "huge")
        archive.parent.mkdir(parents=True)
        line = archive_line({"run_id": "r", "messages": [{"role": "user", "content": "k" * 100000}]})
        with archive.open("w", encoding="utf-8", newline="") as handle:
            for index in range(50):
                handle.write(line.replace('"run_id": "r"', '"run_id": "r%d"' % index))
        self.assertGreater(archive.stat().st_size, ARCHIVE_LIMIT)
        state = inspect(harness, "huge")
        self.assertTrue(state["records_truncated"])
        self.assertGreater(state["archive_bytes"], ARCHIVE_LIMIT)
        self.assertTrue(state["records"])
        self.assertEqual(state["records"][-1]["run_id"], "r49")

    def test_next_request_reports_turns_omitted_from_the_active_state(self):
        # M-10: 큰 턴이 활성 상태에서 빠진 사실이 다음 요청 이벤트에 없었다.
        (self.workspace / "notes" / "huge.txt").write_text("w" * 60000, encoding="utf-8")
        calls = []
        for index in range(10):
            call = tool_call("read_file", path="notes/huge.txt")
            call["id"] = "d" + str(index)
            calls.append(call)
        harness = self.harness(
            response({"tool_calls": calls[:6]}),
            response({"tool_calls": calls[6:]}),
            response({"content": "끝"}),
            response({"content": "다음"}),
        )
        first = Run("많이 읽기", "omit", features=QUIET)
        harness.execute(first)
        second = Run("다음", "omit", features=QUIET)
        harness.execute(second)
        self.assertEqual(second.status, "completed", second.error)
        omitted = next(e for e in second.events if e["kind"] == "memory.state.omitted")
        self.assertEqual(omitted["output"]["omitted_recent_turns"], 1)
        select = next(e for e in second.events if e["kind"] == "memory.select")
        self.assertEqual(select["input"]["omitted_recent_turns_in_state"], 1)
        self.assertEqual(select["input"]["archive_turn_count"], 1)


if __name__ == "__main__":
    unittest.main()
