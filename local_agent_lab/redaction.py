"""공유용 실행 기록 사본을 만든다. 원본 기록은 바꾸지 않는다.

기록에는 프롬프트·파일 원문·도구 결과·소스 스냅샷·로컬 경로가 그대로 들어간다. 이 모듈은
(1) 무엇이 치환될지 미리 보여주고(preview), (2) 치환한 깊은 복사본을 돌려준다(apply).
문자열 안에 JSON이 다시 들어 있는 필드(raw_json_text, 도구 결과 content 등)와 소스 스냅샷도
모두 문자열 단위로 같은 규칙을 적용한다. JSON 이스케이프 때문에 경로 구분자가 '\\\\'로 적힌 경우도
같은 경로로 인식한다. 치환한 사본에는 무엇을 몇 번 바꿨는지 share 필드로 남긴다.
"""

from __future__ import annotations

import copy
import getpass
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .paths import data_home
from .trace import ROOT

DEFAULT_OPTIONS = {
    "paths": True,  # 프로젝트·작업 공간·홈·LOCALAPPDATA 경로와 사용자 이름
    "secrets": True,  # API 키·토큰·개인 키 블록
    "emails": True,
    "ips": True,  # loopback 이외의 IPv4
    "omit_file_text": False,  # file.decode·read_file 원문 제거
    "omit_sources": False,  # 실행 당시 소스 스냅샷 코드 제거
}

SECRET_PATTERNS = [
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
    ),
]
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
LOOPBACK = re.compile(r"^(?:127\.|0\.0\.0\.0$)")
GENERIC_HOME = re.compile(
    r"(?:[A-Za-z]:(?:\\\\|\\|/)+Users(?:\\\\|\\|/)+|/home/|/Users/)(?P<user>[^\\/\"'<>|\s]+)"
)
TEXT_FIELDS = {"text", "content", "raw_text", "code"}


def _separators(path: str):
    """같은 경로의 표기 변형: 역슬래시, JSON 이스케이프된 역슬래시, 슬래시."""
    text = str(path).rstrip("\\/")
    if not text:
        return []
    parts = re.split(r"[\\/]+", text)
    variants = {"\\".join(parts), "\\\\".join(parts), "/".join(parts)}
    pattern = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
    return [re.compile("(?i)" + pattern)]


class Redactor:
    def __init__(self, options=None, *, workspace=None, home=None, user=None):
        self.options = {**DEFAULT_OPTIONS, **(options or {})}
        self.counts = {}
        self.examples = {}
        home = Path(home) if home else Path.home()
        self.user = user if user is not None else _current_user()
        # 긴 경로를 먼저 치환해야 홈 치환이 프로젝트 경로를 먼저 잘라 먹지 않는다.
        self.path_rules = []
        if self.options["paths"]:
            ordered = []
            if workspace:
                ordered.append(("<WORKSPACE>", Path(workspace)))
            ordered.append(("<PROJECT>", ROOT))
            try:
                ordered.append(("<LOCALAPPDATA>", data_home().parent))
            except (OSError, RuntimeError):
                pass
            ordered.append(("<USER_HOME>", home))
            ordered.sort(key=lambda item: len(str(item[1])), reverse=True)
            for label, path in ordered:
                for pattern in _separators(str(path)):
                    self.path_rules.append((label, pattern))

    def _note(self, category, original, replacement):
        self.counts[category] = self.counts.get(category, 0) + 1
        bucket = self.examples.setdefault(category, [])
        if len(bucket) < 5 and original not in bucket:
            bucket.append(original if len(original) <= 120 else original[:117] + "...")
        return replacement

    def text(self, value: str) -> str:
        if not value:
            return value
        if self.options["paths"]:
            for label, pattern in self.path_rules:
                value = pattern.sub(lambda m: self._note("path", m.group(0), label), value)
            value = GENERIC_HOME.sub(
                lambda m: self._note("path", m.group(0), "<USER_HOME>"), value
            )
            if self.user and len(self.user) >= 3:
                pattern = re.compile(r"(?<![\w.-])" + re.escape(self.user) + r"(?![\w-])")
                value = pattern.sub(lambda m: self._note("user", m.group(0), "<USER>"), value)
        if self.options["secrets"]:
            for name, pattern in SECRET_PATTERNS:
                value = pattern.sub(
                    lambda m, name=name: self._note(
                        "secret", m.group(0), "<REDACTED:" + name + ">"
                    ),
                    value,
                )
        if self.options["emails"]:
            value = EMAIL.sub(lambda m: self._note("email", m.group(0), "<EMAIL>"), value)
        if self.options["ips"]:
            value = IPV4.sub(
                lambda m: m.group(0)
                if LOOPBACK.match(m.group(0))
                else self._note("ip", m.group(0), "<IP>"),
                value,
            )
        return value

    def walk(self, value, key=None, path=()):
        if isinstance(value, str):
            if self.options["omit_file_text"] and key == "text" and _under(path, ("output",)) and _event_kind(path) in {
                "file.decode"
            }:
                return self._note("omitted_file_text", f"{len(value)}자", f"<OMITTED {len(value)} chars>")
            if self.options["omit_file_text"] and key == "content" and _event_kind(path) == "tool.result":
                return self._note("omitted_file_text", f"{len(value)}자", f"<OMITTED {len(value)} chars>")
            return self.text(value)
        if isinstance(value, list):
            return [self.walk(item, key, path + (key,)) for item in value]
        if isinstance(value, dict):
            result = {}
            for child_key, child in value.items():
                if (
                    self.options["omit_sources"]
                    and child_key == "code"
                    and isinstance(child, str)
                    and (path and path[0] == "source_snapshots")
                ):
                    result[child_key] = self._note("omitted_source", f"{len(child)}자", "<OMITTED SOURCE>")
                    continue
                result[child_key] = self.walk(child, child_key, path + (child_key,))
            return result
        return value


def _current_user():
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - 환경에 따라 사용자 조회가 실패할 수 있다.
        return os.environ.get("USERNAME") or os.environ.get("USER") or ""


def _under(path, keys):
    return any(k in path for k in keys)


def _event_kind(path):
    # walk()는 이벤트의 kind를 알 수 없으므로 호출자가 events를 순회하며 kind를 path에 끼워 넣는다.
    for part in path:
        if isinstance(part, str) and part.startswith("kind:"):
            return part[5:]
    return None


def preview(snapshot, options=None, *, workspace=None, home=None, user=None):
    """치환하지 않고 무엇이 몇 번 치환될지 센다."""
    redactor = Redactor(options, workspace=workspace, home=home, user=user)
    _apply(redactor, snapshot)
    return {
        "counts": redactor.counts,
        "examples": redactor.examples,
        "options": redactor.options,
        "event_count": len(snapshot.get("events", [])),
        "includes": _includes(snapshot),
        "note": "미리보기는 원본을 바꾸지 않습니다. 치환 대상은 문자열 안의 JSON과 소스 스냅샷까지 같은 규칙으로 찾습니다. 정규식 기반이라 모든 비밀값을 찾는다고 보증하지 않습니다.",
    }


def share_copy(snapshot, options=None, *, workspace=None, home=None, user=None):
    """치환한 깊은 복사본. 원본 객체는 바꾸지 않는다."""
    redactor = Redactor(options, workspace=workspace, home=home, user=user)
    result = _apply(redactor, snapshot)
    result["share"] = {
        "redacted": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_run_id": snapshot.get("id"),
        "options": redactor.options,
        "counts": redactor.counts,
        "placeholders": sorted(
            {"<PROJECT>", "<USER_HOME>", "<USER>", "<WORKSPACE>", "<LOCALAPPDATA>", "<EMAIL>", "<IP>"}
        ),
        "note": "공유용 사본입니다. 원본 기록은 .runs/traces에 그대로 있습니다. 바이트 수·해시·토큰 수는 치환 전 실제 측정값이며 다시 계산하지 않았습니다.",
    }
    return result


def _apply(redactor, snapshot):
    result = {}
    for key, value in snapshot.items():
        if key == "events" and isinstance(value, list):
            result[key] = [
                redactor.walk(event, "events", ("events", "kind:" + str(event.get("kind"))))
                for event in value
            ]
        else:
            result[key] = redactor.walk(copy.deepcopy(value), key, (key,))
    return result


def _includes(snapshot):
    kinds = {}
    for event in snapshot.get("events", []):
        kinds[event.get("kind")] = kinds.get(event.get("kind"), 0) + 1
    return {
        "prompt": bool(snapshot.get("prompt")),
        "file_texts": kinds.get("file.decode", 0),
        "tool_results": kinds.get("tool.result", 0),
        "model_requests": kinds.get("model.request", 0),
        "source_snapshots": len(snapshot.get("source_snapshots", {}) or {}),
        "browser_input": kinds.get("browser.input", 0),
    }


def parse_options(query):
    """쿼리 문자열의 1/0 값을 옵션으로. 모르는 키는 무시한다."""
    options = {}
    for key in DEFAULT_OPTIONS:
        raw = query.get(key, [None])[0]
        if raw in {"0", "1"}:
            options[key] = raw == "1"
    return options
