"""모델과 실행 자료는 소스 저장소 밖에 둔다."""

import os
from pathlib import Path


def data_home():
    # 런처가 기존 설치 위치를 찾았거나 사용자가 지정한 경우. 지정하지 않으면 OS 기본 위치를 쓴다.
    override = os.environ.get("AGENT_LENS_HOME")
    if override:
        return Path(override)
    if os.name == "nt":
        return (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            / "AgentLens"
        )
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "agent-lens"


def reference_model_path():
    return data_home() / "models" / "Qwen3-0.6B"
