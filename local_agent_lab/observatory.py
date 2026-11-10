"""화면에 표시할 등록 도구·하네스의 실제 구현. 임의 파일 열기는 제공하지 않는다."""

import inspect
import importlib
from pathlib import Path

from . import harness
from .trace import ROOT, code_source
from .inference import preview_input, generate, audit_response


def implementation(function):
    cached = code_source(function.__code__)
    if cached:
        return {**cached, "file": cached["file"].replace("\\", "/"), "line": None}
    lines, start = inspect.getsourcelines(function)
    return {
        "file": Path(inspect.getsourcefile(function)).relative_to(ROOT).as_posix(),
        "function": function.__name__,
        "qualified_name": function.__qualname__,
        "start_line": start,
        "line": None,
        "code": "".join(lines),
    }


def tool_branch(name, module=None):
    """등록된 도구의 실제 구현 함수."""
    return implementation((module or harness).IMPLEMENTATIONS[name])


def catalog(active=None, run=None):
    from .learning import EXPLANATIONS

    prefix = (
        type(active).__module__.rsplit(".", 1)[0]
        if active is not None
        else "local_agent_lab"
    )
    harness = importlib.import_module(prefix + ".harness")
    loop = importlib.import_module(prefix + ".agent_loop")
    policy = importlib.import_module(prefix + ".policy")
    inference = importlib.import_module(prefix + ".inference")
    context = importlib.import_module(prefix + ".context")
    compaction = importlib.import_module(prefix + ".compaction")
    checkpoint = importlib.import_module(prefix + ".checkpoint")
    from . import integrations, orchestration, context_memory

    definitions = [
        ("loop", "실행 루프", loop.execute, None),
        ("settings", "설정 계층", context.resolve_settings, None),
        ("rules", "규칙 로더", harness.discover_rules, "rules"),
        ("memory", "이력 복원", harness.Harness.load_history, "memory"),
        ("compaction", "이전 대화 요약", context_memory.prepare, "compaction"),
        ("turn_compaction", "실행 중 압축", compaction.prepare_input, "turn_compaction"),
        ("checkpoint", "실행 재개", checkpoint.save_checkpoint, None),
        ("skills", "스킬 검색", harness.discover_skills, "skills"),
        ("commands", "커맨드 확장", harness.expand_command, "commands"),
        ("context", "입력 조립", harness.build_context, None),
        ("files", "파일 I/O", harness.read_traced, None),
        ("tools", "도구 검사·실행", harness.execute_tool, "tools"),
        ("gate", "검증 게이트", policy.final_gate, None),
        ("tokenizer", "템플릿·토큰", inference.preview_input, None),
        ("model", "모델 호출", inference.generate, None),
        ("kv", "KV 캐시", inference.audit_response, None),
        ("mcp", "MCP 연결", integrations.prepare_mcp, "mcp"),
        ("lsp", "LSP 연결", integrations.prepare_lsp, "lsp"),
        ("search", "문서 검색", harness.IMPLEMENTATIONS["search_text"], None),
        ("planning", "작업 계획", harness.IMPLEMENTATIONS["set_plan"], "planning"),
        ("agents", "에이전트 모드 · 위임", orchestration.prepare, None),
    ]
    labels = {
        "list_files": "파일 목록",
        "read_file": "파일 읽기",
        "load_skill": "스킬 본문",
        "write_file": "파일 쓰기",
        "check_file": "구문 검사",
        "add_numbers": "정수 덧셈",
        "search_text": "문자열 검색",
        "web_search": "인터넷 검색",
        "set_plan": "계획 등록",
        "complete_plan_step": "계획 상태 표시",
    }
    return {
        "components": [
            {
                "id": key,
                "label": label,
                "feature": feature,
                "status": "implemented",
                "source": implementation(function),
                **EXPLANATIONS.get(key, {}),
            }
            for key, label, function, feature in definitions
        ],
        "tools": [
            {
                "name": tool["function"]["name"],
                "label": labels.get(tool["function"]["name"], tool["function"]["name"]),
                "schema": tool,
                "source": tool_branch(tool["function"]["name"], harness),
            }
            for tool in harness.TOOLS
        ]
        + [
            {
                "name": tool["function"]["name"],
                "label": "MCP 외부 도구"
                if tool["function"]["name"].startswith("mcp_")
                else "하위 에이전트 위임"
                if tool["function"]["name"] == "delegate_tasks"
                else "언어 서버 도구",
                "schema": tool,
                "source": implementation(
                    integrations._mcp_tool
                    if tool["function"]["name"].startswith("mcp_")
                    else orchestration.delegate_tasks
                    if tool["function"]["name"] == "delegate_tasks"
                    else integrations._lsp_tool
                ),
            }
            for tool in (run.extra_tools if run else [])
        ],
        "note": "실행 중 강조는 기록된 관찰 지점입니다. 줄마다 중단하는 디버거는 아닙니다. 구현 둘러보기는 현재 소스이며 실행 기록의 소스 스냅샷과 구분합니다.",
    }
