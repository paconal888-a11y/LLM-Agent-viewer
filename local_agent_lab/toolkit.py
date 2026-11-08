"""등록 도구와 구현. 도구를 추가할 때 schema와 IMPLEMENTATIONS를 함께 수정한다."""

import ast
import json
from dataclasses import dataclass
from pathlib import Path

from .files import read_traced, workspace_path


def function_tool(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


STRING = {"type": "string"}
TOOLS = [
    function_tool("list_files", "List files in the workspace.", {}, []),
    function_tool(
        "read_file",
        "Read the actual UTF-8 contents of a workspace file.",
        {"path": STRING},
        ["path"],
    ),
    function_tool(
        "load_skill",
        "Load complete skill instructions before a matching task.",
        {"name": STRING},
        ["name"],
    ),
    function_tool(
        "write_file",
        "Create or replace a workspace text file.",
        {"path": STRING, "content": STRING},
        ["path", "content"],
    ),
    function_tool(
        "check_file",
        "Check Python syntax, JSON syntax, or non-empty text. Does not run a program.",
        {"path": STRING},
        ["path"],
    ),
    function_tool(
        "add_numbers",
        "Return the sum of two integers.",
        {"a": {"type": "integer"}, "b": {"type": "integer"}},
        ["a", "b"],
    ),
    function_tool(
        "search_text",
        "Search literal text in workspace text files. Returns actual paths and line numbers.",
        {"query": STRING, "path": STRING},
        ["query"],
    ),
    function_tool(
        "set_plan",
        "Record an explicit short plan. steps is newline-separated. This is an observable plan, not hidden reasoning.",
        {"steps": STRING},
        ["steps"],
    ),
    function_tool(
        "complete_plan_step",
        "Mark a plan item complete (one-based index). This marks plan state, not verified work.",
        {"index": {"type": "integer"}},
        ["index"],
    ),
]


@dataclass
class ToolContext:
    run: object
    workspace: Path
    catalog: list
    active_skills: set
    parent: int
    name: str

    def read(self, path, category="tools", required=True):
        return read_traced(
            self.run,
            workspace_path(self.workspace, path),
            category,
            reason=f"{self.name} 도구가 파일을 읽습니다.",
            parent=self.parent,
            required=required,
        )

    def finish(self, result):
        self.run.emit(
            "tool.result",
            self.name + " 실행 결과",
            "도구 함수가 실제로 반환한 값입니다.",
            category="tools",
            actor="tool",
            parent=self.parent,
            output=result,
        )
        return result


def list_files(ctx):
    files = [
        path.relative_to(ctx.workspace).as_posix()
        for path in ctx.workspace.rglob("*")
        if path.is_file() and path.resolve().is_relative_to(ctx.workspace.resolve())
    ]
    return ctx.finish({"files": sorted(files)[:200]})


def read_file(ctx, path):
    canonical = (
        workspace_path(ctx.workspace, path).relative_to(ctx.workspace).as_posix()
    )
    return ctx.finish({"path": canonical, "content": ctx.read(path)})


def load_skill(ctx, name):
    selected = next((skill for skill in ctx.catalog if skill["name"] == name), None)
    if selected is None:
        raise ValueError("목록에 없는 스킬입니다.")
    instructions = ctx.read(selected["path"], category="skills")
    ctx.active_skills.add(name)
    return ctx.finish({"name": name, "instructions": instructions})


def write_file(ctx, path, content):
    if len(content.encode("utf-8")) > 65536:
        raise ValueError("파일 쓰기는 64 KiB까지 허용합니다.")
    target = workspace_path(ctx.workspace, path)
    before = ctx.read(path, required=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="")
    return ctx.finish(
        {
            "path": target.relative_to(ctx.workspace).as_posix(),
            "written_bytes": len(content.encode("utf-8")),
            "before": before,
            "after": content,
            "requires_check": True,
        }
    )


def check_file(ctx, path):
    text = ctx.read(path)
    canonical = (
        workspace_path(ctx.workspace, path).relative_to(ctx.workspace).as_posix()
    )
    parsed = None
    try:
        if Path(path).suffix == ".py":
            compile(ast.parse(text, filename=path), path, "exec")
            checker = "ast.parse + compile (Python 구문 확인; 프로그램 실행 없음)"
        elif Path(path).suffix == ".json":
            parsed = json.loads(text)
            checker = "json.loads (JSON 구문 분석)"
        else:
            if not text.strip():
                raise ValueError("파일 내용이 비어 있습니다.")
            checker = "non-empty text"
        result = {"path": canonical, "passed": True, "checker": checker}
        if Path(path).suffix == ".json":
            result["json_value"] = parsed
    except (SyntaxError, ValueError) as error:
        result = {"path": canonical, "passed": False, "error": str(error)}
    return ctx.finish(result)


def add_numbers(ctx, a, b):
    return ctx.finish({"a": a, "b": b, "result": a + b})


def search_text(ctx, query, path="."):
    if not query or len(query) > 200:
        raise ValueError("검색어를 1~200자로 입력하세요.")
    target = workspace_path(ctx.workspace, path)
    if not target.exists():
        raise ValueError("검색할 파일이나 폴더가 없습니다.")
    candidates = [target] if target.is_file() else sorted(target.rglob("*"))
    matches = []
    scanned = 0
    total = 0
    limited = False
    for file in candidates:
        if (
            not file.is_file()
            or not file.resolve().is_relative_to(ctx.workspace)
            or file.suffix not in {".md", ".txt", ".py", ".json"}
            or file.stat().st_size > 65536
        ):
            continue
        if scanned >= 50 or total + file.stat().st_size > 524288:
            limited = True
            break
        relative = file.relative_to(ctx.workspace).as_posix()
        text = ctx.read(relative)
        scanned += 1
        total += file.stat().st_size
        for number, line in enumerate(text.splitlines(), 1):
            if query.casefold() in line.casefold():
                matches.append({"path": relative, "line": number, "text": line[:500]})
            if len(matches) >= 30:
                limited = True
                break
        if limited:
            break
    return ctx.finish(
        {
            "query": query,
            "matches": matches,
            "files_scanned": scanned,
            "scanned_bytes": total,
            "limited": limited,
        }
    )


def set_plan(ctx, steps):
    lines = [line.strip() for line in steps.splitlines() if line.strip()]
    if not 1 <= len(lines) <= 12 or any(len(line) > 300 for line in lines):
        raise ValueError("계획은 1~12줄, 한 줄 300자 이내로 작성하세요.")
    ctx.run.plan = [{"text": line, "status": "pending"} for line in lines]
    return ctx.finish({"plan": ctx.run.plan, "verified_work": False})


def complete_plan_step(ctx, index):
    if not 1 <= index <= len(ctx.run.plan):
        raise ValueError("등록한 계획 항목의 번호를 사용하세요.")
    ctx.run.plan[index - 1]["status"] = "done"
    return ctx.finish({"plan": ctx.run.plan, "verified_work": False})


IMPLEMENTATIONS = {
    function.__name__: function
    for function in (
        list_files,
        read_file,
        load_skill,
        write_file,
        check_file,
        add_numbers,
        search_text,
        set_plan,
        complete_plan_step,
    )
}
