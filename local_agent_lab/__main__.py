import argparse
import json
import sys
from pathlib import Path

from .runtime import LocalRuntime, RuntimeFailure, smoke


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="로컬 추론 서버 연결 및 도구 호출 검증"
    )
    parser.add_argument("command", choices=["probe", "smoke"])
    parser.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--model", help="/models에 나타나는 실제 모델 ID")
    parser.add_argument("--api", choices=["chat", "responses"], default="chat")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--output", type=Path, help="검사 결과를 저장할 JSON 경로")
    args = parser.parse_args()
    if args.command == "smoke" and not args.model:
        parser.error("smoke에는 --model이 필요합니다.")
    try:
        runtime = LocalRuntime(args.base_url, args.timeout)
        if args.command == "probe":
            result = {
                "status": "reachable",
                "base_url": runtime.base_url,
                "models": runtime.models(),
            }
        else:
            result = smoke(runtime, args.model, args.api)
        code = 0
    except RuntimeFailure as exc:
        result = {"status": "failed", "error": str(exc)}
        code = 1
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
