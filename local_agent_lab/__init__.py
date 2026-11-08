"""Local model connectivity and harness experiments."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def app_version():
    """pyproject.toml의 project.version. 런처가 같은 파일을 읽어 실행 중인 앱과 대조한다."""
    try:
        import tomllib

        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except (OSError, KeyError, ValueError):
        return "unknown"
