"""독립 검토에서 재현한 관측값 정확성 결함의 회귀 검사(usage 미보고, 하위 에이전트 정지 시간, 중복 tool id, 요청 원본 설정)."""

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from local_agent_lab.features import run_metrics
from local_agent_lab.harness import Harness
from local_agent_lab.trace import ROOT, Run
from local_agent_lab.web import Application
from test_harness import FixtureRuntime, response, tool_call

QUIET = {"rules": False, "skills": False, "commands": False, "memory": False}


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        shutil.copytree(ROOT / "demo_workspace", self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def harness(self, *responses):
        return Harness(self.workspace, self.root / "state", runtime=FixtureRuntime(*responses))

    def test_missing_usage_is_reported_as_unknown_not_zero(self):
        # I-03: 실행기가 usage를 보고하지 않으면 집계가 0토큰으로 표시됐다.
        silent = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "답"}}]}
        run = Run("usage 없음", "no-usage", features=QUIET)
        self.harness(silent).execute(run)
        self.assertEqual(run.status, "completed", run.error)
        snapshot = run.snapshot()
        self.assertEqual(snapshot["usage_reports"], {"reported": 0, "missing": 1})
        self.assertIsNone(snapshot["totals"]["prompt_tokens"])
        self.assertEqual(snapshot["totals"]["usage_missing_calls"], 1)
        metrics = run_metrics(snapshot)
        self.assertIsNone(metrics["prompt_tokens"])
        self.assertEqual(metrics["usage_missing_calls"], 1)
        # 보고가 있으면 그대로 더한다(기존 fixture는 10/5).
        run2 = Run("usage 있음", "usage", features=QUIET)
        self.harness(response({"content": "답"})).execute(run2)
        self.assertEqual(run2.snapshot()["totals"]["prompt_tokens"], 10)
        self.assertEqual(run2.snapshot()["totals"]["usage_missing_calls"], 0)

    def test_duplicate_tool_call_ids_are_rejected_as_a_model_format_error(self):
        # I-06: 서로 다른 호출이 같은 id를 가져도 둘 다 실행되어 결과를 짝지을 수 없었다.
        first = tool_call("add_numbers", a=1, b=2)
        second = tool_call("add_numbers", a=3, b=4)
        first["id"] = second["id"] = "call_same"
        run = Run("중복 id", "dup-id", features=QUIET)
        self.harness(response({"tool_calls": [first, second]})).execute(run)
        self.assertEqual(run.status, "failed")
        self.assertIn("tool_call id", run.error)
        self.assertEqual(run.tool_calls, 0)

    def test_parent_step_pause_is_subtracted_from_child_timing(self):
        # I-02/W-07: 부모의 단계 정지가 하위 Run의 paused_seconds에 쌓이지 않아 forward·active 시간에 섞였다.
        parent = Run("부모", "pause-parent", step=True)
        child = Run("자식", "pause-child", parent_run=parent, agent_name="Explore")
        parent.children.append(child)
        done = threading.Event()

        def work():
            started = time.perf_counter()
            child.emit("child.work", "하위 작업", "정지 포함 구간", pause=True)
            child.wall = time.perf_counter() - started
            done.set()

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        with parent.condition:
            self.assertTrue(parent.condition.wait_for(lambda: parent.status == "paused", timeout=3))
        time.sleep(0.3)
        parent.control("continue")
        self.assertTrue(done.wait(3))
        self.assertGreaterEqual(parent.paused_seconds, 0.25)
        self.assertGreaterEqual(child.paused_seconds, 0.25, "부모에서 멈춘 시간이 자식에도 반영된다")
        self.assertLessEqual(child.paused_seconds, child.wall + 0.01)
        snapshot = child.snapshot()
        self.assertLess(snapshot["active_seconds"], 0.2)

    def test_snapshot_keeps_the_requested_settings_before_mode_preparation_changes_them(self):
        # W-04: plan 모드는 writes를 끄고 team 모드는 delegate_tasks 조건을 덧붙이며 continue는 step을 끈다.
        run = Run(
            "계획",
            "request-original",
            step=True,
            features={**QUIET, "writes": True},
            requirements=[{"tool": "read_file", "arguments": {"path": "AGENTS.md"}}],
            mode="plan",
            engine={"temperature": 0.7, "top_k": 3},
        )
        harness = self.harness(response({"content": "계획입니다."}))
        worker = threading.Thread(target=harness.execute, args=(run,), daemon=True)
        worker.start()
        with run.condition:
            self.assertTrue(run.condition.wait_for(lambda: run.status == "paused", timeout=3))
        run.control("continue")
        worker.join(5)
        snapshot = run.snapshot()
        self.assertFalse(snapshot["step_mode"])
        self.assertTrue(snapshot["request"]["step"])
        self.assertFalse(snapshot["features"]["writes"])
        self.assertTrue(snapshot["request"]["features"]["writes"])
        self.assertEqual(snapshot["request"]["requirements"], [{"tool": "read_file", "arguments": {"path": "AGENTS.md"}}])
        self.assertEqual(snapshot["request"]["engine"]["temperature"], 0.7)
        self.assertEqual(snapshot["request"]["engine"]["top_k"], 3)
        self.assertEqual(snapshot["request"]["mode"], "plan")

    def test_next_is_only_accepted_while_paused(self):
        # W-10: running 상태에서 next를 누르면 허가가 쌓여 다음 정지 지점을 조용히 건너뛰었다.
        run = Run("step", "permits", step=True)
        run.control("next")
        run.control("next")
        self.assertEqual(run.permits, 0)
        started = time.perf_counter()

        def work():
            run.emit("a", "첫 정지", "x")
            run.emit("b", "둘째 정지", "y")

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        with run.condition:
            self.assertTrue(run.condition.wait_for(lambda: run.status == "paused", timeout=3))
        run.control("next")
        run.control("next")  # 한 번의 정지에서는 한 단계만 허가한다.
        time.sleep(0.2)
        self.assertEqual(run.status, "paused")
        self.assertEqual(len(run.events), 2)
        run.control("continue")
        thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_samples_only_blocks_experiments_and_project_writes(self):
        # W-05/D-10: 샘플 모드의 서버 측 차단이 /api/runs·/api/backend에만 있었다.
        app = Application(self.harness(), samples_only=True)
        with self.assertRaises(ValueError):
            app.experiments.start("비교", None, None)
        self.assertIsNone(app.experiment_active)

    def test_run_metrics_count_tools_from_the_answer_request_not_the_summary_request(self):
        # W-06: 요약 요청이 먼저 오면 공개 도구 수가 0으로 보였다.
        snapshot = {
            "events": [
                {"kind": "model.request", "model_purpose": "compaction", "input": {"tools": []}},
                {"kind": "model.request", "model_purpose": "answer", "input": {"tools": [{"function": {"name": "read_file"}}, {"function": {"name": "check_file"}}]}},
            ],
            "totals": {"prompt_tokens": 5, "completion_tokens": 1, "usage_missing_calls": 0},
        }
        metrics = run_metrics(snapshot)
        self.assertEqual(metrics["tool_definitions"], 2)
        self.assertEqual(metrics["tool_names"], ["read_file", "check_file"])


    def test_cache_experiment_reports_same_sampling_even_when_transport_differs(self):
        # I-14: kv_cache 실험은 cache_prompt만 다른데 '같은 생성 설정 ×'로만 보여 샘플링 차이와 구분되지 않았다.
        import time

        app = Application(self.harness(response({"content": "A"}), response({"content": "B"})))
        experiment = app.experiments.start("same", left_engine={"cache_prompt": False}, right_engine={"cache_prompt": True})
        deadline = time.monotonic() + 10
        while app.experiment_active and time.monotonic() < deadline:
            time.sleep(0.01)
        result = app.experiments.get(experiment)
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertFalse(result["controls"]["same_generation"])
        self.assertTrue(result["controls"]["same_sampling"])
        self.assertTrue(result["controls"]["same_prompt"])
        self.assertIn("same_harness_code", result["controls"])
        self.assertEqual(set(result["generation_diff"]), {"cache_prompt"})


    def test_reference_hook_timer_excludes_user_pause_and_needs_no_torch(self):
        # ⑦: hook 본문에서 쓴 시간만 누적하고 그 안의 사용자 정지는 뺀다(torch 없이 검사 가능한 부분).
        from local_agent_lab.reference_engine import ReferenceRuntime

        runtime = ReferenceRuntime(autoload=False)
        run = Run("hook timer", "hook-timer")
        runtime._run = run

        def hook(module, inputs, output):
            time.sleep(0.25)  # 그중 0.2초는 사용자 정지였다고 기록한다
            run.paused_seconds += 0.2
            return "ok"

        wrapped = runtime._timed(hook)
        self.assertEqual(wrapped(None, None, None), "ok")
        self.assertGreaterEqual(runtime._hook_ms, 30)
        self.assertLess(runtime._hook_ms, 150, "정지로 기록한 200ms는 hook 비용에 넣지 않는다")
        runtime._run = None
        before = runtime._hook_ms
        wrapped(None, None, None)
        self.assertEqual(runtime._hook_ms, before, "실행 중이 아니면 측정하지 않는다")


    def test_installed_package_code_under_the_project_is_not_copied_into_snapshots(self):
        # D-06: .venv 안의 torch/transformers 함수 본문이 소스 스냅샷(과 MIT 샘플)에 들어갔다.
        import types

        from local_agent_lab.trace import ROOT as PROJECT_ROOT, code_source

        fake = PROJECT_ROOT / ".venv" / "Lib" / "site-packages" / "torchlike" / "module.py"
        existed = fake.exists()
        if not existed:
            fake.parent.mkdir(parents=True, exist_ok=True)
            fake.write_text("def inner():\n    return 1\n", encoding="utf-8")
        try:
            code = compile("def inner():\n    return 1\n", str(fake), "exec")
            function_code = next(c for c in code.co_consts if isinstance(c, types.CodeType))
            source = code_source(function_code)
            self.assertTrue(source["external"])
            self.assertEqual(source["code"], "")
            self.assertTrue(source["file"].startswith("<external>/.venv/"))
            self.assertEqual(source["function"], "inner")
        finally:
            if not existed:
                fake.unlink()
                for parent in (fake.parent, fake.parent.parent, fake.parent.parent.parent, fake.parent.parent.parent.parent):
                    try:
                        parent.rmdir()
                    except OSError:
                        break


if __name__ == "__main__":
    unittest.main()
