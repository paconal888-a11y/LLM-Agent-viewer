"""기록만으로 살펴볼 층을 좁히는지 확인한다. 응답은 손으로 만든 픽스처와 배포 샘플 재생이다."""

import gzip
import json
import unittest

from local_agent_lab.diagnosis import diagnose
from local_agent_lab.trace import ROOT

TOOL = {"type": "function", "function": {"name": "add_numbers", "parameters": {}}}
REQUIRED = [{"tool": "add_numbers", "arguments": {"a": 1, "b": 2}}]
CONSTRAINT = {"role": "user", "content": "완료 조건"}


def request(event_id, tools=(TOOL,), messages=(CONSTRAINT,), **extra):
    return {
        "id": event_id,
        "kind": "model.request",
        "input": {"messages": list(messages), "tools": list(tools), "max_tokens": 64},
        "output": {},
        **extra,
    }


def response(event_id, finish="stop", **extra):
    return {
        "id": event_id,
        "kind": "model.response",
        "input": {},
        "output": {
            "choices": [{"finish_reason": finish, "message": {"content": "답"}}],
            "usage": {"prompt_tokens": 100},
        },
        **extra,
    }


def planned(event_id):
    return {
        "id": event_id,
        "kind": "context.requirements",
        "input": {},
        "output": {"appended_message": CONSTRAINT},
    }


def error(event_id, text):
    return {"id": event_id, "kind": "run.error", "input": {}, "output": {"error": text}}


def gate(event_id, missing):
    return {
        "id": event_id,
        "kind": "validation.requirements",
        "input": {},
        "output": {"passed": not missing, "missing": missing},
    }


def dispatch(event_id, name="add_numbers", arguments='{"a": 9}'):
    return {
        "id": event_id,
        "kind": "tool.dispatch",
        "input": {"function": {"name": name, "arguments": arguments}},
        "output": {},
    }


def snapshot(events, status="failed", requirements=REQUIRED):
    return {"id": "r", "status": status, "requirements": requirements, "events": events}


def check(result, check_id):
    return next(
        item
        for layer in result["layers"]
        for item in layer["checks"]
        if item["id"] == check_id
    )


class LayerOrderTests(unittest.TestCase):
    def test_no_reply_points_to_the_runtime(self):
        result = diagnose(
            snapshot([planned(1), request(2), error(3, "추론 서버 연결 실패")])
        )
        self.assertEqual(result["focus"], "runtime")
        self.assertEqual(check(result, "model_replied")["event_id"], 3)
        self.assertIn("실행기", result["next_step"])

    def test_output_truncation_is_a_setting_not_a_model_failure(self):
        result = diagnose(
            snapshot([planned(1), request(2), response(3, "length"), error(4, "한도 도달")])
        )
        self.assertEqual(result["focus"], "runtime")
        self.assertIn("출력 한도 64", check(result, "output_limit")["detail"])

    def test_context_overflow_error_is_reported_as_the_runtime_layer(self):
        result = diagnose(
            snapshot([planned(1), request(2), error(3, "request exceeds the available context size")])
        )
        self.assertEqual(check(result, "context_window")["result"], "fail")
        self.assertEqual(result["focus"], "runtime")

    def test_hidden_required_tool_is_not_blamed_on_the_model(self):
        result = diagnose(
            snapshot([planned(1), request(2, tools=()), response(3), gate(4, REQUIRED), error(5, "x")])
        )
        self.assertEqual(result["focus"], "harness")
        self.assertEqual(check(result, "required_tools_exposed")["result"], "fail")
        # 공개되지 않은 도구는 '호출하지 않았다'로 세지 않는다.
        self.assertEqual(check(result, "required_tool_called")["result"], "pass")

    def test_requirements_missing_from_the_input_point_to_information(self):
        result = diagnose(
            snapshot([request(1, messages=({"role": "user", "content": "요청"},)), response(2), gate(3, REQUIRED), error(4, "x")])
        )
        self.assertEqual(result["focus"], "information")
        self.assertEqual(check(result, "requirements_in_input")["result"], "fail")

    def test_tool_result_lost_before_the_next_request_is_an_information_issue(self):
        lost = {"role": "tool", "content": "결과"}
        result = diagnose(
            snapshot(
                [
                    planned(1),
                    request(2),
                    {"id": 3, "kind": "context.append", "input": {}, "output": {"appended_message": lost}},
                    request(4),
                    response(5),
                    gate(6, REQUIRED),
                    error(7, "x"),
                ]
            )
        )
        self.assertEqual(result["focus"], "information")
        self.assertEqual(check(result, "results_in_next_input")["event_id"], 3)

    def test_model_choice_is_named_only_after_the_other_layers_are_clear(self):
        repeat = {
            "id": 6,
            "kind": "validation.repeat",
            "input": {},
            "output": {"blocked": True, "streak": 3, "limit": 3},
        }
        result = diagnose(
            snapshot([planned(1), request(2), response(3), gate(4, REQUIRED), repeat, error(7, "반복")])
        )
        self.assertEqual(result["focus"], "model")
        self.assertEqual(check(result, "required_tool_called")["detail"], "호출 0회: add_numbers")
        self.assertEqual(check(result, "repeat_guard")["result"], "fail")
        self.assertIn("반복 측정", result["next_step"])

    def test_call_budget_does_not_take_the_focus_from_the_repeating_model(self):
        result = diagnose(
            snapshot([planned(1), request(2), response(3), gate(4, REQUIRED), error(5, "모델 호출 8회 제한에 도달했습니다.")])
        )
        self.assertEqual(check(result, "call_budget")["result"], "warn")
        self.assertEqual(result["focus"], "model")

    def test_wrong_arguments_show_the_condition_and_the_actual_call(self):
        result = diagnose(
            snapshot(
                [
                    planned(1),
                    request(2),
                    response(3),
                    dispatch(4),
                    {"id": 5, "kind": "tool.validate", "parent": 4, "input": {}, "output": {}},
                    gate(6, REQUIRED),
                    error(7, "x"),
                ]
            )
        )
        detail = check(result, "requirements_met")["detail"]
        self.assertIn('"a": 1', detail)
        self.assertIn('{"a": 9}', detail)

    def test_rejected_request_and_failed_execution_are_different_layers(self):
        rejected = {"id": 5, "kind": "tool.error", "parent": 4, "input": {}, "output": {"error": "필수 인자가 빠졌습니다."}}
        result = diagnose(snapshot([request(1), response(2), dispatch(4), rejected], requirements=[]))
        self.assertEqual(check(result, "tool_request_valid")["result"], "fail")
        self.assertEqual(check(result, "tool_execution")["result"], "pass")
        validated = {"id": 5, "kind": "tool.validate", "parent": 4, "input": {}, "output": {}}
        failed = {"id": 6, "kind": "tool.error", "parent": 4, "input": {}, "output": {"error": "디스크 오류"}}
        result = diagnose(snapshot([request(1), response(2), dispatch(4), validated, failed], requirements=[]))
        self.assertEqual(check(result, "tool_request_valid")["result"], "pass")
        self.assertEqual(check(result, "tool_execution")["detail"], "디스크 오류")
        self.assertEqual(result["focus"], "harness")

    def test_unexpected_exception_points_to_the_harness_with_its_location(self):
        crashed = {
            "id": 3,
            "kind": "run.error",
            "input": {},
            "output": {"error": "'missing'", "type": "KeyError", "file": "toolkit.py", "line": 12},
        }
        result = diagnose(snapshot([request(1), response(2), crashed], requirements=[]))
        self.assertEqual(result["focus"], "harness")
        self.assertEqual(check(result, "unexpected_exception")["detail"], "KeyError · toolkit.py:12")

    def test_unknown_failure_does_not_invent_a_layer(self):
        result = diagnose(snapshot([], requirements=[]))
        self.assertIsNone(result["focus"])
        self.assertIn("좁히지 못했습니다", result["headline"])
        self.assertEqual({layer["status"] for layer in result["layers"]}, {"unknown"})


class ScopeTests(unittest.TestCase):
    def test_completed_without_requirements_is_not_called_verified(self):
        result = diagnose(snapshot([request(1), response(2)], "completed", []))
        self.assertIsNone(result["focus"])
        self.assertIn("코드가 확인한 완료 조건은 없습니다", result["headline"])
        self.assertEqual(result["verification"]["by_code"], [])
        self.assertIn("이 실행에는 완료 조건이 없습니다", result["verification"]["not_checked"][0])

    def test_completed_lists_what_the_code_actually_checked(self):
        required = [REQUIRED[0], {"tool": "b", "arguments": {}, "result": {"ok": True}}]
        final = {
            "id": 5,
            "kind": "validation.final",
            "input": {"changed_files": ["a.json"]},
            "output": {"passed": True, "unchecked_files": []},
        }
        result = diagnose(snapshot([planned(1), request(2), response(3), gate(4, []), final], "completed", required))
        self.assertEqual(
            result["verification"]["by_code"],
            ["완료 조건 2개를 실제 도구 결과와 대조 (결과 값까지 1개, 나머지는 도구·인자만)", "수정한 파일 1개의 검사 통과"],
        )
        self.assertIn("최종 답변 문장의 사실 여부", result["verification"]["not_checked"])

    def test_compaction_requests_are_not_read_as_answer_requests(self):
        result = diagnose(
            snapshot([request(1, tools=(), model_purpose="compaction"), response(2, model_purpose="compaction")], "completed", [])
        )
        self.assertEqual(result["layers"][0]["status"], "unknown")

    def test_child_agent_events_stay_out_of_the_main_analysis(self):
        child = {**dispatch(5), "agent_id": "child"}
        result = diagnose(snapshot([request(1), response(2), child], "completed", []))
        self.assertEqual([c for layer in result["layers"] for c in layer["checks"] if c["id"] == "tool_request_valid"], [])

    def test_failed_subagent_is_surfaced_even_when_the_run_completed(self):
        joined = {
            "id": 3,
            "kind": "tool.result",
            "input": {},
            "output": {"all_completed": False, "results": [{"status": "failed"}, {"status": "completed"}]},
        }
        result = diagnose(snapshot([request(1), response(2), joined], "completed", []))
        self.assertEqual(check(result, "subagents")["detail"][:6], "완료 1/2")
        self.assertEqual(result["layers"][1]["status"], "attention")

    def test_stopped_run_is_not_analysed_as_a_failure(self):
        result = diagnose(snapshot([planned(1), request(2)], "stopped"))
        self.assertIsNone(result["focus"])
        self.assertIn("중지", result["headline"])


class SampleRecordTests(unittest.TestCase):
    """배포 샘플(저장 기록 재생)에 적용해 형식 호환과 대표 판정을 고정한다."""

    def load(self):
        index = json.loads((ROOT / "samples" / "index.json").read_text(encoding="utf-8"))
        result = {}
        for item in index:
            data = json.loads(gzip.decompress((ROOT / "samples" / (item["id"] + ".json.gz")).read_bytes()))
            data["sample"]["source_run_id"] = data["id"]
            result[item["slug"]] = data
        return result

    def test_every_sample_can_be_analysed_and_keeps_the_expected_focus(self):
        samples = self.load()
        self.assertEqual(len(samples), 16)
        results = {slug: diagnose(data) for slug, data in samples.items()}
        self.assertEqual(results["repeat-guard"]["focus"], "model")
        self.assertEqual(results["repeat-before"]["focus"], "model")
        self.assertEqual(len(results["verified-write"]["verification"]["by_code"]), 2)
        self.assertIn("코드가 확인한 완료 조건은 없습니다", results["tensors"]["headline"])
        # 이 샘플을 기록한 버전은 완료 조건을 모델 입력에 넣지 않았다.
        self.assertEqual(check(results["feedback"], "requirements_in_input")["result"], "fail")
        # 주 에이전트의 요약은 코드가 대조하지 않는다는 사실을 드러낸다.
        self.assertEqual(check(results["team"], "subagent_summary")["result"], "warn")


if __name__ == "__main__":
    unittest.main()
