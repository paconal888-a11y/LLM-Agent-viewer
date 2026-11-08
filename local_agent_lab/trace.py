"""실제 실행 지점의 데이터와 소스 위치를 보존하는 관찰 기록."""

from __future__ import annotations

import copy
import inspect
import json
import os
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


EXTERNAL_MARKERS = {".venv", "venv", "site-packages", "dist-packages", "__pypackages__"}


def external_source(path, code):
    """프로젝트 안에 설치된 서드파티 코드(.venv 등)는 본문을 기록하지 않는다.

    이전에는 .venv가 ROOT 아래라는 이유로 torch·transformers 함수 본문이 소스 스냅샷에 들어갔고,
    MIT로 배포하는 샘플 기록에 다른 라이선스의 코드가 섞였다. 위치만 남기고 본문은 비운다.
    """
    parts = path.parts
    marker = next((i for i, part in enumerate(parts) if part in EXTERNAL_MARKERS), None)
    shown = "/".join(parts[marker:]) if marker is not None else path.name
    return {
        "file": "<external>/" + shown,
        "function": code.co_name,
        "qualified_name": code.co_qualname,
        "start_line": code.co_firstlineno,
        "code": "",
        "external": True,
        "note": "프로젝트 밖(설치된 패키지)의 코드입니다. 라이선스와 크기 때문에 본문은 기록하지 않습니다.",
    }


@lru_cache(maxsize=128)
def code_source(code):
    path = Path(code.co_filename).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        return None
    if any(part in EXTERNAL_MARKERS for part in path.relative_to(ROOT).parts):
        return external_source(path, code)
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


def json_text(value, **options):
    """저장할 JSON 문자열. 모델 출력의 짝 없는 서로게이트는 UTF-8로 쓸 수 없어 이스케이프한다."""
    text = json.dumps(value, ensure_ascii=False, **options)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(value, ensure_ascii=True, **options)
    return text


def replace_file(source, target):
    """Windows는 다른 스레드가 대상 파일을 읽는 동안 교체를 거부한다. 잠깐 기다렸다가 다시 시도한다."""
    for attempt in range(25):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 24:
                raise
            time.sleep(0.02)


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
        # 요청 시점의 설정 원본. 모드 준비가 features·requirements를 바꾸고 continue가 step을 끄기 전의 값이라
        # 과거 실행을 그대로 다시 실행할 때 기준이 된다.
        self.request = {
            "step": bool(step),
            "features": dict(self.features),
            "requirements": [dict(item) for item in self.requirements],
            "mode": self.mode,
            "engine": engine_options(engine),
        }
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
        self.settings = {}
        self.checkpoint = None
        self.resumed_from = None
        self.turn_message_offset = 0
        self.implementation = None
        self.paused_seconds = 0.0
        self.pause_started = None
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.started = time.perf_counter()
        self.finished = None
        self.closed = False
        self.status = "running"
        self.step_mode = step
        self.answer = ""
        self.error = None
        self.events = []
        self.model_calls = 0
        self.tool_calls = 0
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        # 실행기가 usage를 보고한 호출 수와 보고하지 않은 호출 수. 미보고를 0으로 섞지 않기 위한 근거.
        self.usage_reports = {"reported": 0, "missing": 0}
        self.lock = threading.RLock()
        self.emit_lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.permits = 0
        self.stopped = False

    def add_usage(self, usage):
        """실행기가 보고한 usage만 더한다. 보고가 없으면 0을 더하지 않고 missing으로 센다."""
        if isinstance(usage, dict) and any(
            isinstance(usage.get(key), (int, float)) for key in self.usage
        ):
            for key in self.usage:
                self.usage[key] += int(usage.get(key, 0) or 0)
            self.usage_reports["reported"] += 1
        else:
            self.usage_reports["missing"] += 1

    def usage_totals(self):
        """하위 포함 합계. 한 호출이라도 usage를 보고하지 않았으면 None(미수집)으로 돌려준다."""
        runs = [self] + list(self.children)
        missing = sum(r.usage_reports["missing"] for r in runs)
        reported = sum(r.usage_reports["reported"] for r in runs)
        totals = {
            key: sum(r.usage[key] for r in runs) for key in self.usage
        }
        if reported == 0 and missing == 0:
            return {**totals, "usage_reported_calls": 0, "usage_missing_calls": 0}
        if missing:
            return {
                **{key: None for key in totals},
                "usage_reported_calls": reported,
                "usage_missing_calls": missing,
                "partial_usage": totals,
            }
        return {**totals, "usage_reported_calls": reported, "usage_missing_calls": 0}

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
        model_purpose=None,
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
                "model_purpose": model_purpose or self.model_purpose,
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
            # 부모가 단계 정지 중이면 이 호출이 멈춘다. 그 시간은 자식의 forward·active 시간에서도 빼야 하므로 자식에 더한다.
            parent_pause_before = self.parent_run.paused_seconds
            started_mirror = time.perf_counter()
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
                model_purpose=self.model_purpose,
            )
            self.parent_event_ids[event["id"]] = mirrored
            inherited = self.parent_run.paused_seconds - parent_pause_before
            if inherited > 0:
                with self.condition:
                    self.paused_seconds += min(
                        inherited, time.perf_counter() - started_mirror
                    )
        return event["id"]

    def record_interrupted(self, kind, title, detail, **kwargs):
        """중지 이후에도 남겨야 하는 기록. 정지하지 않고 RunStopped도 일으키지 않는다."""
        frame = inspect.currentframe().f_back
        source = source_location(frame)
        del frame
        with self.condition:
            event = {
                "id": len(self.events) + 1,
                "kind": kind,
                "title": title,
                "detail": detail,
                "actor": kwargs.get("actor", "harness"),
                "category": kwargs.get("category", "context"),
                "status": "warning",
                "parent": kwargs.get("parent"),
                "elapsed_ms": round((time.perf_counter() - self.started) * 1000, 2),
                "input": copy.deepcopy(kwargs.get("input")),
                "output": copy.deepcopy(kwargs.get("output")),
                "source": source,
                "agent_id": self.id,
                "agent_name": self.agent_name,
                "child_event_id": None,
                "child_elapsed_ms": None,
                "model_purpose": self.model_purpose,
            }
            self.events.append(event)
            self.condition.notify_all()
        return event["id"]

    def control(self, action):
        with self.condition:
            if action == "next":
                # 정지 중일 때만 한 단계 허가를 준다. running 상태에서 쌓인 허가는 다음 정지 지점을 조용히 건너뛰게 한다.
                if self.status == "paused" and self.step_mode:
                    self.permits = 1
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
                        **self.usage_totals(),
                    },
                    "request": self.request,
                    "integrations": self.integration_info,
                    "plan": self.plan,
                    "engine": self.engine,
                    "effective_prompt": self.effective_prompt,
                    "conditions": self.conditions,
                    "settings": self.settings,
                    "resumed_from": self.resumed_from,
                    "implementation": self.implementation,
                    "step_mode": self.step_mode,
                    "answer": self.answer,
                    "error": self.error,
                    "events": self.events[after:],
                    "event_count": len(self.events),
                    "model_calls": self.model_calls,
                    "tool_calls": self.tool_calls,
                    "usage": self.usage,
                    "usage_reports": self.usage_reports,
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
                    json_text(self.snapshot(compact=True), indent=2), encoding="utf-8"
                )
                replace_file(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
            return path
