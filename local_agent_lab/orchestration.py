"""실제 단일·집중·위임 실행. 공유 가중치와 독립 messages를 구분한다."""

from __future__ import annotations

import functools
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

MODES = {
    "single": "한 에이전트",
    "plan": "계획만 · 파일 수정 금지",
    "deep": "한 에이전트 · 집중 점검",
    "team": "주 에이전트 + 하위 에이전트",
}
READ_TOOLS = {
    "list_files",
    "read_file",
    "search_text",
    "check_file",
    "load_skill",
    "add_numbers",
}


def normalize_mode(value="single"):
    if not isinstance(value, str) or value not in MODES:
        raise ValueError("지원하는 에이전트 모드: single, plan, deep, team")
    return value


def bounded(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label}: {low}~{high} 정수로 지정하세요.")
    return value


def prepare(run, harness):
    from .files import read_traced

    config = {}
    if run.mode in {"deep", "team"}:
        config = json.loads(
            read_traced(
                run,
                harness.workspace / "agents.json",
                "context",
                reason="선택한 모드의 예산·역할·권한을 실제 파일에서 읽습니다.",
            )
        )
    if run.mode == "plan":
        run.features["writes"] = False
        run.features["planning"] = run.features["tools"]
        run.role_instructions += "\nYou are in plan-only mode. Inspect available evidence and provide an actionable plan. Do not modify files or claim implementation. Use planning tools when available.\n"
    if run.mode == "deep":
        profile = config["deep"]
        run.max_model_calls = bounded(
            profile["max_model_calls"], 2, 16, "집중 모드 호출 예산"
        )
        run.review_instruction = profile["review_instruction"]
        if (
            not isinstance(run.review_instruction, str)
            or not 1 <= len(run.review_instruction) <= 2000
        ):
            raise ValueError("점검 지침은 1~2000자입니다.")
        run.role_instructions += "\nWork through this request carefully using tools. Before finalizing, the harness will request one additional review against actual evidence.\n"
    if run.mode == "team":
        if not run.features["tools"]:
            raise ValueError("하위 에이전트 모드는 도구 연결이 필요합니다.")
        team = config["team"]
        bounded(team["max_workers"], 1, 3, "동시 작업자 수")
        bounded(team["child_max_model_calls"], 1, 8, "하위 호출 예산")
        profiles = team["profiles"]
        if not isinstance(profiles, dict) or not 1 <= len(profiles) <= 4:
            raise ValueError("하위 역할은 1~4개입니다.")
        for key, profile in profiles.items():
            if not isinstance(key, str) or not key.isidentifier() or len(key) > 30:
                raise ValueError("역할 ID는 짧은 식별자로 작성하세요.")
            for field in ("label", "description", "instruction"):
                if (
                    not isinstance(profile.get(field), str)
                    or not 1 <= len(profile[field]) <= 2000
                ):
                    raise ValueError(
                        "역할의 label·description·instruction을 확인하세요."
                    )
            if (
                not isinstance(profile.get("tools"), list)
                or set(profile["tools"]) - READ_TOOLS
            ):
                raise ValueError(
                    "하위 에이전트는 지정한 읽기·검색·검사·계산 도구만 지원합니다."
                )
        public = [
            {
                "id": key,
                "label": p["label"],
                "description": p["description"],
                "tools": p["tools"],
            }
            for key, p in profiles.items()
        ]
        run.extra_tools.append(
            {
                "type": "function",
                "function": {
                    "name": "delegate_tasks",
                    "description": "Delegate independent tasks to real subagents. tasks_json is a JSON string array of 1 to 3 objects with profile and task. Profiles: "
                    + json.dumps(public, ensure_ascii=False),
                    "parameters": {
                        "type": "object",
                        "properties": {"tasks_json": {"type": "string"}},
                        "required": ["tasks_json"],
                        "additionalProperties": False,
                    },
                },
            }
        )
        run.tool_handlers["delegate_tasks"] = functools.partial(
            delegate_tasks, harness=harness, config=team
        )
        run.role_instructions += '\nYou are the main agent. Call delegate_tasks once to give independent, clearly scoped tasks to subagents using the listed profiles. Read their actual results before synthesizing your answer. tasks_json must be a JSON string array such as [{"profile":"explore","task":"read the requested document"},{"profile":"oracle","task":"check the requested document"}].\n'
        if not any(item["tool"] == "delegate_tasks" for item in run.requirements):
            run.requirements.append({"tool": "delegate_tasks", "arguments": {}})
    run.emit(
        "agent.mode",
        "에이전트 모드와 실행 예산 적용",
        "집중 모드는 같은 에이전트의 예산과 점검을 바꿉니다. 위임 모드는 별도 messages와 도구 권한을 가진 하위 실행을 만듭니다. 모델 가중치는 공유하고 신경망의 reasoning effort를 바꾸지는 않습니다.",
        category="context",
        output={
            "mode": run.mode,
            "label": MODES[run.mode],
            "max_model_calls": run.max_model_calls,
            "additional_review": run.mode == "deep",
            "parent_run_id": run.parent_run.id if run.parent_run else None,
            "allowed_tools": sorted(run.allowed_tool_names)
            if run.allowed_tool_names is not None
            else None,
            "role_instruction": run.role_instructions,
        },
    )


@contextmanager
def inference_turn(harness, run, iteration):
    from .trace import RunStopped

    run.emit(
        "agent.queue",
        "모델 계산 차례 대기",
        "작업 스레드는 함께 진행할 수 있지만 공유 실행기를 보호하기 위해 실제 모델 계산은 한 번에 하나씩 수행합니다. 외부 서버 자체의 병렬 처리 능력을 측정한 값은 아닙니다.",
        category="model",
        output={"call": iteration, "scheduler": "shared_runtime_lock"},
    )
    started = time.perf_counter()
    while not harness.inference_lock.acquire(timeout=0.1):
        if run.stopped:
            raise RunStopped()
    try:
        if run.stopped:
            raise RunStopped()
        run.emit(
            "agent.compute",
            "모델 계산 차례 획득",
            "queue 이벤트의 관찰 정지 이후부터 lock 획득까지의 벽시간입니다. 다른 에이전트가 lock을 가진 채 관찰 중인 시간은 포함될 수 있습니다.",
            category="model",
            output={
                "call": iteration,
                "wait_ms": round((time.perf_counter() - started) * 1000, 3),
            },
        )
        yield
    finally:
        harness.inference_lock.release()


def delegate_tasks(ctx, tasks_json, harness, config):
    from .trace import Run

    if getattr(ctx.run, "delegation_used", False):
        raise ValueError("학습용 위임은 한 실행에서 한 번만 가능합니다.")
    if len(tasks_json) > 8000:
        raise ValueError("위임 요청이 너무 큽니다.")
    tasks = json.loads(tasks_json)
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 3:
        raise ValueError("하위 작업은 1~3개 목록입니다.")
    for item in tasks:
        if (
            not isinstance(item, dict)
            or set(item) != {"profile", "task"}
            or item["profile"] not in config["profiles"]
            or not isinstance(item["task"], str)
            or not 1 <= len(item["task"]) <= 1500
        ):
            raise ValueError(
                "각 하위 작업에는 등록한 profile과 1~1500자 task를 지정하세요."
            )
    ctx.run.delegation_used = True
    ctx.run.emit(
        "agent.delegate",
        "주 에이전트가 하위 작업을 전달",
        "모델이 생성한 작업을 검사한 뒤 독립 대화로 전달합니다. 주 대화 전체·세션 기억·MCP/LSP·쓰기 권한은 자동 상속하지 않습니다.",
        category="tools",
        parent=ctx.parent,
        input={"tasks": tasks},
        output={
            "max_workers": config["max_workers"],
            "shared_model": harness.model,
            "shared_workspace": str(harness.workspace),
            "shared_weights": True,
            "isolated_messages": True,
        },
    )
    children = []
    for index, task in enumerate(tasks):
        profile = config["profiles"][task["profile"]]
        prompt = (
            "[원래 사용자 요청: 참고 자료]\n"
            + ctx.run.effective_prompt
            + "\n\n[이 하위 에이전트가 수행할 작업]\n"
            + task["task"]
        )
        child = Run(
            prompt,
            "child-" + ctx.run.id + "-" + str(index),
            features={
                **ctx.run.features,
                "tools": bool(profile["tools"]),
                "skills": ctx.run.features["skills"]
                and "load_skill" in profile["tools"],
                "memory": False,
                "writes": False,
                "mcp": False,
                "lsp": False,
                "commands": False,
                "planning": False,
            },
            engine={**ctx.run.engine, "cache_prompt": False},
            parent_run=ctx.run,
            agent_name=profile["label"],
        )
        child.allowed_tool_names = set(profile["tools"])
        child.role_instructions = (
            "\n"
            + profile["instruction"]
            + "\nYou are a read-only subagent. Return concise evidence to the main agent, with paths where relevant.\n"
        )
        child.max_model_calls = config["child_max_model_calls"]
        child.implementation = ctx.run.implementation
        ctx.run.children.append(child)
        children.append(child)
        ctx.run.emit(
            "agent.spawn",
            profile["label"] + " · 하위 실행 생성",
            "선택한 역할 지침·작업·도구 목록을 별도 Run에 설정했습니다. 같은 가중치를 쓰더라도 같은 대화나 KV를 공유한다는 뜻은 아닙니다.",
            category="tools",
            parent=ctx.parent,
            output={
                "child_id": child.id,
                "agent_name": child.agent_name,
                "prompt": prompt,
                "profile": profile,
                "features": child.features,
                "engine": child.engine,
                "max_model_calls": child.max_model_calls,
            },
        )

    def work(child):
        worker = type(harness)(
            workspace=harness.workspace,
            state_dir=harness.state_dir,
            model=harness.model,
            runtime=harness.runtime,
        )
        worker.inference_lock = harness.inference_lock
        worker.execute(child)
        return {
            "run_id": child.id,
            "agent_name": child.agent_name,
            "status": child.status,
            "answer": child.answer[:2400],
            "answer_truncated": len(child.answer) > 2400,
            "error": child.error,
            "model_calls": child.model_calls,
            "tool_calls": child.tool_calls,
            "usage": child.usage,
        }

    with ThreadPoolExecutor(
        max_workers=config["max_workers"], thread_name_prefix="agent-lens-child"
    ) as pool:
        results = list(pool.map(work, children))
    ctx.run.emit(
        "agent.join",
        "하위 결과를 주 에이전트로 수집",
        "실제 하위 실행이 끝난 결과입니다. 다음 tool 메시지에 전달하며 원본 하위 trace는 별도로 보존합니다. 실패한 작업을 성공한 것처럼 대체하지 않습니다.",
        category="tools",
        parent=ctx.parent,
        output={
            "results": results,
            "return_policy": "answer_first_2400_chars_and_status; full trace retained",
        },
    )
    return ctx.finish(
        {
            "results": results,
            "all_completed": all(r["status"] == "completed" for r in results),
            "protocol": "subagents",
        }
    )
