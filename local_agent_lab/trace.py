"""실제 실행 지점의 데이터와 소스 위치를 보존하는 관찰 기록."""

from __future__ import annotations

import copy
import inspect
import json
import threading
import time
import uuid
import hashlib
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path

from .features import normalize_features, normalize_requirements
from .inference import engine_options
from .orchestration import normalize_mode

ROOT = Path(__file__).resolve().parents[1]


class RunStopped(Exception):
    pass


@lru_cache(maxsize=128)
def code_source(code):
    path = Path(code.co_filename).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        return None
    lines, start = inspect.getsourcelines(code)
    return {
        "file": str(path.relative_to(ROOT)),
        "function": code.co_name,
        "qualified_name": code.co_qualname,
        "start_line": start,
        "code": "".join(lines),
    }


def frame_source(frame):
    """현재 프레임의 코드와 위치를 함께 보존한다. 외부 라이브러리는 제외한다."""
    try:
        source = code_source(frame.f_code)
        return {**source, "line": frame.f_lineno} if source else None
    except (OSError, TypeError, ValueError):
        return None


def source_location(frame):
    """이벤트 발생 지점과 그 순간 실제 Python 호출 스택의 스냅샷."""
    source = frame_source(frame)
    if source is None:
        return None
    stack = []
    caller = frame
    try:
        while caller is not None and len(stack) < 8:
            item = frame_source(caller)
            if item is not None:
                stack.append(item)
            caller = caller.f_back
    finally:
        del caller
    source["stack"] = list(reversed(stack))
    return source


def pack_sources(snapshot):
    """같은 소스는 한 번 저장한다. 코드 내용과 각 프레임의 줄 위치는 보존한다."""
    sources = {}

    def pack(source):
        if not source:
            return source
        definition = {
            key: source[key]
            for key in ("file", "function", "qualified_name", "start_line", "code")
            if key in source
        }
        key = hashlib.sha256(
            json.dumps(definition, sort_keys=True).encode()
        ).hexdigest()[:20]
        sources[key] = definition
        return {
            **{
                key: value
                for key, value in source.items()
                if key not in {"code", "stack"}
            },
            "source_ref": key,
            **(
                {"stack": [pack(frame) for frame in source["stack"]]}
                if "stack" in source
                else {}
            ),
        }

    for event in snapshot["events"]:
        event["source"] = pack(event.get("source"))
    snapshot["source_snapshots"] = sources
    snapshot["trace_format"] = 2
    return snapshot


def expand_sources(snapshot):
    sources = snapshot.get("source_snapshots", {})

    def expand(source):
        if not source:
            return source
        return {
            **sources.get(source.get("source_ref"), {}),
            **source,
            **(
                {"stack": [expand(frame) for frame in source["stack"]]}
                if "stack" in source
                else {}
            ),
        }

    for event in snapshot["events"]:
        event["source"] = expand(event.get("source"))
    return snapshot


class Run:
    def __init__(
        self,
        prompt,
        session_id,
        step=False,
        features=None,
        engine=None,
        requirements=None,
        mode="single",
        parent_run=None,
        agent_name="주 에이전트",
    ):
        self.id = uuid.uuid4().hex[:16]
        self.session_id = session_id
        self.prompt = prompt
        self.effective_prompt = prompt
        self.features = normalize_features(features)
        self.requirements = normalize_requirements(requirements)
        self.mode = normalize_mode(mode)
        self.parent_run = parent_run
        self.agent_name = agent_name
        self.children = []
        self.parent_event_ids = {}
        self.allowed_tool_names = None
        self.role_instructions = ""
        self.max_model_calls = 8
        self.review_instruction = ""
        self.model_purpose = "answer"
        if self.requirements and not self.features["tools"]:
            raise ValueError("도구 완료 조건을 사용하려면 도구를 연결하세요.")
        self.engine = engine_options(engine)
        self.token_previews = []
        self.context_segments = []
        self.extra_tools = []
        self.tool_handlers = {}
        self.integration_info = []
        self.plan = []
        self.conditions = {}
        self.implementation = None
        self.paused_seconds = 0.0
        self.pause_started = None
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.started = time.perf_counter()
        self.finished = None
        self.status = "running"
        self.step_mode = step
        self.answer = ""
        self.error = None
        self.events = []
        self.model_calls = 0
        self.tool_calls = 0
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.lock = threading.RLock()
        self.emit_lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.permits = 0
        self.stopped = False

    def emit(self, *args, **kwargs):
        frame = inspect.currentframe().f_back
        kwargs.setdefault("source_override", source_location(frame))
        del frame
        # 단계 정지 시에도 하나의 전역 관찰 순서를 유지한다.
        with self.emit_lock:
            return self._emit(*args, **kwargs)

    def _emit(
        self,
        kind,
        title,
        detail,
        *,
        actor="harness",
        category="context",
        input=None,
        output=None,
        parent=None,
        status="ok",
        pause=True,
        source_override=None,
        agent_id=None,
        agent_name=None,
        child_event_id=None,
        child_elapsed_ms=None,
    ):
        frame = inspect.currentframe().f_back
        source = source_override or source_location(frame)
        del frame
        with self.condition:
            if self.stopped:
                raise RunStopped()
            event = {
                "id": len(self.events) + 1,
                "kind": kind,
                "title": title,
                "detail": detail,
                "actor": actor,
                "category": category,
                "status": status,
                "parent": parent,
                "elapsed_ms": round((time.perf_counter() - self.started) * 1000, 2),
                "input": copy.deepcopy(input),
                "output": copy.deepcopy(output),
                "source": source,
                "agent_id": agent_id or self.id,
                "agent_name": agent_name or self.agent_name,
                "child_event_id": child_event_id,
                "child_elapsed_ms": child_elapsed_ms,
                "model_purpose": self.model_purpose,
            }
            self.events.append(event)
            self.condition.notify_all()
            if self.step_mode and pause:
                self.status = "paused"
                self.pause_started = time.perf_counter()
                try:
                    while self.step_mode and self.permits <= 0 and not self.stopped:
                        self.condition.wait(timeout=1)
                finally:
                    self.paused_seconds += time.perf_counter() - self.pause_started
                    self.pause_started = None
                if self.stopped:
                    raise RunStopped()
                self.permits = max(0, self.permits - 1)
                self.status = "running"
        if self.parent_run:
            # 자식 lock을 해제한 뒤 부모에 전달하여 stop과 교착하지 않는다.
            mirrored = self.parent_run.emit(
                kind,
                self.agent_name + " · " + title,
                detail,
                actor=actor,
                category=category,
                input=input,
                output=output,
                parent=self.parent_event_ids.get(parent),
                status=status,
                pause=pause,
                source_override=source,
                agent_id=self.id,
                agent_name=self.agent_name,
                child_event_id=event["id"],
                child_elapsed_ms=event["elapsed_ms"],
            )
            self.parent_event_ids[event["id"]] = mirrored
        return event["id"]

    def control(self, action):
        with self.condition:
            if action == "next":
                self.permits += 1
            elif action == "continue":
                self.step_mode = False
            elif action == "stop":
                self.stopped = True
                self.status = "stopping"
            else:
                raise ValueError("알 수 없는 실행 제어입니다.")
            self.condition.notify_all()
        if action == "stop":
            for child in list(self.children):
                if child.status in {"running", "paused", "stopping"}:
                    child.control("stop")

    def snapshot(self, after=0, compact=False):
        with self.lock:
            elapsed = (self.finished or time.perf_counter()) - self.started
            paused = self.paused_seconds + (
                time.perf_counter() - self.pause_started if self.pause_started else 0
            )
            snapshot = copy.deepcopy(
                {
                    "id": self.id,
                    "session_id": self.session_id,
                    "prompt": self.prompt,
                    "created_at": self.created_at,
                    "status": self.status,
                    "features": self.features,
                    "requirements": self.requirements,
                    "mode": self.mode,
                    "agent_name": self.agent_name,
                    "parent_run_id": self.parent_run.id if self.parent_run else None,
                    "max_model_calls": self.max_model_calls,
                    "agents": [
                        {
                            "id": child.id,
                            "name": child.agent_name,
                            "parent_id": self.id,
                            "status": child.status,
                            "model_calls": child.model_calls,
                            "tool_calls": child.tool_calls,
                            "usage": dict(child.usage),
                        }
                        for child in self.children
                    ],
                    "totals": {
                        "model_calls": self.model_calls
                        + sum(c.model_calls for c in self.children),
                        "tool_calls": self.tool_calls
                        + sum(c.tool_calls for c in self.children),
                        "prompt_tokens": self.usage["prompt_tokens"]
                        + sum(c.usage["prompt_tokens"] for c in self.children),
                        "completion_tokens": self.usage["completion_tokens"]
                        + sum(c.usage["completion_tokens"] for c in self.children),
                    },
                    "integrations": self.integration_info,
                    "plan": self.plan,
                    "engine": self.engine,
                    "effective_prompt": self.effective_prompt,
                    "conditions": self.conditions,
                    "implementation": self.implementation,
                    "step_mode": self.step_mode,
                    "answer": self.answer,
                    "error": self.error,
                    "events": self.events[after:],
                    "event_count": len(self.events),
                    "model_calls": self.model_calls,
                    "tool_calls": self.tool_calls,
                    "usage": self.usage,
                    "elapsed_seconds": round(elapsed, 3),
                    "paused_seconds": round(paused, 3),
                    "active_seconds": round(max(0, elapsed - paused), 3),
                }
            )
            return pack_sources(snapshot) if compact else snapshot

    def save(self, directory):
        with self.lock:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (self.id + ".json")
            temporary = path.with_suffix("." + uuid.uuid4().hex[:8] + ".tmp")
            try:
                temporary.write_text(
                    json.dumps(
                        self.snapshot(compact=True), ensure_ascii=False, indent=2
                    ),
                    encoding="utf-8",
                )
                temporary.replace(path)
            finally:
                if temporary.exists():
                    temporary.unlink()
            return path
