"""프로젝트 파일 편집과 실행용 하네스 버전. 웹 서버 자체는 다시 불러오지 않는다."""

from __future__ import annotations

import ast
import codecs
import hashlib
import importlib
import json
import os
import re
import sys
import threading
import time
import types
import uuid
from pathlib import Path

from .trace import ROOT, code_source

MODULES = (
    "files",
    "toolkit",
    "websearch",
    "policy",
    "context",
    "inference",
    "compaction",
    "checkpoint",
    "agent_loop",
    "harness",
)
EDITABLE_CODE = {f"local_agent_lab/{name}.py" for name in MODULES}
READABLE_CODE = EDITABLE_CODE | {
    "local_agent_lab/reference_engine.py",
    "local_agent_lab/model_files.py",
    "local_agent_lab/runtime.py",
    "local_agent_lab/trace.py",
    "local_agent_lab/web.py",
    "local_agent_lab/project.py",
    "web/workbench.js",
    "local_agent_lab/stdio_rpc.py",
    "local_agent_lab/integrations.py",
    "local_agent_lab/demo_mcp_server.py",
    "local_agent_lab/feedback.py",
    "local_agent_lab/orchestration.py",
    "local_agent_lab/context_memory.py",
    "web/architecture.js",
    "web/inspection.js",
    "web/diagram.css",
}


def current_manifest(root=ROOT, workspace=None):
    """지금 디스크의 편집 가능 모듈 버전과 작업 공간 서명. 과거 실행의 implementation·conditions와 대조한다."""
    from .features import workspace_signature

    root = Path(root).resolve()
    manifest = [
        {
            "path": f"local_agent_lab/{name}.py",
            "sha256": sha(
                (root / "local_agent_lab" / f"{name}.py").read_text(encoding="utf-8")
            ),
        }
        for name in MODULES
    ]
    result = {"version": sha(json.dumps(manifest, sort_keys=True)), "files": manifest}
    if workspace is not None:
        result["workspace"] = workspace_signature(Path(workspace))
    return result


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_newlines(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def decode_source(raw):
    """파일 바이트를 편집기용 텍스트(LF·BOM 없음)와, 저장 때 되살릴 줄바꿈·BOM 형식으로 나눈다."""
    bom = raw.startswith(codecs.BOM_UTF8)
    decoded = raw[len(codecs.BOM_UTF8) :].decode("utf-8") if bom else raw.decode("utf-8")
    crlf = decoded.count("\r\n")
    counts = {
        "crlf": crlf,
        "lf": decoded.count("\n") - crlf,
        "cr": decoded.count("\r") - crlf,
    }
    present = [name for name, count in counts.items() if count]
    return normalize_newlines(decoded), {
        "newline": max(present, key=counts.get) if present else None,
        "mixed": len(present) > 1,
        "bom": bom,
    }


def encode_source(text, file_format):
    """편집기 텍스트를 파일의 원래 줄바꿈·BOM으로 되돌린다. 줄바꿈이 없던 파일에 새 줄이 생기면 LF로 쓴다."""
    newline = {"crlf": "\r\n", "cr": "\r"}.get(file_format["newline"], "\n")
    body = normalize_newlines(text).replace("\n", newline)
    return (codecs.BOM_UTF8 if file_format["bom"] else b"") + body.encode("utf-8")


def check_text(path, text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > 262144:
        raise ValueError("파일은 UTF-8 256 KiB 이내로 작성하세요.")
    try:
        if path.endswith(".py"):
            compile(ast.parse(text, filename=path), path, "exec")
        elif path.endswith(".json"):
            json.loads(text)
        return {
            "passed": True,
            "checker": "Python 구문 검사"
            if path.endswith(".py")
            else "JSON 구문 검사"
            if path.endswith(".json")
            else "텍스트 확인",
        }
    except (SyntaxError, ValueError) as error:
        return {
            "passed": False,
            "message": str(error),
            "line": getattr(error, "lineno", None),
            "column": getattr(error, "offset", None),
        }


class HarnessFactory:
    def __init__(self, root=ROOT, state_dir=None):
        self.root = Path(root).resolve()
        self.state_dir = Path(state_dir or self.root / ".runs")
        self.bundles = {}
        self.latest = None

    def prepare(self, previous):
        contents = {
            f"local_agent_lab/{name}.py": (
                self.root / "local_agent_lab" / f"{name}.py"
            ).read_text(encoding="utf-8")
            for name in MODULES
        }
        manifest = [
            {"path": path, "sha256": sha(text)} for path, text in contents.items()
        ]
        version = sha(json.dumps(manifest, sort_keys=True))
        if version not in self.bundles:
            namespace = "local_agent_lab._version_" + version[:16]
            package = types.ModuleType(namespace)
            package.__path__ = [str(self.root / "local_agent_lab")]
            package.__package__ = namespace
            sys.modules[namespace] = package
            # 실행 상태·예외형·통신 어댑터는 서버와 공유하고 하네스 구성만 독립적으로 불러온다.
            for name in (
                "trace",
                "runtime",
                "features",
                "paths",
                "reference_engine",
                "integrations",
                "feedback",
                "orchestration",
                "context_memory",
            ):
                sys.modules[namespace + "." + name] = importlib.import_module(
                    "local_agent_lab." + name
                )
            loaded = []
            try:
                for name in MODULES:
                    module_name = namespace + "." + name
                    module = types.ModuleType(module_name)
                    module.__file__ = str(self.root / "local_agent_lab" / f"{name}.py")
                    module.__package__ = namespace
                    sys.modules[module_name] = module
                    loaded.append(module_name)
                    exec(
                        compile(
                            contents[f"local_agent_lab/{name}.py"],
                            module.__file__,
                            "exec",
                        ),
                        module.__dict__,
                    )
                    # code 객체는 줄 번호·주석이 같으면 같다고 판정되어 이전 소스 문자열이 재사용된다.
                    code_source.cache_clear()
                    for value in list(module.__dict__.values()):
                        if getattr(value, "__module__", None) != module_name:
                            continue
                        if isinstance(value, types.FunctionType):
                            code_source(value.__code__)
                        elif isinstance(value, type):
                            for method in value.__dict__.values():
                                if isinstance(method, types.FunctionType):
                                    code_source(method.__code__)
                module = sys.modules[namespace + ".harness"]
                if not callable(getattr(module, "Harness", None)):
                    raise ValueError("harness.py에 Harness 클래스가 필요합니다.")
                specs = module.TOOLS
                implementations = module.IMPLEMENTATIONS
                names = [tool["function"]["name"] for tool in specs]
                if len(names) != len(set(names)) or any(
                    not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", name)
                    for name in names
                ):
                    raise ValueError(
                        "도구 이름이 중복되었거나 형식이 올바르지 않습니다."
                    )
                if any(not callable(implementations.get(name)) for name in names):
                    raise ValueError("도구 목록의 각 이름에 실행 함수가 필요합니다.")
                self.bundles[version] = {
                    "module": module,
                    "files": manifest,
                    "version": version,
                }
                destination = self.state_dir / "code" / version
                for path, text in contents.items():
                    target = destination / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text, encoding="utf-8")
            except Exception:
                for name in list(sys.modules):
                    if name == namespace or name.startswith(namespace + "."):
                        sys.modules.pop(name, None)
                raise
        bundle = self.bundles[version]
        harness = bundle["module"].Harness(
            workspace=previous.workspace,
            state_dir=previous.state_dir,
            runtime=previous.runtime,
            model=previous.model,
        )
        self.latest = bundle
        return harness, {"version": version, "files": manifest}


class ProjectFiles:
    def __init__(self, root=ROOT, state_dir=None):
        self.root = Path(root).resolve()
        self.state_dir = Path(state_dir or self.root / ".runs")
        self.lock = threading.Lock()

    def resolve(self, relative, write=False):
        if (
            not isinstance(relative, str)
            or len(relative) > 300
            or "\\" in relative
            or ":" in relative
        ):
            raise ValueError("프로젝트의 상대 경로를 사용하세요.")
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("프로젝트 밖의 파일입니다.")
        if relative.startswith("demo_workspace/"):
            workspace = (self.root / "demo_workspace").resolve()
            if not path.is_relative_to(workspace) or not path.is_file():
                raise ValueError("실습 작업 공간의 파일을 선택하세요.")
            if path.suffix not in {".md", ".json", ".py", ".txt"}:
                raise ValueError("실습용 텍스트 파일만 지원합니다.")
        elif relative not in (
            EDITABLE_CODE if write else READABLE_CODE | {"README.md"}
        ):
            raise ValueError("편집기에서 제공하는 파일이 아닙니다.")
        if not path.is_file() or path.stat().st_size > 262144:
            raise ValueError("읽을 수 없는 파일입니다.")
        return path

    def list(self):
        items = []
        for relative in sorted(READABLE_CODE | {"README.md"}):
            path = self.root / relative
            if path.is_file():
                items.append(
                    {
                        "path": relative,
                        "kind": "harness"
                        if relative in EDITABLE_CODE
                        else "engine"
                        if relative.endswith("reference_engine.py")
                        else "reference",
                        "editable": relative in EDITABLE_CODE,
                        "apply": "next_run"
                        if relative in EDITABLE_CODE
                        else "read_only",
                    }
                )
        workspace = self.root / "demo_workspace"
        for path in sorted(workspace.rglob("*")):
            if (
                path.is_file()
                and path.resolve().is_relative_to(workspace.resolve())
                and path.suffix in {".md", ".json", ".py", ".txt"}
                and path.stat().st_size <= 262144
            ):
                items.append(
                    {
                        "path": path.relative_to(self.root).as_posix(),
                        "kind": "workspace",
                        "editable": True,
                        "apply": "next_read",
                    }
                )
        return items

    def read(self, relative, version=None):
        path = self.resolve(relative)
        if version:
            if (
                not re.fullmatch(r"[a-f0-9]{64}", version)
                or relative not in EDITABLE_CODE
            ):
                raise ValueError("저장된 코드 버전이 올바르지 않습니다.")
            path = self.state_dir / "code" / version / relative
            if not path.is_file():
                raise ValueError("저장된 코드 버전이 없습니다.")
        text, file_format = decode_source(path.read_bytes())
        return {
            "path": relative,
            "text": text,
            "sha256": sha(text),
            "version": version,
            "file_format": file_format,
            "editable": not version
            and (relative in EDITABLE_CODE or relative.startswith("demo_workspace/")),
        }

    def save(self, relative, text, expected_sha, exact_bytes=None):
        with self.lock:
            path = self.resolve(relative, write=True)
            before = path.read_bytes()
            current, file_format = decode_source(before)
            if sha(current) != expected_sha:
                raise ValueError(
                    "파일이 다른 곳에서 변경되었습니다. 다시 불러온 뒤 수정 내용을 확인하세요."
                )
            check = check_text(relative, text)
            if not check["passed"]:
                return {"saved": False, "validation": check}
            text = normalize_newlines(text)
            # 편집기는 LF 텍스트만 다룬다. 쓸 때 원래 줄바꿈·BOM을 되살려 내용 없는 바이트 변경을 만들지 않는다.
            payload = (
                encode_source(text, file_format) if exact_bytes is None else exact_bytes
            )
            edit_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
            backup = self.state_dir / "edits" / edit_id
            backup.mkdir(parents=True, exist_ok=True)
            (backup / "before.bin").write_bytes(before)
            (backup / "before.txt").write_text(current, encoding="utf-8")
            (backup / "after.txt").write_text(text, encoding="utf-8")
            (backup / "edit.json").write_text(
                json.dumps(
                    {
                        "path": relative,
                        "before_sha256": sha(current),
                        "after_sha256": sha(text),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            temporary = path.with_name(
                path.name + ".agent-lens-" + uuid.uuid4().hex[:6] + ".tmp"
            )
            try:
                temporary.write_bytes(payload)
                os.replace(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
            return {
                "saved": True,
                "path": relative,
                "sha256": sha(text),
                "edit_id": edit_id,
                "validation": check,
                "file_format": decode_source(payload)[1],
                "newline_unified": file_format["mixed"] and exact_bytes is None,
                "applies": "다음 실행부터 반영됩니다."
                if relative in EDITABLE_CODE
                else "다음 파일 읽기부터 반영됩니다.",
            }

    def restore(self, edit_id, expected_sha):
        if not re.fullmatch(r"[0-9-]+-[a-f0-9]{8}", edit_id):
            raise ValueError("수정 기록 ID가 올바르지 않습니다.")
        directory = self.state_dir / "edits" / edit_id
        if not (directory / "edit.json").is_file() or not (directory / "before.txt").is_file():
            raise ValueError("수정 기록을 찾을 수 없습니다: " + edit_id)
        metadata = json.loads((directory / "edit.json").read_text(encoding="utf-8"))
        original = (directory / "before.txt").read_text(encoding="utf-8")
        # 이전 버전이 만든 수정 기록에는 원본 바이트가 없다. 그때는 현재 파일의 줄바꿈·BOM으로 복원한다.
        exact = directory / "before.bin"
        return self.save(
            metadata["path"],
            original,
            expected_sha,
            exact_bytes=exact.read_bytes() if exact.is_file() else None,
        )
