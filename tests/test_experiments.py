"""조건별 반복 측정. 모델 응답은 FixtureRuntime 픽스처이고 하네스·도구·파일 쓰기는 실제로 실행한다."""

import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from local_agent_lab.experiments import aggregate
from local_agent_lab.features import DEFAULT_FEATURES
from local_agent_lab.harness import Harness
from local_agent_lab.trace import ROOT
from local_agent_lab.web import Application
from test_harness import FixtureRuntime, response, tool_call


class ByRequest(FixtureRuntime):
    """completions 요청을 받을 때마다 함수로 응답을 만든다."""

    def __init__(self, reply):
        super().__init__()
        self.reply = reply
        self.completions = []

    def request(self, route, payload=None):
        if route.endswith("completions"):
            self.completions.append(payload)
            return self.reply(payload, len(self.completions))
        return super().request(route, payload)


class RepeatedExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        shutil.copytree(ROOT / "demo_workspace", self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def app(self, runtime):
        return Application(Harness(self.workspace, self.root / "state", runtime=runtime))

    def wait(self, app, experiment_id):
        done = threading.Event()

        def wait_finished():
            while app.experiment_active:
                with app.lock:
                    runs = list(app.runs.values())
                if runs:
                    with runs[-1].condition:
                        runs[-1].condition.wait(timeout=0.02)
            done.set()

        threading.Thread(target=wait_finished, daemon=True).start()
        self.assertTrue(done.wait(timeout=20))
        return app.experiments.get(experiment_id)

    def test_repeats_alternate_order_vary_seed_and_isolate_sessions(self):
        order = []

        def reply(payload, count):
            order.append(("tools" if payload["tools"] else "none", payload["seed"]))
            return response({"content": "답"})

        app = self.app(ByRequest(reply))
        off = {key: False for key in DEFAULT_FEATURES}
        experiment_id = app.experiments.start(
            "요청",
            off,
            {**off, "tools": True},
            left_engine={"seed": 7},
            right_engine={"seed": 7},
            repeats=3,
        )
        item = self.wait(app, experiment_id)
        self.assertEqual(item["status"], "completed")
        self.assertEqual(
            order,
            [
                ("none", 7), ("tools", 7),
                ("tools", 8), ("none", 8),
                ("none", 9), ("tools", 9),
            ],
        )
        self.assertEqual([row["round"] for row in item["trials"]["A"]], [1, 2, 3])
        self.assertEqual(item["summary"]["A"]["runs"], 3)
        sessions = {run.session_id for run in app.runs.values()}
        self.assertEqual(len(sessions), 6)
        self.assertIn("seed를 1씩", item["note"])
        self.assertTrue(item["controls"]["same_workspace_all_trials"])

    def test_requirements_define_pass_rate_and_failures_are_attributed(self):
        def reply(payload, count):
            # 규칙이 들어간 쪽(B)은 seed가 홀수인 회차에서만 실제 도구를 부른다. A는 답만 반복한다.
            has_tool_result = any(m["role"] == "tool" for m in payload["messages"])
            with_rules = "project_rules" in (payload["messages"][0].get("content") or "")
            if with_rules and not has_tool_result and payload["seed"] % 2 == 1:
                return response({"tool_calls": [tool_call("add_numbers", a=1, b=2)]})
            return response({"content": "완료했습니다"})

        app = self.app(ByRequest(reply))
        experiment_id = app.experiments.start(
            "1과 2를 더해줘",
            {**DEFAULT_FEATURES, "rules": False},
            dict(DEFAULT_FEATURES),
            left_engine={"seed": 1},
            right_engine={"seed": 1},
            repeats=2,
            requirements=[{"tool": "add_numbers", "arguments": {"a": 1, "b": 2}}],
        )
        item = self.wait(app, experiment_id)
        self.assertEqual(item["status"], "completed", item.get("error"))
        self.assertEqual(item["summary"]["A"]["verified"], {"passed": 0, "total": 2})
        self.assertEqual(item["summary"]["B"]["verified"], {"passed": 1, "total": 2})
        # 도구 공개·입력 포함은 양쪽 모두 정상이고 필수 도구를 부르지 않은 회차만 실패한다.
        self.assertEqual(item["summary"]["A"]["failure_layers"], {"model": 2})
        self.assertEqual(item["summary"]["B"]["failure_layers"], {"model": 1})
        self.assertEqual(item["requirements"][0]["tool"], "add_numbers")

    def test_requirements_need_tools_on_both_sides(self):
        app = self.app(ByRequest(lambda payload, count: response({"content": "답"})))
        with self.assertRaises(ValueError):
            app.experiments.start(
                "요청",
                {**DEFAULT_FEATURES, "tools": False},
                dict(DEFAULT_FEATURES),
                requirements=[{"tool": "add_numbers", "arguments": {}}],
            )
        self.assertIsNone(app.experiment_active)

    def test_write_trials_start_from_the_same_workspace(self):
        target = self.workspace / "notes" / "created-by-trial.md"
        guide = self.workspace / "notes" / "agent-guide.md"
        before = guide.read_bytes()

        def reply(payload, count):
            if not any(m["role"] == "tool" for m in payload["messages"]):
                return response(
                    {"tool_calls": [tool_call("write_file", path="notes/created-by-trial.md", content="x")]}
                )
            return response({"content": "썼습니다"})

        app = self.app(ByRequest(reply))
        features = {**DEFAULT_FEATURES, "writes": True}
        experiment_id = app.experiments.start(
            "파일을 써줘", features, features, repeats=2
        )
        item = self.wait(app, experiment_id)
        self.assertEqual(item["status"], "completed")
        self.assertFalse(target.exists())
        self.assertEqual(guide.read_bytes(), before)
        self.assertTrue(item["controls"]["same_workspace_all_trials"])
        self.assertIn("되돌렸습니다", item["note"])

    def test_single_run_comparison_keeps_its_previous_shape(self):
        app = self.app(ByRequest(lambda payload, count: response({"content": "답"})))
        off = {key: False for key in DEFAULT_FEATURES}
        item = self.wait(app, app.experiments.start("요청", off, off, left_engine={"seed": 5}, right_engine={"seed": 5}))
        self.assertEqual(item["results"]["A"]["status"], "completed")
        self.assertEqual(item["results"]["A"]["conditions"]["seed"], 5)
        self.assertEqual(item["trials"]["A"][0]["seed"], 5)
        self.assertNotIn("seed를 1씩", item["note"])
        self.assertIsNone(item["summary"]["A"]["verified"])

    def test_invalid_repeat_values_are_rejected(self):
        app = self.app(ByRequest(lambda payload, count: response({"content": "답"})))
        for value in (0, 11, 1.5, "3", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.experiments.start("요청", repeats=value)
        with self.assertRaises(ValueError):
            app.experiments.start("요청", requirements=[{"tool": "x", "bad": 1}])
        self.assertIsNone(app.experiment_active)

    def test_aggregate_counts_only_trials_with_a_criterion(self):
        rows = [
            {"status": "completed", "verified": True, "focus": None, "model_calls": 2, "tool_calls": 1, "prompt_tokens": 10, "completion_tokens": 3, "active_seconds": 1.0},
            {"status": "failed", "verified": False, "focus": "model", "model_calls": 4, "tool_calls": 0, "prompt_tokens": None, "completion_tokens": 5, "active_seconds": 3.0},
            {"status": "failed", "verified": None, "focus": None, "model_calls": 0, "tool_calls": 0, "prompt_tokens": 30, "completion_tokens": 0, "active_seconds": 0.5},
        ]
        result = aggregate(rows)
        self.assertEqual(result["verified"], {"passed": 1, "total": 2})
        self.assertEqual(result["statuses"], {"completed": 1, "failed": 2})
        self.assertEqual(result["failure_layers"], {"model": 1, "unknown": 1})
        self.assertEqual(result["mean"]["model_calls"], 2.0)
        # 미수집(None)은 평균에서 뺀다. 0으로 세지 않는다.
        self.assertEqual(result["mean"]["prompt_tokens"], 20.0)


if __name__ == "__main__":
    unittest.main()
