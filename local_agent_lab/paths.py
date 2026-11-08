"""모델과 실행 자료는 소스 저장소 밖에 둔다."""

import os
from pathlib import Path


def data_home():
    if os.name == "nt":
        return (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            / "AgentLens"
        )
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "agent-lens"


def reference_model_path():
    return data_home() / "models" / "Qwen3-0.6B"
