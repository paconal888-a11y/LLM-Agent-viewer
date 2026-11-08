"""중단된 실행을 이어가기 위한 상태 저장. 모델 내부 상태가 아니라 하네스가 가진 대화와 진행 상태만 저장한다."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .trace import replace_file

FORMAT = "agent-lens-checkpoint/1"
RESUMABLE_STATUSES = {"stopped", "failed", "interrupted"}
KEEP_FILES = 20


def checkpoint_path(state_dir, run_id):
    if not run_id.isalnum() or len(run_id) != 16:
        raise ValueError("실행 ID가 올바르지 않습니다.")
    return Path(state_dir) / "checkpoints" / (run_id + ".json")


def save_checkpoint(state_dir, run, turn):
    """같은 폴더의 임시 파일에 쓰고 교체해, 중간에 죽어도 이전 저장본이 남게 한다."""
    path = checkpoint_path(state_dir, run.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "format": FORMAT,
        "run_id": run.id,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "session_id": run.session_id,
        "prompt": run.prompt,
        "effective_prompt": run.effective_prompt,
        "request": run.request,
        "requirements": run.requirements,
        "conditions": run.conditions,
        "settings": run.settings,
        "implementation": (run.implementation or {}).get("version"),
        "max_model_calls": run.max_model_calls,
        "model_calls": run.model_calls,
        "tool_calls": run.tool_calls,
        "usage": run.usage,
        "usage_reports": run.usage_reports,
        "plan": run.plan,
        "delegation_used": getattr(run, "delegation_used", False),
        "context_segments": run.context_segments,
        "turn_message_offset": run.turn_message_offset,
        **turn,
    }
    temporary = path.with_suffix("." + uuid.uuid4().hex[:8] + ".tmp")
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        replace_file(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path, path.stat().st_size


def load_checkpoint(state_dir, run_id):
    path = checkpoint_path(state_dir, run_id)
    if not path.is_file():
        raise ValueError("이어서 실행할 저장 상태가 없습니다.")
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("format") != FORMAT:
        raise ValueError("지원하지 않는 저장 상태 형식입니다.")
    return record


def discard_checkpoint(state_dir, run_id):
    try:
        checkpoint_path(state_dir, run_id).unlink()
    except FileNotFoundError:
        pass


def resume_summary(state_dir, run_id, status):
    """화면에 보여 줄 재개 가능 여부. 파일이 없거나 끝난 실행이면 None."""
    if status not in RESUMABLE_STATUSES:
        return None
    try:
        record = load_checkpoint(state_dir, run_id)
    except (ValueError, OSError, json.JSONDecodeError):
        return None
    return {
        "saved_at": record["saved_at"],
        "next_call": record["next_iteration"],
        "last_call": record["last_iteration"],
        "budget_left": max(0, record["last_iteration"] - record["next_iteration"] + 1),
        "messages": len(record["messages"]),
    }


def trim_checkpoints(state_dir, keep=KEEP_FILES):
    folder = Path(state_dir) / "checkpoints"
    files = sorted(folder.glob("*.json"), key=lambda item: item.stat().st_mtime)
    for item in files[:-keep]:
        item.unlink(missing_ok=True)
