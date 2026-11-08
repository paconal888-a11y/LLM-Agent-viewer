"""작업 공간 경로와 실제 파일 I/O 관찰."""

import hashlib
from pathlib import Path


def workspace_path(workspace, relative):
    if not isinstance(relative, str) or not relative or len(relative) > 300:
        raise ValueError("파일 경로는 1~300자 문자열이어야 합니다.")
    requested = Path(relative.replace("\\", "/"))
    if requested.is_absolute() or ":" in relative:
        raise ValueError("작업 공간 안의 상대 경로만 허용합니다.")
    resolved = (workspace / requested).resolve()
    if not resolved.is_relative_to(workspace.resolve()):
        raise ValueError("작업 공간 밖의 경로입니다.")
    return resolved


def read_traced(
    run, path, category, *, reason, parent=None, required=True, max_bytes=65536, store_text=True
):
    """파일시스템 호출과 디코딩을 각각 실제 실행한 뒤 결과를 기록한다."""
    run.emit(
        "file.resolve",
        "파일 경로 확정",
        reason,
        category=category,
        input={"requested_path": str(path)},
        output={"absolute_path": str(path.resolve())},
        parent=parent,
    )
    exists = path.is_file()
    run.emit(
        "file.exists",
        "파일 존재 여부 확인",
        "Path.is_file()의 반환값에 따라 읽기 또는 건너뛰기로 분기합니다.",
        category=category,
        actor="filesystem",
        input={"path": str(path)},
        output={"exists": exists},
        parent=parent,
    )
    if not exists:
        if required:
            raise ValueError(f"파일을 찾을 수 없습니다: {path.name}")
        return ""
    if path.stat().st_size > max_bytes:
        raise ValueError(f"허용된 읽기 크기 {max_bytes}바이트를 초과했습니다.")
    handle = path.open("rb")
    try:
        run.emit(
            "file.open",
            "파일 핸들 열기",
            "Python이 파일시스템에 읽기 전용 바이너리 열기를 요청했습니다.",
            category=category,
            actor="filesystem",
            input={"path": str(path), "mode": "rb"},
            output={"handle_open": not handle.closed},
            parent=parent,
        )
        raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError("읽기 도중 파일이 허용 크기를 초과했습니다.")
        run.emit(
            "file.read",
            "바이트 읽기",
            "파일 핸들에서 실제 바이트를 읽었습니다. 아직 모델에 전달한 상태는 아닙니다.",
            category=category,
            actor="filesystem",
            input={"path": str(path)},
            output={
                "byte_count": len(raw),
                "first_48_bytes_hex": raw[:48].hex(" "),
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
            parent=parent,
        )
    finally:
        handle.close()
    run.emit(
        "file.close",
        "파일 핸들 닫기",
        "close() 완료 후 파일 핸들의 closed 값을 확인했습니다.",
        category=category,
        actor="filesystem",
        input={"path": str(path)},
        output={"closed": handle.closed},
        parent=parent,
    )
    text = raw.decode("utf-8-sig")
    run.emit(
        "file.decode",
        "UTF-8 바이트를 문자열로 변환",
        "이 원문이 이후 지침·참고 자료·도구 결과 중 어디에 들어가는지 추적합니다.",
        category=category,
        input={"path": str(path), "encoding": "utf-8-sig", "byte_count": len(raw)},
        # 대화 원본처럼 큰 파일은 기록이 불어나지 않게 본문 없이 크기와 해시만 남긴다.
        output={
            **({"text": text} if store_text else {"text_omitted": True, "sha256": hashlib.sha256(raw).hexdigest()}),
            "characters": len(text),
            "lines": len(text.splitlines()),
        },
        parent=parent,
    )
    return text
