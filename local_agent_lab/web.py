"""Agent Lens의 로컬 HTTP 화면과 관찰 API. 외부 프레임워크 불필요."""

from __future__ import annotations

import argparse
import gzip
import json
import mimetypes
import os
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import app_version
from .harness import Harness, workspace_path
from .runtime import RuntimeFailure
from .trace import ROOT, Run, expand_sources, pack_sources
from .experiments import ExperimentService, CASES
from .observatory import catalog
from .project import ProjectFiles, HarnessFactory, check_text


def browser_source(marker, function):
    lines = (ROOT / "web" / "workbench.js").read_text(encoding="utf-8").splitlines()
    start = (
        next(
            i
            for i, line in enumerate(lines)
            if line.strip() == "// " + marker + "_START"
        )
        + 1
    )
    end = next(
        i for i, line in enumerate(lines) if line.strip() == "// " + marker + "_END"
    )
    return {
        "file": "web/workbench.js",
        "function": function,
        "qualified_name": function,
        "start_line": start + 1,
        "line": start + 1,
        "code": "\n".join(lines[start:end]),
    }


class WorkInProgress(ValueError):
    """다른 탭·새로고침 전의 실행이 계속되고 있다."""


class Application:
    def __init__(self, harness=None, *, hot_reload=False, samples_only=False):
        self.samples_only = samples_only
        self.harness = harness or Harness()
        self.runs = {}
        self.lock = threading.Lock()
        self.health_cache = (0, {})
        self.experiment_active = None
        self.experiments = ExperimentService(self)
        self.project = ProjectFiles(state_dir=self.harness.state_dir)
        self.factory = (
            HarnessFactory(state_dir=self.harness.state_dir) if hot_reload else None
        )
        self.backends = {
            getattr(self.harness.runtime, "backend", "llama"): self.harness.runtime
        }
        # 런처·중지 스크립트가 "같은 프로젝트의 같은 버전이 띄운 이 인스턴스"인지 확인할 식별 정보.
        self.identity = {
            "app": "Agent Lens",
            "version": app_version(),
            "project_root": str(ROOT),
            "instance": uuid.uuid4().hex[:12],
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "samples_only": bool(samples_only),
        }

    def health(self):
        return {**self.identity, **self.runtime_health()}

    def runtime_health(self):
        if self.samples_only:
            return {
                "connected": False,
                "loading": False,
                "mode": "sample",
                "backend": "llama",
                "model": "샘플 기록",
                "runtime": "기록 재생",
                "base_url": "모델 실행 없음",
            }
        now = time.monotonic()
        if now - self.health_cache[0] < 4:
            return self.health_cache[1]
        result = {
            "connected": False,
            "model": self.harness.model,
            "base_url": self.harness.runtime.base_url,
            "workspace": str(self.harness.workspace),
            "runtime": "llama.cpp",
            "mode": "live",
            "features": {
                "file_tools": True,
                "skills": True,
                "session_memory": True,
                "commands": True,
                "comparison": True,
                "mcp_client": True,
                "lsp_client": True,
            },
        }
        if hasattr(self.harness.runtime, "status"):
            result.update(self.harness.runtime.status())
            self.health_cache = (now, result)
            return result
        result["backend"] = "llama"
        try:
            # Use a short-lived probe so the normal inference timeout is unchanged.
            from .runtime import LocalRuntime

            probe = LocalRuntime(self.harness.runtime.base_url, timeout=2)
            status = probe.request("/health")
            result["connected"] = status.get("status") == "ok"
            result["health"] = status
            if result["connected"]:
                models = probe.request("/v1/models").get("data", [])
                result["available_models"] = [item.get("id") for item in models]
                if self.harness.model not in result["available_models"]:
                    result["connected"] = False
                    result["error"] = (
                        "설정한 모델 ID가 로컬 서버의 모델 목록에 없습니다."
                    )
        except RuntimeFailure as exc:
            result["error"] = str(exc)
        self.health_cache = (now, result)
        return result

    def start(
        self,
        prompt,
        session_id,
        step,
        launch=True,
        features=None,
        internal=False,
        engine=None,
        requirements=None,
        mode="single",
    ):
        if self.samples_only:
            raise ValueError(
                "샘플 보기 모드입니다. 실제 실행은 일반 모드로 시작하세요."
            )
        self.harness.history_path(session_id)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 6000:
            raise ValueError("프롬프트를 1~6,000자로 입력하세요.")
        with self.lock:
            if self.experiment_active and not internal:
                raise WorkInProgress(
                    "비교가 진행 중입니다. 실행 상태에서 중지하거나 끝날 때까지 기다리세요."
                )
            active = [
                r
                for r in self.runs.values()
                if r.status in {"running", "paused", "stopping"}
            ]
            if active:
                raise WorkInProgress(
                    "진행 중인 요청이 있습니다. 실행 상태에서 계속하거나 중지할 수 있습니다."
                )
            run = Run(
                prompt.strip(),
                session_id,
                step,
                features=features,
                engine=engine,
                requirements=requirements,
                mode=mode,
            )
            self.runs[run.id] = run
        if launch:
            self.launch(run)
        return run

    def launch(self, run):
        try:
            if self.factory:
                prepared, version = self.factory.prepare(self.harness)
                self.harness = prepared
                run.implementation = version
                run.emit(
                    "harness.modules",
                    "이번 실행의 하네스 코드 연결",
                    "저장된 구성 코드를 버전별로 불러왔습니다. 이전 실행의 소스 스냅샷은 바뀌지 않습니다.",
                    category="context",
                    output=version,
                    pause=False,
                )
        except Exception as error:
            run.error = "하네스 코드를 불러오지 못했습니다: " + str(error)
            run.emit(
                "run.error",
                "코드 적용 실패",
                "편집기에서 오류를 수정하거나 이전 내용으로 되돌리세요.",
                category="output",
                status="error",
                output={"error": run.error},
                pause=False,
            )
            run.status = "failed"
            run.finished = time.perf_counter()
            run.save(self.harness.state_dir / "traces")
            with run.condition:
                run.condition.notify_all()
            return
        worker = threading.Thread(target=self.harness.execute, args=(run,), daemon=True)
        run.emit(
            "thread.dispatch",
            "실행 스레드와 하네스 연결",
            "Application.launch()가 Harness.execute를 작업 스레드의 실행 대상으로 지정하고 start()를 호출합니다.",
            category="input",
            input={"target": "Harness.execute", "run_id": run.id},
            output={"thread_name": worker.name},
            pause=False,
        )
        worker.start()

    def activity(self):
        # health 캐시와 분리한다. 저장된 과거 trace의 paused 상태는 실행 중인 작업이 아니다.
        with self.lock:
            active = next(
                (
                    r
                    for r in self.runs.values()
                    if r.status in {"running", "paused", "stopping"}
                ),
                None,
            )
            experiment = (
                self.experiments.items.get(self.experiment_active)
                if self.experiment_active
                else None
            )
            return {
                "run": {
                    "id": active.id,
                    "prompt": active.prompt,
                    "session_id": active.session_id,
                    "status": active.status,
                    "event_count": len(active.events),
                    "step_mode": active.step_mode,
                    "mode": active.mode,
                }
                if active
                else None,
                "experiment": {
                    "id": experiment["id"],
                    "phase": experiment.get("phase"),
                    "current_run": experiment.get("current_run"),
                    "status": experiment["status"],
                }
                if experiment
                else None,
            }

    def busy(self):
        return self.experiment_active or any(
            run.status in {"running", "paused", "stopping"}
            for run in self.runs.values()
        )

    def select_backend(self, name):
        if self.samples_only:
            raise ValueError("샘플 보기 모드에서는 모델 실행기를 시작하지 않습니다.")
        with self.lock:
            if self.busy():
                raise ValueError("실행을 완료하거나 중지한 뒤 실행기를 바꾸세요.")
            if name not in {"reference", "llama"}:
                raise ValueError("실행기를 선택하세요.")
            if name not in self.backends:
                if name == "reference":
                    from .reference_engine import ReferenceRuntime

                    self.backends[name] = ReferenceRuntime()
                else:
                    from .runtime import LocalRuntime

                    self.backends[name] = LocalRuntime(
                        "http://127.0.0.1:8088", timeout=180
                    )
            self.harness.runtime = self.backends[name]
            self.harness.model = "Qwen3-0.6B" if name == "reference" else "qwen3-4b"
            self.health_cache = (0, {})
        return self.health()

    def get_run(self, run_id, after=0, compact=False):
        if run_id.startswith("sample-"):
            slug = run_id.removeprefix("sample-")
            items = json.loads(
                (ROOT / "samples" / "index.json").read_text(encoding="utf-8")
            )
            item = next((v for v in items if v["slug"] == slug), None)
            if not item:
                raise ValueError("샘플 기록을 찾을 수 없습니다.")
            with gzip.open(
                ROOT / "samples" / (item["id"] + ".json.gz"), "rb"
            ) as handle:
                raw = handle.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError("샘플 크기 초과")
            data = json.loads(raw)
            data["sample"]["source_run_id"] = data["id"]
            data["id"] = run_id
            data["events"] = data["events"][after:]
            return data if compact else expand_sources(data)
        if not run_id.isalnum() or len(run_id) != 16:
            raise ValueError("실행 ID가 올바르지 않습니다.")
        if run_id in self.runs:
            return self.runs[run_id].snapshot(after, compact=compact)
        path = self.harness.state_dir / "traces" / (run_id + ".json")
        if not path.exists():
            raise ValueError("실행 기록을 찾을 수 없습니다.")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("status") in {"running", "paused", "stopping"}:
            data["recorded_status"] = data["status"]
            data["status"] = "interrupted"
            data["interruption"] = (
                "이전 서버의 미완료 기록입니다. 현재 실행 중인 작업은 없습니다."
            )
        data["events"] = data["events"][after:]
        if compact:
            return data if "source_snapshots" in data else pack_sources(data)
        return expand_sources(data)

    def history(self):
        collected = {}
        directory = self.harness.state_dir / "traces"
        if directory.exists():
            for path in sorted(
                directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
            )[:20]:
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                    if item.get("status") in {"running", "paused", "stopping"}:
                        item["status"] = "interrupted"
                    collected[item["id"]] = {
                        key: item.get(key)
                        for key in (
                            "id",
                            "prompt",
                            "status",
                            "created_at",
                            "event_count",
                            "model_calls",
                        )
                    }
                except (OSError, ValueError, KeyError):
                    continue
        with self.lock:
            for run in self.runs.values():
                item = run.snapshot()
                collected[run.id] = {
                    key: item[key]
                    for key in (
                        "id",
                        "prompt",
                        "status",
                        "created_at",
                        "event_count",
                        "model_calls",
                    )
                }
        return sorted(
            collected.values(), key=lambda item: item["created_at"], reverse=True
        )[:20]


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentLens/0.5"

        def local_host(self):
            try:
                host = urllib.parse.urlsplit("http://" + self.headers.get("Host", ""))
                return (
                    host.hostname in {"127.0.0.1", "localhost", "::1"}
                    and host.port == self.server.server_port
                )
            except ValueError:
                return False

        def send(
            self,
            status,
            data,
            content_type="application/json; charset=utf-8",
            attachment=None,
        ):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
            )
            if attachment:
                self.send_header(
                    "Content-Disposition", f'attachment; filename="{attachment}"'
                )
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.local_host():
                self.send(403, {"error": "이 로컬 앱 주소로 접속하세요."})
                return
            try:
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                query = urllib.parse.parse_qs(parsed.query)
                if path == "/api/status":
                    self.send(200, {**app.health(), "port": self.server.server_port})
                elif path == "/api/activity":
                    self.send(200, app.activity())
                elif path == "/api/memory":
                    from .context_memory import inspect

                    self.send(
                        200, inspect(app.harness, query.get("session_id", [""])[0])
                    )
                elif path == "/api/history":
                    self.send(200, app.history())
                elif path == "/api/samples":
                    items = json.loads(
                        (ROOT / "samples" / "index.json").read_text(encoding="utf-8")
                    )
                    self.send(200, [{**v, "id": "sample-" + v["slug"]} for v in items])
                elif path.startswith("/api/feedback/"):
                    from .feedback import report

                    self.send(
                        200, report(app.get_run(path.split("/")[3], compact=True))
                    )
                elif path == "/api/implementation":
                    self.send(
                        200,
                        catalog(app.harness, next(reversed(app.runs.values()), None)),
                    )
                elif path == "/api/project":
                    self.send(200, {"files": app.project.list()})
                elif path == "/api/project/manifest":
                    from .project import current_manifest

                    self.send(
                        200,
                        current_manifest(
                            app.factory.root if app.factory else ROOT,
                            workspace=app.harness.workspace,
                        ),
                    )
                elif path == "/api/project/file":
                    self.send(
                        200,
                        app.project.read(
                            query.get("path", [""])[0], query.get("version", [None])[0]
                        ),
                    )
                elif path == "/api/experiments/cases":
                    self.send(200, CASES)
                elif path.startswith("/api/experiments/"):
                    self.send(200, app.experiments.get(path.split("/")[3]))
                elif path == "/api/workspace":
                    files = sorted(
                        str(p.relative_to(app.harness.workspace)).replace("\\", "/")
                        for p in app.harness.workspace.rglob("*")
                        if p.is_file()
                        and p.resolve().is_relative_to(app.harness.workspace)
                    )
                    self.send(
                        200, {"files": files[:200], "name": app.harness.workspace.name}
                    )
                elif path == "/api/file":
                    relative = query.get("path", [""])[0]
                    file_path = workspace_path(app.harness.workspace, relative)
                    if not file_path.is_file() or file_path.stat().st_size > 65536:
                        raise ValueError("읽을 수 없는 파일입니다.")
                    self.send(
                        200,
                        {
                            "path": relative,
                            "content": file_path.read_text(encoding="utf-8-sig"),
                        },
                    )
                elif path.startswith("/api/runs/") and path.endswith("/search"):
                    from .search import search

                    run_id = path.split("/")[3]
                    kinds = [k for k in query.get("kind", []) if k]
                    self.send(
                        200,
                        search(
                            app.get_run(run_id, 0, compact=True),
                            query.get("q", [""])[0],
                            kinds=set(kinds) or None,
                        ),
                    )
                elif path.startswith("/api/runs/") and (
                    path.endswith("/export/preview") or path.endswith("/export/share")
                ):
                    from . import redaction

                    run_id = path.split("/")[3]
                    snapshot = app.get_run(run_id, 0, compact=True)
                    options = redaction.parse_options(query)
                    if path.endswith("/export/preview"):
                        self.send(
                            200,
                            redaction.preview(
                                snapshot, options, workspace=app.harness.workspace
                            ),
                        )
                    else:
                        self.send(
                            200,
                            redaction.share_copy(
                                snapshot, options, workspace=app.harness.workspace
                            ),
                            attachment=f"agent-lens-{run_id}-share.json",
                        )
                elif path.startswith("/api/runs/"):
                    run_id = path.split("/")[3]
                    after = max(0, int(query.get("after", ["0"])[0]))
                    snapshot = app.get_run(
                        run_id,
                        0 if path.endswith("/export") else after,
                        compact=path.endswith("/export")
                        or query.get("compact") == ["1"],
                    )
                    self.send(
                        200,
                        snapshot,
                        attachment=f"agent-lens-{run_id}.json"
                        if path.endswith("/export")
                        else None,
                    )
                else:
                    filename = "index.html" if path == "/" else path.lstrip("/")
                    if filename not in {
                        "index.html",
                        "workbench.js",
                        "architecture.js",
                        "inspection.js",
                        "diagram.css",
                        "workbench.css",
                        "favicon.svg",
                    }:
                        self.send(404, {"error": "찾을 수 없는 경로입니다."})
                        return
                    static_path = ROOT / "web" / filename
                    mime = (
                        mimetypes.guess_type(filename)[0] or "application/octet-stream"
                    )
                    self.send(
                        200,
                        static_path.read_bytes(),
                        mime
                        + (
                            "; charset=utf-8"
                            if mime.startswith("text/") or "javascript" in mime
                            else ""
                        ),
                    )
            except (ValueError, OSError, IndexError) as exc:
                self.send(400, {"error": str(exc)})

        def reject_post(self, message):
            # 작은 본문을 비워 Windows의 unread-body TCP reset 대신 HTTP 403을 돌려준다.
            self.close_connection = True
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if 0 < length <= 1048576:
                    self.connection.settimeout(1)
                    self.rfile.read(length)
            except (ValueError, OSError):
                pass
            self.send(403, {"error": message})

        def do_POST(self):
            if not self.local_host():
                self.reject_post("이 로컬 앱 주소로 접속하세요.")
                return
            try:
                origin = self.headers.get("Origin")
                if origin:
                    parsed_origin = urllib.parse.urlsplit(origin)
                    if (
                        parsed_origin.hostname not in {"127.0.0.1", "localhost", "::1"}
                        or parsed_origin.port != self.server.server_port
                    ):
                        self.reject_post("이 로컬 앱의 요청만 허용합니다.")
                        return
                if (
                    self.headers.get("Content-Type", "").split(";")[0]
                    != "application/json"
                ):
                    raise ValueError("application/json 요청이 필요합니다.")
                length = int(self.headers.get("Content-Length", "0"))
                limit = 1048576 if self.path.startswith("/api/project/") else 40000
                if length <= 0 or length > limit:
                    raise ValueError("요청 크기가 허용 범위를 벗어났습니다.")
                raw = self.rfile.read(length)
                decoded = raw.decode("utf-8")
                payload = json.loads(decoded)
                if self.path == "/api/runs":
                    client = payload.get("client_trace", {})
                    if not isinstance(client, dict) or len(json.dumps(client)) > 2000:
                        raise ValueError("브라우저 관찰 정보가 올바르지 않습니다.")
                    run = app.start(
                        payload.get("prompt"),
                        payload.get("session_id", "default"),
                        bool(payload.get("step")),
                        launch=False,
                        features=payload.get("features"),
                        engine=payload.get("engine"),
                        requirements=payload.get("requirements"),
                        mode=payload.get("mode", "single"),
                    )
                    if client:
                        run.emit(
                            "browser.input",
                            "브라우저에서 입력한 문자열",
                            "실제 input 이벤트 핸들러가 보고한 문자·UTF-8 바이트 수입니다. 첫 입력 시각이 없으면 기본 예제 또는 프로그램으로 채운 입력입니다. 키 입력 전체를 저장하지 않습니다.",
                            actor="browser",
                            category="input",
                            output={"text": payload.get("prompt"), **client},
                            pause=False,
                            source_override=browser_source(
                                "BROWSER_INPUT", "observeInput"
                            ),
                        )
                        run.emit(
                            "browser.serialize",
                            "문자열과 설정을 JSON 요청으로 전달",
                            "submit()이 요청 객체를 구성하고 api()의 JSON.stringify와 fetch를 통해 전송했습니다. raw_json_text는 서버가 실제 수신한 본문이며 브라우저 관찰 정보도 포함됩니다.",
                            actor="browser",
                            category="input",
                            input={"prompt": payload.get("prompt")},
                            output={
                                "raw_json_text": decoded,
                                "actual_received_bytes": len(raw),
                            },
                            pause=False,
                            source_override=browser_source("BROWSER_SUBMIT", "submit"),
                        )
                    run.emit(
                        "http.body.read",
                        "HTTP 요청 본문을 바이트로 수신",
                        "브라우저의 fetch 요청을 Handler.do_POST가 받았습니다. Content-Length를 검사하고 rfile.read()로 실제 본문을 읽었습니다.",
                        category="input",
                        input={
                            "method": "POST",
                            "path": self.path,
                            "declared_content_length": length,
                        },
                        output={
                            "actual_bytes": len(raw),
                            "first_48_bytes_hex": raw[:48].hex(" "),
                        },
                        pause=False,
                    )
                    run.emit(
                        "http.json.decode",
                        "요청 바이트를 UTF-8·JSON으로 해석",
                        "raw.decode('utf-8')로 문자열을 만들고 json.loads()로 객체를 구성했습니다. prompt 필드를 꺼내 하네스에 전달합니다.",
                        category="input",
                        input={"raw_json_text": decoded},
                        output={"parsed_object": payload},
                        pause=False,
                    )
                    app.launch(run)
                    self.send(201, {"id": run.id})
                elif self.path == "/api/experiments":
                    experiment_id = app.experiments.start(
                        payload.get("prompt"),
                        payload.get("left"),
                        payload.get("right"),
                        payload.get("case_id"),
                        payload.get("left_engine"),
                        payload.get("right_engine"),
                        payload.get("left_mode", "single"),
                        payload.get("right_mode", "single"),
                    )
                    self.send(201, {"id": experiment_id})
                elif self.path == "/api/backend":
                    self.send(200, app.select_backend(payload.get("backend")))
                elif self.path == "/api/project/check":
                    app.project.resolve(payload.get("path"), write=True)
                    self.send(200, check_text(payload["path"], payload.get("text")))
                elif self.path == "/api/project/save":
                    with app.lock:
                        if app.busy():
                            raise ValueError(
                                "실행을 완료하거나 중지한 뒤 저장하세요. 편집 중인 내용은 화면에 유지됩니다."
                            )
                        self.send(
                            200,
                            app.project.save(
                                payload.get("path"),
                                payload.get("text"),
                                payload.get("expected_sha256"),
                            ),
                        )
                elif self.path == "/api/project/restore":
                    with app.lock:
                        if app.busy():
                            raise ValueError("실행을 완료하거나 중지한 뒤 되돌리세요.")
                        self.send(
                            200,
                            app.project.restore(
                                payload.get("edit_id", ""),
                                payload.get("expected_sha256"),
                            ),
                        )
                elif self.path.startswith("/api/experiments/") and self.path.endswith(
                    "/stop"
                ):
                    self.send(200, app.experiments.stop(self.path.split("/")[3]))
                elif self.path.startswith("/api/runs/") and self.path.endswith(
                    "/render"
                ):
                    run = app.runs.get(self.path.split("/")[3])
                    if (
                        not run
                        or run.status not in {"completed", "failed"}
                        or payload.get("text") != (run.error or run.answer)
                    ):
                        raise ValueError("확정된 실행 결과의 표시 정보가 필요합니다.")
                    with run.lock:
                        if not getattr(run, "render_acknowledged", False):
                            run.emit(
                                "browser.render",
                                "최종 결과를 브라우저 본문에 반영",
                                "renderAnswer()가 textContent를 설정한 뒤 DOM 본문을 보고했습니다. 사용자가 읽었는지나 모델의 숨겨진 생각을 측정한 기록은 아닙니다.",
                                actor="browser",
                                category="output",
                                output={
                                    "characters": len(payload["text"]),
                                    "text": payload["text"],
                                    "reported_at": payload.get("reported_at"),
                                },
                                pause=False,
                                source_override=browser_source(
                                    "BROWSER_RENDER", "renderAnswer"
                                ),
                            )
                            run.render_acknowledged = True
                            run.save(app.harness.state_dir / "traces")
                    self.send(200, {"recorded": True})
                elif self.path.startswith("/api/runs/") and self.path.endswith(
                    "/control"
                ):
                    run_id = self.path.split("/")[3]
                    run = app.runs.get(run_id)
                    if not run:
                        raise ValueError("제어 가능한 실행이 없습니다.")
                    if payload.get("action") not in {"next", "continue", "stop"}:
                        raise ValueError("지원하지 않는 실행 제어입니다.")
                    if run.status not in {"running", "paused", "stopping"}:
                        self.send(200, {"status": run.status, "already_finished": True})
                        return
                    run.control(payload.get("action"))
                    self.send(200, {"status": run.status})
                else:
                    self.send(404, {"error": "찾을 수 없는 경로입니다."})
            except WorkInProgress as exc:
                self.send(409, {"error": str(exc), "activity": app.activity()})
            except (ValueError, TypeError, AttributeError) as exc:
                self.send(400, {"error": str(exc)})

        def log_message(self, fmt, *args):
            if "/api/runs/" not in str(args):
                super().log_message(fmt, *args)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Agent Lens · 로컬 에이전트 관찰 화면")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--model-url", default="http://127.0.0.1:8088")
    parser.add_argument("--model", default="qwen3-4b")
    parser.add_argument(
        "--samples-only",
        action="store_true",
        help="가중치를 불러오지 않고 실제 저장 기록을 재생",
    )
    parser.add_argument(
        "--backend", choices=["reference", "llama"], default="reference"
    )
    args = parser.parse_args()
    if args.backend == "reference" and not args.samples_only:
        from .reference_engine import ReferenceRuntime

        runtime = ReferenceRuntime()
        harness = Harness(runtime=runtime, model=runtime.model_id)
    else:
        harness = Harness(base_url=args.model_url, model=args.model)
    app = Application(
        harness, hot_reload=not args.samples_only, samples_only=args.samples_only
    )
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app))
    print(f"Agent Lens: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        from .integrations import close_all

        close_all()


if __name__ == "__main__":
    main()
