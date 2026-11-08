"""같은 요청을 서로 다른 실제 하네스 구성으로 실행하고 관찰값을 비교한다."""

import json
import threading
import uuid

from .features import (
    BASELINE_FEATURES,
    DEFAULT_FEATURES,
    normalize_features,
    run_metrics,
)
from .inference import engine_options

CASES = {
    "agent_deep": {
        "name": "한 에이전트와 집중 점검",
        "prompt": "notes/agent-guide.md를 실제로 읽고 하네스의 역할과 주의할 점을 짧게 정리해줘.",
        "description": "동일 에이전트의 호출 예산과 추가 점검 지시만 바꿉니다. 모델 자체의 추론 강도는 바꾸지 않습니다.",
    },
    "agent_team": {
        "name": "한 에이전트와 하위 위임",
        "prompt": "notes/agent-guide.md를 실제로 읽고 하네스의 역할과 주의할 점을 짧게 정리해줘.",
        "description": "같은 로컬 모델에 독립 작업과 대화를 추가하고 하위 보고를 합칩니다. 전체 호출·토큰·도구·시간을 함께 비교합니다.",
    },
    "mcp_tools": {
        "name": "MCP 도구 연결 전후",
        "prompt": "mcp_math_add 도구를 사용할 수 있으면 그 도구로 137과 248을 더해줘. 없으면 사용할 수 없다고 알려줘.",
        "description": "기본 하네스는 같게 유지하고 MCP만 추가합니다. 모델에 공개한 schema와 실제 프로토콜 왕복을 비교합니다.",
    },
    "lsp_diagnostics": {
        "name": "LSP 진단 연결 전후",
        "prompt": "examples/lsp_demo.py를 확인해줘. lsp_diagnostics가 있으면 실제 언어 서버 진단을 받고, 없으면 read_file과 check_file로 확인해줘. 정의되지 않은 이름이 있으면 알려줘.",
        "description": "구문 검사와 실제 언어 서버 진단을 구분합니다. 모델이 스스로 문제를 찾아도 LSP 실행으로 세지 않습니다.",
    },
    "kv_cache": {
        "name": "동일 입력의 KV 접두부 재사용",
        "prompt": "다음 참고자료의 핵심을 한국어 한 문장, 40자 이내로 답하세요.\n\n참고자료:\n"
        + (
            "하네스는 지침과 대화를 메시지로 조립한다. 추론 실행기는 대화 템플릿을 적용하고 문자열을 토큰 ID로 변환한다. KV 캐시는 이전 토큰의 계산 상태를 보관하며 공통 접두부를 재사용하면 새로 처리할 입력이 줄어든다. 저장된 대화 이력과 KV 캐시는 별개의 상태다.\n"
            * 8
        ),
        "description": "메시지는 동일하게 유지하고 A는 요청 간 재사용을 끄고 B는 켭니다. 생성 중 KV 캐시를 없애는 실험이 아닙니다.",
    },
    "grounding": {
        "name": "파일 근거로 답하기",
        "prompt": "notes/release-brief.md를 읽고 프로젝트 코드와 예정 출시일을 정확히 알려줘. 출처 파일 경로도 표시해줘.",
        "description": "모델이 파일에서만 알 수 있는 값을 실제로 확인하는지 비교합니다.",
    },
    "arithmetic": {
        "name": "계산 도구 사용",
        "prompt": "add_numbers 도구를 사용해 137과 248을 더하고 결과를 알려줘.",
        "description": "정답과 실제 도구 사용을 별도 기준으로 확인합니다.",
    },
}


def evaluate(snapshot, case_id):
    if case_id in {"agent_deep", "agent_team"}:
        return {
            "available": False,
            "criteria": [],
            "note": "모드의 실행 구조와 비용을 비교합니다. 더 많은 에이전트나 점검이 더 나은 답변을 보장하지 않습니다.",
        }
    case = CASES.get(case_id)
    if (
        case_id in {"mcp_tools", "lsp_diagnostics"}
        and case
        and snapshot["prompt"] == case["prompt"]
    ):
        results = [
            e.get("output") or {}
            for e in snapshot["events"]
            if e["kind"] == "tool.result"
        ]
        if case_id == "mcp_tools":
            criteria = [
                {
                    "label": "MCP 서버에서 실제 합계 385 반환",
                    "passed": any(
                        r.get("protocol") == "mcp"
                        and (r.get("data") or {}).get("result") == 385
                        for r in results
                    ),
                },
                {
                    "label": "답변에 385 포함",
                    "passed": "385" in snapshot.get("answer", ""),
                },
            ]
        else:
            criteria = [
                {
                    "label": "실제 LSP 진단 수신",
                    "passed": any(
                        r.get("protocol") == "lsp"
                        and any(
                            "missing_amount" in d.get("message", "")
                            for d in r.get("diagnostics", [])
                        )
                        for r in results
                    ),
                },
                {
                    "label": "답변에 missing_amount 포함",
                    "passed": "missing_amount" in snapshot.get("answer", ""),
                },
            ]
        return {
            "available": True,
            "criteria": criteria,
            "passed": sum(c["passed"] for c in criteria),
            "total": len(criteria),
            "note": "도구 실행 증거와 답변 내용의 제한된 예제 기준입니다.",
        }
    if case_id == "kv_cache":
        return {
            "available": False,
            "criteria": [],
            "note": "KV 접두부 재사용의 계산량·시간을 측정합니다. 답변 품질 점수는 매기지 않습니다.",
        }
    if not case or snapshot["prompt"] != case["prompt"]:
        return {
            "available": False,
            "criteria": [],
            "note": "사용자 지정 요청에는 자동 정답 기준을 적용하지 않습니다.",
        }
    answer = snapshot.get("answer", "")
    if case_id == "grounding":
        actual_read = any(
            event["kind"] == "tool.result"
            and event.get("output", {}).get("path") == "notes/release-brief.md"
            and "content" in event.get("output", {})
            for event in snapshot["events"]
        )
        criteria = [
            {"label": "프로젝트 코드 NARI-042 포함", "passed": "NARI-042" in answer},
            {"label": "예정일 2026-12-12 포함", "passed": "2026-12-12" in answer},
            {
                "label": "실제로 읽은 출처 경로 제시",
                "passed": actual_read and "notes/release-brief.md" in answer,
            },
        ]
    else:
        criteria = [
            {"label": "정답 385 포함", "passed": "385" in answer},
            {
                "label": "실제 add_numbers 연산 완료",
                "passed": any(
                    event["kind"] == "tool.result"
                    and event.get("output", {}).get("result") == 385
                    and event.get("output", {}).get("a") == 137
                    and event.get("output", {}).get("b") == 248
                    for event in snapshot["events"]
                ),
            },
        ]
    return {
        "available": True,
        "criteria": criteria,
        "passed": sum(item["passed"] for item in criteria),
        "total": len(criteria),
        "note": "이 예제의 문자열·도구 호출 기준입니다. 일반적인 모델 품질 점수가 아닙니다.",
    }


TRANSPORT_KEYS = {"cache_prompt", "stream", "use_kv", "capture_layer"}


def sampling_only(config):
    return {k: v for k, v in (config or {}).items() if k not in TRANSPORT_KEYS}


def summarize(run, case_id):
    snapshot = run.snapshot()
    counts = {}
    for event in snapshot["events"]:
        counts[event["category"]] = counts.get(event["category"], 0) + 1
    return {
        "run_id": run.id,
        "status": run.status,
        "answer": run.answer,
        "error": run.error,
        "features": snapshot["features"],
        "engine": snapshot.get("engine", {}),
        "mode": snapshot.get("mode", "single"),
        "effective_prompt": snapshot.get("effective_prompt"),
        "implementation": (snapshot.get("implementation") or {}).get("version"),
        "created_at": snapshot.get("created_at"),
        "elapsed_seconds": snapshot.get("elapsed_seconds"),
        "agents": snapshot.get("agents", []),
        "conditions": snapshot["conditions"],
        "metrics": run_metrics(snapshot),
        "evaluation": evaluate(snapshot, case_id),
        "event_categories": counts,
    }


class ExperimentService:
    def __init__(self, app):
        self.app = app
        self.items = {}
        self.directory = app.harness.state_dir / "experiments"

    def start(
        self,
        prompt,
        left=None,
        right=None,
        case_id=None,
        left_engine=None,
        right_engine=None,
        left_mode="single",
        right_mode="single",
    ):
        from .orchestration import normalize_mode

        if case_id in {"agent_deep", "agent_team"}:
            left_mode, right_mode = (
                "single",
                "deep" if case_id == "agent_deep" else "team",
            )
            if left is None:
                left = DEFAULT_FEATURES
            if right is None:
                right = DEFAULT_FEATURES
        modes = {"A": normalize_mode(left_mode), "B": normalize_mode(right_mode)}
        if self.app.samples_only:
            raise ValueError("샘플 보기 모드에서는 비교 실험을 시작하지 않습니다.")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 6000:
            raise ValueError("비교할 요청을 1~6,000자로 입력하세요.")
        features_a = normalize_features(BASELINE_FEATURES if left is None else left)
        features_b = normalize_features(
            (BASELINE_FEATURES if case_id == "kv_cache" else DEFAULT_FEATURES)
            if right is None
            else right
        )
        if case_id in {"mcp_tools", "lsp_diagnostics"}:
            flag = "mcp" if case_id == "mcp_tools" else "lsp"
            if left is None:
                features_a = normalize_features({**DEFAULT_FEATURES, flag: False})
            if right is None:
                features_b = normalize_features({**DEFAULT_FEATURES, flag: True})
        engine_a = engine_options(
            {"cache_prompt": False}
            if case_id == "kv_cache" and left_engine is None
            else left_engine
        )
        engine_b = engine_options(right_engine)
        with self.app.lock:
            active = any(
                run.status in {"running", "paused", "stopping"}
                for run in self.app.runs.values()
            )
            if active or self.app.experiment_active:
                raise ValueError("현재 실행 또는 비교가 끝난 뒤 시작하세요.")
            experiment_id = uuid.uuid4().hex[:16]
            item = {
                "id": experiment_id,
                "status": "running",
                "phase": "A",
                "prompt": prompt.strip(),
                "case_id": case_id,
                "profiles": {"A": features_a, "B": features_b},
                "engines": {"A": engine_a, "B": engine_b},
                "modes": modes,
                "results": {},
            }
            self.items[experiment_id] = item
            self.app.experiment_active = experiment_id
        threading.Thread(target=self.execute, args=(item,), daemon=True).start()
        return experiment_id

    def execute(self, item):
        try:
            for label in ("A", "B"):
                if item.get("stop_requested"):
                    break
                item["phase"] = label
                run = self.app.start(
                    item["prompt"],
                    f"experiment-{item['id']}-{label}",
                    False,
                    features=item["profiles"][label],
                    internal=True,
                    engine=item["engines"][label],
                    mode=item["modes"][label],
                )
                item["current_run"] = run.id
                with run.condition:
                    run.condition.wait_for(
                        lambda: run.status in {"completed", "failed", "stopped"},
                        timeout=1800,
                    )
                if run.status not in {"completed", "failed", "stopped"}:
                    run.control("stop")
                    raise ValueError("비교 실행의 제한 시간을 초과했습니다.")
                item["results"][label] = summarize(run, item["case_id"])
            if item.get("stop_requested"):
                item["status"] = "stopped"
                item["phase"] = "stopped"
                return
            a = item["results"]["A"]["conditions"]
            b = item["results"]["B"]["conditions"]
            if not a or not b:
                # 한쪽이 루프 진입 전에 실패하면 조건이 비어 있다. 빈 값끼리 비교해 '같다'고 표시하지 않는다.
                item["controls"] = None
                item["status"] = "completed"
                item["phase"] = "done"
                item["note"] = "한쪽 실행이 조건을 기록하기 전에 실패해 통제 변수를 비교하지 않았습니다."
                return
            item["controls"] = {
                "same_prompt": item["results"]["A"].get("effective_prompt")
                == item["results"]["B"].get("effective_prompt"),
                "same_harness_code": item["results"]["A"].get("implementation")
                == item["results"]["B"].get("implementation"),
                "same_model": a.get("model") == b.get("model"),
                "same_generation": bool(a.get("effective_generation_config"))
                and a.get("effective_generation_config")
                == b.get("effective_generation_config"),
                # 샘플링(temperature·seed·top_k·top_p·max_tokens 등)만 비교. 캐시·전송·관찰 설정(cache_prompt,
                # stream, use_kv, capture_layer)은 모델 출력 분포가 아니라 계산 경로를 바꾸므로 따로 본다.
                "same_sampling": bool(a.get("effective_generation_config"))
                and sampling_only(a.get("effective_generation_config"))
                == sampling_only(b.get("effective_generation_config")),
                "same_workspace": a.get("workspace", {}).get("sha256")
                == b.get("workspace", {}).get("sha256"),
                "isolated_sessions": True,
            }
            item["generation_diff"] = {
                key: {
                    "A": a.get("effective_generation_config", {}).get(key),
                    "B": b.get("effective_generation_config", {}).get(key),
                }
                for key in set(a.get("effective_generation_config", {}))
                | set(b.get("effective_generation_config", {}))
                if a.get("effective_generation_config", {}).get(key)
                != b.get("effective_generation_config", {}).get(key)
                or key not in a.get("effective_generation_config", {})
                or key not in b.get("effective_generation_config", {})
            }
            if item["case_id"] in {"kv_cache", "decode_kv"}:
                item["controls"]["same_model_input"] = bool(
                    a.get("model_input_sha256")
                ) and a.get("model_input_sha256") == b.get("model_input_sha256")
            item["status"] = "completed"
            item["phase"] = "done"
            item["note"] = (
                "A 다음 B를 한 번씩 측정했습니다. 캐시·실행 순서·관찰 기록의 비용이 시간에 영향을 줄 수 있습니다. cache_prompt는 요청 간 접두부 재사용 옵션이며 생성 중 KV 캐시 전체를 끄는 옵션은 아닙니다."
            )
            if item["case_id"] == "decode_kv":
                item["note"] = (
                    "A는 생성 중 KV 없이 전체 접두부를 재계산하고 B는 KV를 사용합니다. 요청 간 접두부 재사용은 둘 다 껐습니다. A→B를 한 번씩 측정한 값이며 관찰 기록 비용도 포함됩니다."
                )
        except Exception as exc:
            item["status"] = "failed"
            item["error"] = str(exc)
        finally:
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / (item["id"] + ".json")).write_text(
                json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            with self.app.lock:
                self.app.experiment_active = None

    def get(self, experiment_id):
        if not experiment_id.isalnum() or len(experiment_id) != 16:
            raise ValueError("비교 ID가 올바르지 않습니다.")
        if experiment_id in self.items:
            item = json.loads(json.dumps(self.items[experiment_id]))
            if (
                item.get("status") == "running"
                and item.get("current_run") in self.app.runs
            ):
                run = self.app.runs[item["current_run"]]
                item["progress"] = {
                    "events": len(run.events),
                    "model_calls": run.model_calls,
                    "tool_calls": run.tool_calls,
                }
            return item
        path = self.directory / (experiment_id + ".json")
        if not path.exists():
            raise ValueError("비교 기록을 찾을 수 없습니다.")
        return json.loads(path.read_text(encoding="utf-8"))

    def stop(self, experiment_id):
        item = self.items.get(experiment_id)
        if not item or item["status"] != "running":
            raise ValueError("중지할 비교 실행이 없습니다.")
        item["stop_requested"] = True
        current = self.app.runs.get(item.get("current_run"))
        if current and current.status in {"running", "paused"}:
            current.control("stop")
        return {"stop_requested": True}
