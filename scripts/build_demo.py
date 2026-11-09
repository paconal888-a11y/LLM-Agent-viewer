"""GitHub Pages용 정적 데모를 만든다.

저장된 실제 기록을 재생하는 샘플 보기 모드와 같은 응답을 미리 JSON으로 만들어 둔다.
모델을 호출하지 않고, 실행·저장·내보내기는 제공하지 않는다.
"""

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from local_agent_lab.diagnosis import diagnose  # noqa: E402
from local_agent_lab.experiments import CASES  # noqa: E402
from local_agent_lab.feedback import report  # noqa: E402
from local_agent_lab.harness import Harness  # noqa: E402
from local_agent_lab.observatory import catalog  # noqa: E402
from local_agent_lab.project import current_manifest  # noqa: E402
from local_agent_lab.web import Application  # noqa: E402

ASSETS = (
    "workbench.js",
    "architecture.js",
    "inspection.js",
    "lessons.js",
    "story.js",
    "diagram.css",
    "workbench.css",
    "favicon.svg",
)
REPOSITORY = "https://github.com/paconal888-a11y/LLM-Agent-viewer"
PAGE_URL = "https://paconal888-a11y.github.io/LLM-Agent-viewer/"
TITLE = "Agent Lens · AI 에이전트의 동작을 기록으로 배웁니다"
SUMMARY = "프롬프트 한 줄이 규칙 읽기, 입력 조립, 모델 호출, 도구 실행, 완료 판정으로 이어지는 과정을 실제 기록으로 따라갑니다. 설치 없이 볼 수 있습니다."


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def page(source):
    """절대 경로를 상대 경로로 바꾸고 fetch 대체 스크립트를 가장 먼저 불러온다."""
    html = re.sub(r'(href|src)="/(?!/)', r'\1="', source)
    html, replaced = re.subn(r'<title>[^<]*</title>', f"<title>{TITLE}</title>", html)
    assert replaced == 1
    meta = (
        f'<meta name="description" content="{SUMMARY}" />\n'
        f'    <meta property="og:title" content="{TITLE}" />\n'
        f'    <meta property="og:description" content="{SUMMARY}" />\n'
        '    <meta property="og:type" content="website" />\n'
        f'    <meta property="og:url" content="{PAGE_URL}" />\n'
        f'    <meta property="og:image" content="{PAGE_URL}images/scenes.png" />\n'
        '    <meta name="twitter:card" content="summary_large_image" />\n'
        '    <script src="demo.js"></script>\n    '
    )
    html, replaced = re.subn(r'<link rel="icon"', meta + '<link rel="icon"', html, count=1)
    assert replaced == 1
    link = (
        f'<a class="quiet demolink" href="{REPOSITORY}" target="_blank" rel="noopener">Windows에 설치해 실행 ↗</a>\n      '
        '<button id="help"'
    )
    html, replaced = re.subn(r'<button id="help"', link, html, count=1)
    assert replaced == 1
    # 상태를 읽기 전에도 데모 화면으로 시작한다.
    html, replaced = re.subn(r"<body>", '<body class="demo">', html, count=1)
    assert replaced == 1
    return html


def build(output):
    output = Path(output)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as state:
        app = Application(Harness(state_dir=Path(state)), samples_only=True)
        data = output / "data"
        status = {
            key: value
            for key, value in app.health().items()
            if key not in {"project_root", "working_directory", "instance", "pid", "started_at", "workspace"}
        }
        write_json(data / "status.json", {**status, "demo": True, "repository": REPOSITORY})
        index = json.loads((ROOT / "samples" / "index.json").read_text(encoding="utf-8"))
        samples = [{**item, "id": "sample-" + item["slug"]} for item in index]
        write_json(data / "samples.json", samples)
        write_json(data / "implementation.json", catalog(app.harness, None))
        write_json(data / "cases.json", CASES)
        write_json(data / "manifest.json", current_manifest(ROOT, workspace=app.harness.workspace))
        files = app.project.list()
        write_json(
            data / "project.json",
            {"files": [{**item, "editable": False, "apply": "read_only"} for item in files]},
        )
        numbers = {}
        for number, item in enumerate(files):
            numbers[item["path"]] = number
            content = app.project.read(item["path"])
            write_json(data / "files" / f"{number}.json", {**content, "editable": False})
        write_json(data / "files" / "index.json", numbers)
        for sample in samples:
            run = app.get_run(sample["id"], 0, compact=True)
            write_json(data / "runs" / f"{sample['id']}.json", run)
            write_json(data / "diagnosis" / f"{sample['id']}.json", diagnose(run))
            write_json(data / "feedback" / f"{sample['id']}.json", report(run))
    for name in ASSETS:
        shutil.copy2(ROOT / "web" / name, output / name)
    shutil.copy2(ROOT / "scripts" / "demo_fetch.js", output / "demo.js")
    source = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    (output / "index.html").write_text(page(source), encoding="utf-8")
    (output / "images").mkdir()
    shutil.copy2(ROOT / "docs" / "images" / "scenes.png", output / "images" / "scenes.png")
    (output / ".nojekyll").write_text("", encoding="utf-8")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    print("데모를 만들었습니다:", build(parser.parse_args().output))


if __name__ == "__main__":
    main()
