"""하네스 진입점. 실제 루프·도구·입력 구성·검증은 각 모듈에 있다."""

import json
import re
import threading
from .trace import ROOT, Run, RunStopped
from .runtime import LocalRuntime
from .files import workspace_path, read_traced
from .context import (
    build_context,
    discover_rules,
    discover_skills,
    tools_for,
    describe_context,
    expand_command,
)
from .toolkit import TOOLS, IMPLEMENTATIONS, ToolContext, function_tool
from .policy import validate_tool_call
from .feedback import collect_feedback
from .context_memory import (
    append_archive,
    message_origins,
    write_state,
    bounded_state,
    STATE_LIMIT,
    archive_path,
    legacy_records,
)


def execute_tool(run, workspace, catalog, call, active_skills):
    workspace = workspace.resolve()
    parent = run.emit(
        "tool.dispatch",
        "모델의 도구 요청 수신",
        "이 요청은 아직 실행 결과가 아닙니다. 인자·경로 검사를 거쳐 구현 함수로 연결합니다.",
        category="tools",
        input=call,
    )
    try:
        name, args = validate_tool_call(run, workspace, tools_for(run), call, parent)
        implementation = run.tool_handlers.get(name) or IMPLEMENTATIONS.get(name)
        if not callable(implementation):
            raise ValueError("도구 구현이 등록되지 않았습니다: " + str(name))
        context = ToolContext(run, workspace, catalog, active_skills, parent, name)
        try:
            result = implementation(context, **args)
        except RunStopped:
            # 중지 요청이 도구 실행 도중 도착하면 부수효과는 이미 끝났을 수 있다. 결과 이벤트가
            # 없어 기록에서 사라지지 않도록 중단 사실을 남긴 뒤 중지를 계속 전파한다.
            run.record_interrupted(
                "tool.interrupted",
                name + " 실행 중 중지",
                "사용자 중지가 도구 실행 도중 도착했습니다. 도구 함수가 이미 만든 파일·외부 효과는 되돌리지 않았습니다.",
                category="tools",
                actor="tool",
                parent=parent,
                input={"name": name, "arguments": args},
            )
            raise
    except (OSError, ValueError, TypeError) as error:
        result = {"error": str(error), "executed_successfully": False}
        run.emit(
            "tool.error",
            "도구 요청 실패",
            "실패 내용도 모델에게 전달합니다.",
            category="tools",
            actor="validator",
            parent=parent,
            status="error",
            output=result,
        )
    return {
        "role": "tool",
        "tool_call_id": call["id"],
        "content": json.dumps(result, ensure_ascii=False),
    }


class Harness:
    def __init__(
        self,
        workspace=None,
        state_dir=None,
        base_url="http://127.0.0.1:8088",
        model="qwen3-4b",
        runtime=None,
    ):
        self.workspace = (workspace or ROOT / "demo_workspace").resolve()
        self.state_dir = state_dir or ROOT / ".runs"
        self.runtime = runtime or LocalRuntime(base_url, timeout=180)
        self.model = model
        self.inference_lock = threading.Lock()
        self.history_lock = threading.Lock()

    def history_path(self, session_id):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", session_id):
            raise ValueError("세션 ID 형식이 올바르지 않습니다.")
        return self.state_dir / "sessions" / (session_id + ".json")

    def load_history(self, run):
        if not run.features["memory"]:
            run.emit(
                "memory.disabled",
                "이전 맥락 불러오기 건너뛰기",
                "memory=false이므로 저장된 세션과 resume.md를 모델 입력에 포함하지 않습니다. 이번 실행 기록은 보존합니다.",
                category="memory",
                output={"enabled": False},
            )
            return {}
        path = self.history_path(run.session_id)
        if not path.exists():
            run.emit(
                "memory.session",
                "새 세션 시작",
                "이 세션 ID로 저장된 대화가 없어 빈 이력으로 시작합니다.",
                category="memory",
                output={"session_id": run.session_id, "history_exists": False},
            )
            return {}
        raw = read_traced(
            run,
            path,
            "memory",
            reason="현재 세션의 이전 완료 대화와 응답 발췌를 불러옵니다.",
            max_bytes=8 * 1024 * 1024,
        )
        history = json.loads(raw)
        budget = history.get("state_budget") or {}
        if budget.get("omitted_recent_turns"):
            run.emit(
                "memory.state.omitted",
                "활성 세션에서 제외된 큰 대화",
                "이전 저장 때 활성 상태 예산(512 KiB)을 넘어 최근 대화 일부가 활성 상태에서 빠졌습니다. 원문은 대화 원본 저장소에 있으며 이번 모델 입력에는 들어가지 않습니다.",
                category="memory",
                status="warning",
                output={**budget, "archive_turn_count": history.get("archive_turn_count")},
            )
        if len(raw.encode("utf-8")) > STATE_LIMIT:
            archive = archive_path(self, run.session_id)
            if not archive.exists():
                records = legacy_records(history)
                archive.parent.mkdir(parents=True, exist_ok=True)
                archive.write_text(
                    "".join(
                        json.dumps(record, ensure_ascii=False) + "\n"
                        for record in records
                    ),
                    encoding="utf-8",
                    newline="",
                )
                history["archive_turn_count"] = len(records)
                run.emit(
                    "memory.archive.bootstrap",
                    "큰 이전 세션의 원문 보존",
                    "활성 상태를 줄이기 전에 기존 최근 대화 원문을 JSONL에 보존했습니다.",
                    category="memory",
                    output={"path": str(archive), "recovered_turns": len(records)},
                    pause=False,
                )
            history = bounded_state(history)
            run.emit(
                "memory.state.recover",
                "큰 활성 세션을 읽기 예산에 맞게 복원",
                "대화 전체를 모델 입력에 넣지 않습니다. 큰 최근 대화는 활성 상태에서 제외하며 원본 archive는 보존합니다.",
                category="memory",
                output=history.get("state_budget", {}),
            )
        return history

    def save_history(self, run, previous, turn):
        if run.parent_run:
            run.emit(
                "memory.child",
                "하위 대화는 실행 기록으로 보존",
                "하위 작업을 주 세션의 일반 대화 이력에 합치지 않습니다. 요약 결과만 주 에이전트의 tool 메시지로 돌아갑니다.",
                category="memory",
                output={"history_saved": False, "trace_saved": True},
                pause=False,
            )
            return
        path = self.history_path(run.session_id)
        if not previous and path.exists():
            raw = read_traced(
                run,
                path,
                "memory",
                reason="저장 이력을 이어붙이기 위한 읽기입니다. 이 내용은 이번 모델 입력에는 넣지 않습니다.",
                max_bytes=8 * 1024 * 1024,
            )
            previous = json.loads(raw)
        archive_count = append_archive(self, run, previous, turn)
        turns = (previous.get("turns", []) + [turn])[-2:]
        previous_origins = previous.get("turn_origins") or []
        previous_origins = [None] * (
            len(previous.get("turns", [])) - len(previous_origins)
        ) + previous_origins
        data = {
            "turns": turns,
            "turn_origins": (previous_origins + [message_origins(run, turn)])[-2:],
            "archive_turn_count": archive_count,
            "summary": previous.get("summary", ""),
            "summary_through": previous.get("summary_through", 0),
            "summary_source_run_ids": previous.get("summary_source_run_ids"),
            "summary_path": previous.get("summary_path"),
            "excerpt": run.answer[:1000],
            "excerpt_method": "first_1000_characters_of_last_answer",
            "turn_ids": (previous.get("turn_ids", []) + [run.id])[-2:],
            "feedback_ledger": (
                previous.get("feedback_ledger", []) + collect_feedback(run)
            )[-20:],
        }
        data = bounded_state(data)
        write_state(path, data)
        run.emit(
            "memory.save",
            "세션 이력과 직전 답변 발췌 저장",
            "다음 요청은 이 JSON을 읽어 최근 대화를 복원합니다. 모델 가중치를 학습하거나 수정하지 않습니다.",
            category="memory",
            output={
                "path": str(path),
                "saved_turns": len(data["turns"]),
                "state_budget": data.get("state_budget"),
                "excerpt": data["excerpt"],
                "dropped_turn_ids": previous.get("turn_ids", [])[:-1]
                if len(previous.get("turn_ids", [])) >= 2
                else [],
                "stored_feedback": len(data["feedback_ledger"]),
            },
            pause=False,
        )

    def dispatch_tool(self, run, catalog, call, active_skills):
        return execute_tool(run, self.workspace, catalog, call, active_skills)

    def execute(self, run):
        from .agent_loop import execute

        execute(self, run)
