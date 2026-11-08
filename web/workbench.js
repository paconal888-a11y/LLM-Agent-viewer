/* Agent Lens: 실제 관찰 기록을 표시한다. 재생은 기록 탐색이며 모델 재실행이 아니다. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id),
    esc = (value) =>
      String(value ?? "").replace(
        /[&<>"']/g,
        (c) =>
          ({
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;",
          })[c],
      );
  const pretty = (value) =>
      typeof value === "string"
        ? value
        : JSON.stringify(value ?? null, null, 2),
    num = (value) =>
      typeof value === "number"
        ? value.toLocaleString(undefined, { maximumFractionDigits: 2 })
        : "—";
  const state = {
    run: null,
    events: [],
    sources: {},
    cursor: -1,
    live: true,
    tab: "change",
    frame: -1,
    view: "journey",
    files: [],
    catalog: { components: [], tools: [] },
    file: null,
    drafts: new Map(),
    edits: new Map(),
    session: "session-" + crypto.randomUUID().slice(0, 12),
    timer: null,
    playing: null,
    health: {},
    experiment: null,
    inputStart: null,
    inputChanges: 0,
    agentFilter: "all",
    activity: { run: null, experiment: null },
    ready: false,
    inputMessage: -1,
  };
  const features = {
      rules: true,
      memory: true,
      compaction: false,
      skills: true,
      tools: true,
      commands: true,
      writes: false,
      mcp: false,
      lsp: false,
      feedback_recall: false,
      planning: false,
    },
    labels = {
      rules: "AGENTS.md",
      memory: "대화 이력",
      compaction: "이전 대화 요약",
      skills: "스킬",
      tools: "도구",
      commands: "/명령어",
      writes: "파일 쓰기 허용",
      mcp: "MCP 도구 연결",
      lsp: "LSP 연결",
      feedback_recall: "동일 요청 지적 재포함",
      planning: "작업 계획 도구",
    };
  const examples = {
    team: "delegate_tasks 도구를 사용해 두 작업을 나눠줘. explore는 notes/agent-guide.md를 읽고 하네스의 역할을 정리하고, oracle은 같은 파일을 읽고 모델의 말과 도구의 실제 행동을 왜 구분해야 하는지 검토해줘. 두 보고를 받은 뒤 핵심을 합쳐 알려줘.",
    tool: "add_numbers 도구를 사용해 137과 248을 더하고 결과를 알려줘.",
    rules: "이 프로젝트의 답변 규칙에 따라 한 문장으로 인사해줘.",
    skill:
      "file-summary 스킬을 사용해서 notes/agent-guide.md를 읽고 핵심 내용을 세 가지로 요약해줘.",
    command: "/summary notes/agent-guide.md",
    write:
      "data/learning-demo.json을 읽고 practice 값을 true로 바꿔 저장한 뒤 check_file로 검사해줘.",
    mcp: "mcp_math_add 도구로 137과 248을 더하고 결과를 알려줘.",
    lsp: "lsp_diagnostics 도구로 examples/lsp_demo.py의 실제 언어 서버 진단을 확인하고 문제가 있는 줄과 이유를 알려줘.",
    plan: "먼저 set_plan 도구로 '하네스 검색' 한 단계 계획을 등록해줘. search_text 도구로 notes 폴더에서 하네스를 찾고, complete_plan_step 도구로 1번 항목을 완료 표시해줘. 검색 결과를 알려줘.",
  };
  const exampleRequirements = {
    team: [{ tool: "delegate_tasks" }],
    plan: [
      { tool: "set_plan" },
      { tool: "search_text", arguments: { query: "하네스", path: "notes" } },
      { tool: "complete_plan_step", arguments: { index: 1 } },
    ],
    mcp: [
      {
        tool: "mcp_math_add",
        arguments: { a: 137, b: 248 },
        result: { data: { result: 385 } },
      },
    ],
    lsp: [
      { tool: "lsp_diagnostics", arguments: { path: "examples/lsp_demo.py" } },
    ],
    tool: [{ tool: "add_numbers", arguments: { a: 137, b: 248 } }],
    rules: [],
    skill: [
      { tool: "load_skill", arguments: { name: "file-summary" } },
      { tool: "read_file", arguments: { path: "notes/agent-guide.md" } },
    ],
    command: [
      { tool: "read_file", arguments: { path: "notes/agent-guide.md" } },
    ],
    write: [
      { tool: "read_file", arguments: { path: "data/learning-demo.json" } },
      { tool: "write_file", arguments: { path: "data/learning-demo.json" } },
      {
        tool: "check_file",
        arguments: { path: "data/learning-demo.json" },
        result: { json_value: { practice: true } },
      },
    ],
  };
  const stages = [
    ["input", "입력", "01"],
    ["read", "파일 읽기", "02"],
    ["context", "입력 조립", "03"],
    ["token", "토큰화", "04"],
    ["model", "모델 계산", "05"],
    ["tool", "도구 · 왕복", "06"],
    ["output", "검사 · 완료", "07"],
  ];
  const actors = {
    user: ["U", "사용자"],
    browser: ["B", "브라우저"],
    harness: ["H", "하네스"],
    runtime: ["R", "실행기"],
    llm: ["M", "모델"],
    tool: ["T", "도구"],
    filesystem: ["F", "파일시스템"],
    validator: ["V", "검증 코드"],
    mcp: ["C", "MCP 클라이언트"],
    lsp: ["L", "언어 서버 클라이언트"],
  };
  async function api(path, body) {
    const r = await fetch(
      path,
      body === undefined
        ? {}
        : {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          },
    );
    const data = await r.json();
    if (!r.ok) {
      if (data.activity) applyActivity(data.activity);
      throw Error(data.error || "요청 실패");
    }
    return data;
  }
  function toast(message) {
    $("toast").textContent = message;
    $("toast").classList.remove("hidden");
    clearTimeout(state.toast);
    state.toast = setTimeout(() => $("toast").classList.add("hidden"), 4500);
  }
  function action(fn) {
    return async (event) => {
      try {
        await fn(event);
      } catch (error) {
        toast(error.message);
      }
    };
  }
  function busy() {
    return (
      !!state.activity.run ||
      !!state.activity.experiment ||
      (state.run &&
        ["running", "paused", "stopping"].includes(state.run.status)) ||
      !!state.experiment
    );
  }
  function store(key, value) {
    try {
      localStorage.setItem("agent-lens." + key, JSON.stringify(value));
    } catch {}
  }
  function stored(key) {
    try {
      return JSON.parse(localStorage.getItem("agent-lens." + key) || "null");
    } catch {
      return null;
    }
  }
  function saveDraft() {
    if (!state.ready) return;
    store("draft", {
      prompt: $("prompt").value,
      session: state.session,
      mode: $("agentMode").value,
      features: { ...features },
      requirements: $("requirements").value,
      step: $("stepMode").checked,
    });
  }
  function restoreDraft() {
    const draft = stored("draft");
    if (!draft) return;
    if (typeof draft.prompt === "string") $("prompt").value = draft.prompt;
    if (/^[a-zA-Z0-9_-]{1,64}$/.test(draft.session))
      state.session = draft.session;
    if (["single", "plan", "deep", "team"].includes(draft.mode))
      $("agentMode").value = draft.mode;
    if (draft.features)
      Object.keys(features).forEach((k) => {
        if (typeof draft.features[k] === "boolean")
          features[k] = draft.features[k];
      });
    if (typeof draft.requirements === "string")
      $("requirements").value = draft.requirements;
    $("stepMode").checked = draft.step === true;
  }
  function applyActivity(activity) {
    state.activity = activity;
    const run = activity.run,
      experiment = activity.experiment;
    $("activityBar").classList.toggle("hidden", !run && !experiment);
    document.body.classList.toggle("has-activity", !!run || !!experiment);
    if (run || experiment)
      $("activityBar").innerHTML =
        `<span class="activity-dot"></span><strong>${experiment ? "비교" + " " + esc(experiment.phase) : run.status === "paused" ? "단계 대기" : run.status === "stopping" ? "중지 중" : "실행 중"}</strong><span class="activity-prompt">${esc(run?.prompt || "구성 비교")}</span><button data-activity="open">실행 열기</button>${run?.status === "paused" && !experiment ? '<button data-activity="next">다음 단계</button><button data-activity="continue">계속</button>' : ""}<button data-activity="stop" class="danger" ${run?.status === "stopping" ? "disabled" : ""}>중지</button>`;
    $("run").disabled = busy() || !state.health.connected;
    $("startCompare").disabled = busy() || !state.health.connected;
    $("backend").disabled = busy() || state.health.mode === "sample";
  }
  async function refreshActivity({ recover = false } = {}) {
    const activity = await api("/api/activity");
    applyActivity(activity);
    if (recover && (activity.run || activity.experiment)) {
      if (activity.run) {
        state.session = activity.run.session_id;
        await loadRun(activity.run.id);
        state.live = true;
        select(state.events.length - 1);
      }
      if (activity.experiment) {
        state.experiment = activity.experiment.id;
        pollCompare().catch((e) => toast(e.message));
      }
    }
    return activity;
  }
  function view(name) {
    state.view = name;
    for (const v of ["journey", "editor", "learn", "feedback", "memory"])
      $(v + "View").classList.toggle("hidden", v !== name);
    $("pageName").textContent = {
      journey: "구조 · 실행",
      editor: "파일 편집",
      learn: "구성 · 비교",
      feedback: "오류 · 기억",
      memory: "대화 저장소",
    }[name];
    document
      .querySelectorAll(".mainnav [data-view]")
      .forEach((b) => b.classList.toggle("active", b.dataset.view === name));
    $("project").classList.remove("open");
    document
      .querySelector(".layout")
      .classList.toggle(
        "reading",
        ["feedback", "learn", "memory"].includes(name),
      );
    document
      .querySelector(".layout")
      .classList.toggle("diagram-layout", name === "journey");
    if (name === "memory") loadMemory().catch((e) => toast(e.message));
    if (name === "feedback") {
      if (
        state.run &&
        [...$("feedbackRun").options].some((o) => o.value === state.run.id)
      )
        $("feedbackRun").value = state.run.id;
      loadFeedback().catch((e) => toast(e.message));
    }
  }
  function featureUI() {
    if (!features.tools) features.skills = false;
    if (!features.tools) features.writes = false;
    if (!features.tools) {
      features.mcp = false;
      features.lsp = false;
      features.planning = false;
    }
    if (!features.memory) {
      features.feedback_recall = false;
      features.compaction = false;
    }
    if (!features.tools) $("requirements").value = "[]";
    $("requirements").disabled = !features.tools;
    $("features").innerHTML = Object.entries(features)
      .map(
        ([key, on]) =>
          `<label class="check"><input type="checkbox" data-feature="${key}" ${on ? "checked" : ""} ${key === "skills" && !features.tools ? "disabled" : ""}>${labels[key]}</label>`,
      )
      .join("");
    $("configSummary").textContent =
      ($("backend").value === "reference" ? "교육용 실행기" : "빠른 실행기") +
      " · " +
      (Object.values(features).some(Boolean) ? "하네스 연결" : "모델만");
    try {
      $("configSummary").textContent +=
        " · 완료 조건 " + JSON.parse($("requirements").value).length + "개";
    } catch {
      $("configSummary").textContent += " · 완료 조건 형식 확인";
    }
    saveDraft();
  }
  function engine() {
    return {
      use_kv: $("useKv").checked,
      cache_prompt: $("prefixCache").checked,
      stream: $("stream").checked,
      max_tokens: Number($("maxTokens").value),
      temperature: Number($("temperature").value),
      capture_layer: Number($("captureLayer").value),
      seed: 42,
      top_k: 40,
      top_p: 0.95,
    };
  }
  function updateBackend() {
    const ref = $("backend").value === "reference";
    $("maxTokens").max = ref ? "256" : "1024";
    if (state.configBackend !== $("backend").value) {
      $("maxTokens").value = ref ? "256" : "768";
      state.configBackend = $("backend").value;
    }
    $("backendNote").textContent = ref
      ? "교육용은 실제 forward·Q/K/V 텐서·attention·샘플링을 기록합니다. 작은 CPU 모델이라 복잡한 요청을 놓칠 수 있습니다."
      : "로컬 llama.cpp 서버에 연결합니다. 실제 HTTP·토큰·캐시 통계를 기록하며 내부 텐서는 제공하지 않습니다. 먼저 -Backend llama로 실행기를 준비하세요.";
    $("useKv").disabled = !ref;
    $("captureLayer").disabled = !ref;
    $("stream").disabled = ref;
    if (!ref) $("useKv").checked = true;
    featureUI();
  }
  async function health() {
    state.health = await api("/api/status");
    $("statusDot").classList.toggle("ready", state.health.connected);
    $("connection").textContent = state.health.loading
      ? "모델을 메모리에 불러오는 중"
      : state.health.connected
        ? state.health.model + " · 로컬 연결"
        : "모델 연결 필요";
    $("connection").title = state.health.error || state.health.base_url || "";
    $("run").disabled = !state.health.connected || busy();
    if (!busy()) {
      $("backend").value = state.health.backend || "llama";
      updateBackend();
    }
    $("startCompare").disabled = !state.health.connected || busy();
    $("backend").disabled = busy() || state.health.mode === "sample";
    if (state.health.mode === "sample") {
      $("connection").textContent = "샘플 기록 · 모델 실행 없음";
      $("backend").disabled = true;
      $("backendNote").textContent =
        "샘플 보기 모드입니다. 실제 실행은 Start-AgentLens.ps1로 일반 모드를 시작하세요.";
    }
  }
  function stage(event) {
    const k = event.kind;
    if (event.category === "integrations") return "tool";
    if (
      event.category === "output" ||
      k.startsWith("validation.final") ||
      k.startsWith("memory.save")
    )
      return "output";
    if (k.startsWith("tool.") || k === "context.append") return "tool";
    if (
      k.startsWith("model.template") ||
      k.startsWith("model.token") ||
      k.startsWith("model.detoken") ||
      k.startsWith("model.preview")
    )
      return "token";
    if (event.category === "model") return "model";
    if (
      k.startsWith("file.") ||
      k.startsWith("rules.") ||
      k.startsWith("skills.")
    )
      return "read";
    if (
      event.category === "input" ||
      k.startsWith("browser.") ||
      k.startsWith("http.body") ||
      k.startsWith("http.json") ||
      k === "harness.modules" ||
      k === "thread.dispatch"
    )
      return "input";
    return "context";
  }
  function pathOf(event) {
    const p =
      event?.output?.path ||
      event?.input?.path ||
      event?.input?.relative_path ||
      event?.output?.relative_path ||
      event?.output?.absolute_path ||
      event?.input?.requested_path;
    if (typeof p !== "string") return null;
    const normalized = p.replaceAll("\\", "/");
    const mark = normalized.indexOf("/demo_workspace/");
    return mark >= 0 ? normalized.slice(mark + 1) : normalized;
  }
  function renderRoute() {
    const past = state.events.slice(0, state.cursor + 1),
      current = state.events[state.cursor];
    $("route").innerHTML = stages
      .map(([key, label, id]) => {
        const count = past.filter((e) => stage(e) === key).length;
        return `<button class="routenode ${count ? "seen" : ""} ${current && stage(current) === key ? "current" : ""}" data-stage="${key}"><b>${id}</b><span>${label}</span><small>${count ? count + "단계" : ""}</small></button>`;
      })
      .join("");
    const visible = ["mcp", "lsp"].filter(
      (key) => state.run?.features?.[key] || past.some((e) => e.actor === key),
    );
    $("connections").classList.toggle("hidden", !visible.length);
    $("connections").innerHTML = visible
      .map((key) => {
        const sent = past.filter((e) => e.kind === key + ".send"),
          received = past.filter((e) => e.kind === key + ".receive"),
          started = past.findLast(
            (e) =>
              e.kind === key + ".process.start" ||
              e.kind === key + ".process.reuse",
          );
        return `<button class="connectioncard ${current?.actor === key ? "active" : ""}" data-extension="${key}"><strong>${key.toUpperCase()} <small>${started ? "프로세스 " + started.output.pid : "연결 설정 · 시작 전"}</small></strong><span>→ 전송 ${sent.length} · ← 수신 ${received.length}</span></button>`;
      })
      .join("");
  }
  function rootAgentId() {
    return state.run?.sample?.source_run_id || state.run?.id;
  }
  function eventRow(e) {
    const actor = actors[e.actor] || actors.harness;
    return `<button class="event ${e.id === state.events[state.cursor]?.id ? "selected" : ""} ${e.id > state.events[state.cursor]?.id ? "past" : ""}" data-event="${e.id}"><span class="actor ${esc(e.actor)}">${actor[0]}</span><span class="eventtext"><span class="eventtitle">${esc(eventName(e))}</span><span class="eventsub">${esc(pathOf(e) || e.source?.function || e.kind)}</span></span><span class="eventid">#${e.id}</span></button>`;
  }
  function chapterGroups(events) {
    let call = 0;
    const calls = new Map();
    const perAgent = new Map();
    for (const item of state.events) {
      const id = item.agent_id || rootAgentId();
      if (item.kind === "model.request")
        perAgent.set(id, (perAgent.get(id) || 0) + 1);
      calls.set(
        item.id,
        item.output?.call ?? item.input?.call ?? perAgent.get(id) ?? 0,
      );
    }
    const groups = [];
    for (const e of events) {
      call = calls.get(e.id) || 0;
      const s = stage(e);
      let key = ["model", "token"].includes(s)
        ? "model-" + call
        : s === "tool" ||
            (e.parent &&
              state.events.find((item) => item.id === e.parent)?.kind ===
                "tool.dispatch")
          ? "tool-" + call
          : !call && ["read", "context"].includes(s)
            ? "prepare"
            : s;
      if (["mcp", "lsp"].includes(e.actor) && !e.parent && !call)
        key = "connect-" + e.actor;
      let group = groups.at(-1);
      key += "-agent-" + (e.agent_id || rootAgentId());
      if (!group || group.key !== key) {
        group = {
          key,
          stage: s,
          call,
          agentId: e.agent_id || rootAgentId(),
          agentName: e.agent_name || "주 에이전트",
          events: [],
        };
        groups.push(group);
      }
      group.events.push(e);
    }
    return groups;
  }
  function modelRows(events) {
    let result = "",
      steps = [],
      frames = [];
    function flushFrames() {
      if (!frames.length) return;
      const found = frames.findIndex(
          (e) => e.id === state.events[state.cursor]?.id,
        ),
        at = Math.max(0, found),
        start = Math.max(0, Math.min(at - 3, frames.length - 7)),
        end = Math.min(frames.length, start + 7);
      result += `<details class="tokenstep" ${found >= 0 ? "open" : ""}><summary>SSE 수신 · 전송 프레임 ${frames.length}개<small>토큰과 구분</small></summary><p class="matrixnote streamnote">원본 ${start + 1}–${end} / ${frames.length}. 재생 막대로 모든 조각을 탐색할 수 있습니다.</p>${start > 0 ? `<button class="quiet" data-event="${frames[Math.max(0, start - 4)].id}">↑ 이전 조각</button>` : ""}${frames.slice(start, end).map(eventRow).join("")}${end < frames.length ? `<button class="quiet" data-event="${frames[end].id}">↓ 다음 조각</button>` : ""}</details>`;
      frames = [];
    }
    function flush() {
      if (!steps.length) return;
      const sample = steps.find((e) => e.kind === "reference.sample"),
        first = steps[0],
        step = first.output?.step ?? first.input?.step ?? 0;
      const active = steps.some((e) => e.id === state.events[state.cursor]?.id);
      result += `<details class="tokenstep" data-step="${first.id}" ${active ? "open" : ""}><summary><span>${step === 0 ? "입력 처리 · 첫 토큰" : "출력 토큰 " + (step + 1)}</span>${sample ? `<code>${esc(piece(sample.output.piece))} <span>· ${sample.output.id}</span></code>` : ""}<small>${steps.length} 단계</small></summary>${steps.map(eventRow).join("")}</details>`;
      steps = [];
    }
    for (const e of events) {
      if (
        [
          "model.stream",
          "model.stream.control",
          "model.stream.usage",
          "runtime.prefill",
        ].includes(e.kind)
      ) {
        flush();
        frames.push(e);
        continue;
      }
      flushFrames();
      if (
        e.kind.startsWith("reference.") &&
        e.kind !== "reference.cache.select" &&
        e.kind !== "reference.protocol"
      ) {
        const step = e.output?.step ?? e.input?.step;
        if (
          steps.length &&
          (steps[0].output?.step ?? steps[0].input?.step) !== step
        )
          flush();
        steps.push(e);
      } else {
        flush();
        result += eventRow(e);
      }
    }
    flush();
    flushFrames();
    return result;
  }
  function renderTimeline() {
    if (
      !state.events.length ||
      !document.querySelector(".execution").classList.contains("records-open")
    )
      return;
    const opened = state.openGroups || new Set();
    const query = $("eventSearch").value.toLowerCase(),
      events = query
        ? state.events.filter((e) =>
            (
              e.title +
              " " +
              e.kind +
              " " +
              (pathOf(e) || "") +
              " " +
              (e.output?.name || "")
            )
              .toLowerCase()
              .includes(query),
          )
        : state.events;
    const groups = chapterGroups(
      events.filter(
        (e) =>
          state.agentFilter === "all" ||
          (e.agent_id || rootAgentId()) === state.agentFilter,
      ),
    );
    $("timeline").innerHTML =
      groups
        .map((g, i) => {
          const active = g.events.some(
              (e) => e.id === state.events[state.cursor]?.id,
            ),
            open = query || active || opened.has(g.key + "-" + i);
          const title = g.key.startsWith("connect-")
            ? g.key.slice(8).split("-agent-")[0].toUpperCase() +
              " 연결 준비 · 기능 검색"
            : g.key.startsWith("model-")
              ? `모델 호출 ${g.call} · 토큰과 계산`
              : g.key.startsWith("tool-")
                ? `도구 실행 → 모델 입력에 결과 추가`
                : {
                    input: "브라우저 입력 → 하네스",
                    read: "작업 공간에서 파일 읽기",
                    context: "규칙·이력·스킬 로딩 → 모델 입력",
                    output: "완료 검사 · 응답과 이력 저장",
                  }[g.stage] || "실행 준비";
          const label =
            g.agentId !== rootAgentId()
              ? g.agentName +
                " · " +
                (g.stage === "input" ? "하위 작업 → 독립 하네스" : title)
              : title;
          return `<details class="chapter" data-group="${g.key}-${i}" ${open ? "open" : ""}><summary><span class="chaptericon">${String(i + 1).padStart(2, "0")}</span>${esc(label)}<small>${g.events.length} 단계</small></summary><div class="chapterbody">${g.key.startsWith("model-") ? modelRows(g.events) : g.events.map(eventRow).join("")}</div></details>`;
        })
        .join("") || '<p class="note">일치하는 단계가 없습니다.</p>';
    // 새 기록 수신에도 사용자가 펼친 그룹을 보존한다.
    for (const d of $("timeline").querySelectorAll("details[data-group]"))
      if (opened.has(d.dataset.group)) d.open = true;
    for (const d of $("timeline").querySelectorAll("details[data-step]"))
      if (state.openSteps?.has(d.dataset.step)) d.open = true;
    state.timelineRendered = true;
  }
  function renderAgents() {
    const past = state.events.slice(0, state.cursor + 1),
      mode = state.run?.mode || "single";
    const spawned = past.filter((e) => e.kind === "agent.spawn");
    $("agentPanel").classList.toggle(
      "hidden",
      mode === "single" && !spawned.length,
    );
    $("agentFocus").classList.toggle("hidden", mode !== "team");
    if (mode === "single" && !spawned.length) return;
    const names = {
      single: "한 에이전트",
      plan: "계획만 · 파일 수정 금지",
      deep: "한 에이전트 · 집중 점검",
      team: "주 에이전트 → 하위 작업 → 결과 합류",
    };
    const nodes = [
      { id: rootAgentId(), name: "주 에이전트" },
      ...spawned.map((e) => ({
        id: e.output.child_id,
        name: e.output.agent_name,
        spawn: e,
      })),
    ];
    $("agentFocus").classList.toggle("hidden", mode !== "team");
    $("agentFocus").innerHTML =
      '<option value="all">전체 에이전트</option>' +
      nodes
        .map((n) => `<option value="${esc(n.id)}">${esc(n.name)}</option>`)
        .join("");
    $("agentFocus").value = state.agentFilter;
    const card = (n) => {
      const events = past.filter((e) => (e.agent_id || rootAgentId()) === n.id),
        last = events.at(-1);
      const end = events.findLast((e) =>
        ["run.complete", "run.error"].includes(e.kind),
      );
      const label = end
        ? end.kind === "run.complete"
          ? "완료"
          : "실패"
        : !events.length
          ? "작업 대기"
          : last?.kind === "agent.queue"
            ? "모델 차례 대기"
            : "진행";
      const requests = events.filter((e) => e.kind === "model.request").length,
        tools = events.filter((e) => e.kind === "tool.dispatch").length;
      return `<button class="agentcard ${state.agentFilter === n.id ? "selected" : ""}" data-agent="${n.id}"><strong>${esc(n.name)}<span>${label}</span></strong><small>모델 요청 ${requests} · 도구 ${tools}</small>${n.spawn ? `<p>${esc(n.spawn.output.prompt.split("[이 하위 에이전트가 수행할 작업]").at(-1).trim())}</p>` : ""}</button>`;
    };
    const queue = past
      .filter((e) => e.kind === "agent.compute")
      .reduce((sum, e) => sum + (e.output.wait_ms || 0), 0);
    const joined = past.findLast((e) => e.kind === "agent.join");
    const review = past.findLast((e) => e.kind === "agent.review");
    $("agentPanel").innerHTML =
      `<header><strong>${names[mode]}</strong><button class="quiet" data-agent="all">전체 시각순</button></header><div class="agentmain">${card(nodes[0])}</div>${nodes.length > 1 ? `<div class="agentbranches">${nodes.slice(1).map(card).join("")}</div>` : ""}<p class="agentfoot">${joined ? "✓ 하위 보고가 주 대화의 도구 결과로 합류함" : review ? "✓ 같은 에이전트에 추가 점검 요청을 전달함" : mode === "team" ? "모델이 위임할 작업을 정하면 독립 대화가 생성됩니다." : mode === "plan" ? "역할 지침 변경 · 쓰기 도구 비공개 · 파일 변경 요청 거부" : "동일 대화 · 호출 예산 확대 · 추가 점검 1회"} · 계산 차례 대기 누적 ${num(queue)} ms</p><small>같은 가중치를 사용합니다. 작업자 스레드와 모델 동시 추론은 다르며, 이 구현은 모델 계산을 직렬화합니다.</small><button class="quiet" data-file="demo_workspace/agents.json">역할·예산·도구 설정 열기 ↗</button>`;
  }
  function composition() {
    renderAgents();
    const planEvent = state.events
      .slice(0, state.cursor + 1)
      .findLast(
        (e) =>
          e.kind === "tool.result" &&
          Array.isArray(e.output?.plan) &&
          (!e.agent_id || e.agent_id === rootAgentId()),
      );
    $("planPanel").classList.toggle("hidden", !planEvent);
    if (planEvent)
      $("planPanel").innerHTML =
        "<header>모델이 등록한 작업 계획 <small>작업 결과의 검증과 별도</small></header>" +
        planEvent.output.plan
          .map(
            (step, i) =>
              `<div><span>${step.status === "done" ? "✓" : "○"}</span><p>${i + 1}. ${esc(step.text)}</p></div>`,
          )
          .join("");
    const selectedAgent = state.events[state.cursor]?.agent_id || rootAgentId();
    const e = state.events
        .slice(0, state.cursor + 1)
        .findLast(
          (e) =>
            e.output?.segments &&
            (e.agent_id || rootAgentId()) === selectedAgent,
        ),
      parts = e?.output?.segments || [];
    $("contextComposition").classList.toggle("hidden", !parts.length);
    if (!parts.length) return;
    const total = parts.reduce((s, p) => s + p.characters, 0),
      colors = [
        "#6d8ad7",
        "#86aaa4",
        "#a49ac7",
        "#c6ad7d",
        "#869db8",
        "#d09d9b",
      ];
    $("contextComposition").innerHTML =
      `<header><span>${esc(e?.agent_name || "주 에이전트")} · 이 시점의 모델 입력</span><span>${num(total)}자 · ${parts.length}개 조각</span></header><div class="stackbar">${parts.map((p, i) => `<button data-part="${i}" title="${esc(p.label)} · ${p.characters}자"></button>`).join("")}</div><div class="stacklegend">${parts.map((p, i) => `<span><i></i>${esc(p.label)} ${num(p.characters)}</span>`).join("")}</div><p class="matrixnote">문자 수에 따른 구성 비율입니다. 토큰 비율과 다릅니다.</p>`;
    [...$("contextComposition").querySelectorAll("[data-part]")].forEach(
      (b, i) => {
        b.style.flexGrow = parts[i].characters || 1;
        b.style.background = colors[i % colors.length];
        b.onclick = () => {
          $("detailContent").innerHTML = box(parts[i].label, parts[i].text);
          $("inspector").classList.add("open");
        };
      },
    );
    [...$("contextComposition").querySelectorAll(".stacklegend i")].forEach(
      (b, i) => (b.style.background = colors[i % colors.length]),
    );
  }
  function select(index, { open = false } = {}) {
    if (!state.events.length) return;
    state.cursor = Math.max(0, Math.min(index, state.events.length - 1));
    state.frame = -1;
    renderRoute();
    renderTimeline();
    composition();
    renderDetail();
    highlightFiles();
    renderArchitecture();
    $("scrubber").value = state.cursor;
    $("position").textContent = state.cursor + 1 + " / " + state.events.length;
    store("selection", { id: state.run?.id, cursor: state.cursor });
    if (open) $("inspector").classList.add("open");
  }
  function hydrate(source) {
    if (!source) return null;
    return {
      ...state.sources[source.source_ref],
      ...source,
      stack: source.stack?.map(hydrate),
    };
  }
  const eventNames = {
    "browser.input": "프롬프트 입력",
    "browser.serialize": "요청 JSON 전송",
    "browser.render": "답변 표시",
    "http.body.read": "요청 바이트 수신",
    "http.json.decode": "요청 JSON 해석",
    "harness.modules": "실행할 코드 연결",
    "thread.dispatch": "작업 스레드 시작",
    "input.receive": "하네스 진입",
    "request.route": "실행 함수 호출",
    "config.apply": "설정 적용",
    "file.resolve": "경로 확인",
    "file.exists": "파일 존재 확인",
    "file.open": "파일 열기",
    "file.read": "바이트 읽기",
    "file.close": "파일 닫기",
    "file.decode": "UTF-8 해석",
    "rules.wrap": "규칙을 system 지침으로 구성",
    "skills.scan": "스킬 파일 탐색",
    "skills.catalog": "스킬 이름·설명 선택",
    "command.expand": "명령어 프롬프트 확장",
    "context.assemble": "messages 조립",
    "context.append": "도구 결과를 messages에 추가",
    "context.repair": "부족한 행동을 다시 요청",
    "context.requirements": "완료 조건 전달",
    "model.request": "모델 요청",
    "model.template": "대화 템플릿 적용",
    "model.tokenize": "토큰 ID 생성",
    "model.detokenize": "ID를 문자열로 복원",
    "model.response": "모델 응답 수신",
    "model.stream": "출력 조각 수신",
    "tool.dispatch": "도구 요청 전달",
    "tool.validate": "인자·권한 검사",
    "tool.error": "도구 실행 실패",
    "reference.forward.start": "forward 시작",
    "reference.embedding": "임베딩 계산",
    "reference.projection": "Q/K/V 계산",
    "reference.attention": "attention 계산",
    "reference.cache.write": "KV 기록",
    "reference.sample": "다음 토큰 선택",
    "reference.protocol": "출력 형식 해석",
    "agent.mode": "모드·예산 적용",
    "agent.delegate": "작업 위임",
    "agent.spawn": "하위 대화 생성",
    "agent.join": "하위 보고 수집",
    "agent.review": "답변 추가 점검",
    "agent.queue": "모델 차례 대기",
    "agent.compute": "모델 차례 획득",
    "validation.final": "수정 파일 검사 확인",
    "validation.requirements": "완료 조건 확인",
    "validation.repeat": "미검증 답변 반복 확인",
    "output.answer": "답변 확정",
    "memory.save": "이력 저장",
    "run.complete": "실행 종료",
    "run.error": "실행 오류",
  };
  function eventName(e) {
    const title = eventNames[e.kind] || e.title;
    return e.agent_id && e.agent_id !== rootAgentId()
      ? e.agent_name + " · " + title
      : title;
  }
  function brief(value) {
    if (value == null) return "없음";
    if (typeof value === "string")
      return value.length + "자 · " + value.replace(/\s+/g, " ").slice(0, 90);
    if (Array.isArray(value))
      return (
        value.length + "개 · " + pretty(value).replace(/\s+/g, " ").slice(0, 90)
      );
    return pretty(value).replace(/\s+/g, " ").slice(0, 130);
  }
  function renderArchitecture() {
    const stateView = AgentArchitecture.update({
      events: state.events,
      cursor: state.cursor,
      run: state.run,
      health: state.health,
    });
    const e = stateView?.current;
    if (!e) return;
    $("diagramLocation").textContent =
      (e.model_purpose === "compaction"
        ? "대화 요약"
        : e.agent_name || "주 에이전트") +
      " / " +
      stateView.component +
      " / " +
      (stateView.measurement || e.kind);
    let before = e.input,
      after = e.output;
    if (e.kind === "file.decode") {
      before = { byte_count: e.input?.byte_count };
      after = e.output?.text;
    }
    if (e.kind === "model.template") after = e.output?.prompt;
    if (e.kind === "model.tokenize") after = e.output?.tokens;
    if (e.kind === "context.append") after = e.output?.appended_message;
    if (e.kind === "reference.sample") {
      before = e.output?.candidates;
      after = { id: e.output?.id, piece: e.output?.piece };
    }
    $("currentStep").innerHTML =
      `<div class="stepheading"><span class="stepnumber">${e.id} / ${state.events.length}</span><strong>${esc(eventName(e))}</strong><button class="quiet" id="stepMore">코드 · 원본</button></div><div class="steptransfer"><div><small>입력</small><span>${esc(brief(before))}</span></div><b>→</b><div><small>결과</small><span>${esc(brief(after))}</span></div></div>`;
    $("stepMore").onclick = () => $("inspector").classList.add("open");
  }
  function selectComponent(id) {
    if (id === "memory" && !state.events.length) {
      view("memory");
      return;
    }
    const at = state.events
      .slice(0, state.cursor + 1)
      .findLastIndex(
        (e) =>
          AgentArchitecture.locate(e).node === id &&
          (state.agentFilter === "all" ||
            (e.agent_id || rootAgentId()) === state.agentFilter),
      );
    if (at >= 0) {
      state.live = false;
      select(at, { open: true });
      return;
    }
    const definition = AgentArchitecture.definitions[id],
      node = AgentArchitecture.nodes.find((n) => n.id === id);
    $("detailEmpty").classList.add("hidden");
    $("detailBody").classList.remove("hidden");
    $("sourcePeek").classList.add("hidden");
    $("detailActor").textContent = "구성 설명";
    $("detailTitle").textContent = node.title;
    $("detailKind").textContent = id;
    $("detailIndex").textContent = "";
    $("detailExplanation").textContent = definition[0];
    $("componentNavigator").classList.add("hidden");
    clearInspectionExtras();
    $("detailPath").classList.add("hidden");
    $("detailContent").innerHTML =
      `<p class="note">현재 단계까지 이 구성 요소의 실행 기록이 없습니다.</p><button class="secondary" data-file="${definition[1]}">구현 파일 열기</button>`;
    document.querySelector(".detailtabs").classList.add("hidden");
    $("inspector").classList.add("open");
  }
  async function loadMemory() {
    const ticket = (state.memoryTicket || 0) + 1;
    state.memoryTicket = ticket;
    const session = state.run?.session_id || state.session;
    const data = state.run?.sample
      ? state.run.memory_snapshot || {
          records: [],
          state: {},
          summary: {},
          missing: true,
        }
      : await api("/api/memory?session_id=" + encodeURIComponent(session));
    if (ticket !== state.memoryTicket) return;
    const request = state.events.findLast(
      (e) =>
        e.kind === "model.request" &&
        e.model_purpose !== "compaction" &&
        (!e.agent_id || e.agent_id === rootAgentId()),
    );
    const compressed = state.events.findLast(
      (e) => e.kind === "memory.compaction.result",
    );
    const input = request?.input?.messages || [],
      summary = data.summary?.text || "";
    state.memoryData = { data, input, summary };
    $("memoryStorage").innerHTML =
      `<div class="memory-session"><code>${esc(data.session_id || session)}</code><span>${data.records.length}개 완료 대화 · 가중치 재학습 없음</span><button class="quiet" data-file="demo_workspace/memory.json">요약 기준 수정</button></div>${data.missing ? '<p class="notice">이 예제에는 원본 대화 저장소가 포함되지 않았습니다. 새 대화에서 실행하거나 대화 요약 예제를 선택하세요.</p>' : ""}<div class="memory-check"><label for="memoryNeedle">문자열 대조</label><input id="memoryNeedle" placeholder="규칙·프로젝트 코드 등 확인할 값"/><div id="memoryCheckResult">원본·요약·실제 messages의 문자열 포함 여부를 확인합니다.</div></div><div class="storage-grid"><section class="storecard"><header><h2>대화 원본</h2><span>추가 저장 · 삭제 안 함</span></header><code class="storage-path">${esc(data.archive_path || "아직 저장된 파일 없음")}</code>${data.records.map((record, i) => `<details class="stored-turn"><summary><b>${i + 1}</b><span>${esc((record.messages.find((m) => m.role === "user")?.content || "").slice(0, 100))}</span><code>${esc(record.run_id)}</code></summary><pre>${esc(pretty(record.messages))}</pre></details>`).join("") || '<p class="note">완료한 대화가 없습니다.</p>'}</section><section class="storecard"><header><h2>세션 상태</h2><span>다음 요청에서 읽을 데이터</span></header><code class="storage-path">${esc(data.state_path || "이전 형식의 기록")}</code><dl><dt>최근 원문</dt><dd>${data.state.turns?.length || 0}개 대화</dd><dt>요약한 범위</dt><dd>처음 ${data.summary?.through_turn || 0}개 대화</dd><dt>저장된 지적</dt><dd>${data.state.feedback_ledger?.length || 0}개</dd></dl><details><summary>세션 JSON</summary><pre>${esc(pretty(data.state))}</pre></details></section><section class="storecard"><header><h2>모델이 만든 요약</h2><span>원문과 의미가 같다고 보증하지 않음</span></header><code class="storage-path">${esc(data.summary?.path || "저장된 요약 없음")}</code><div class="stored-summary">${esc(summary || "이 대화는 아직 요약하지 않았습니다.")}</div>${compressed ? `<p class="note">이 실행에서 ${compressed.output.before_characters}자 → ${compressed.output.after_characters}자. 토큰 수와 별개입니다.</p><button class="secondary" data-memory-event="${compressed.id}">요약 전후와 코드 보기</button>` : ""}</section><section class="storecard"><header><h2>선택한 실행의 모델 입력</h2><span>디스크에 있는 내용과 구분</span></header>${request ? `<p class="note">model.request #${request.id} · ${input.length}개 메시지</p><button class="secondary" data-memory-event="${request.id}">구조도에서 입력 보기</button><details><summary>전달한 messages 원문</summary><pre>${esc(pretty(input))}</pre></details>` : '<p class="note">선택한 실행에 모델 요청이 없습니다.</p>'}</section></div>`;
    $("memoryNeedle").oninput = () => {
      const query = $("memoryNeedle").value;
      $("memoryCheckResult").textContent = query
        ? [
            ["대화 원본", pretty(data.records)],
            ["요약", summary],
            ["실제 messages", pretty(input)],
          ]
            .map(
              ([label, text]) =>
                label + ": " + (text.includes(query) ? "포함" : "없음"),
            )
            .join(" · ")
        : "문자열 포함 여부만 검사합니다. 의미나 정답을 자동 판정하지 않습니다.";
    };
  }
  function merge(data) {
    if (data.event_count < state.events.length) return;
    Object.assign(state.sources, data.source_snapshots || {});
    const oldCount = state.events.length;
    state.events.push(
      ...data.events
        .filter((e) => e.id > (state.events.at(-1)?.id || 0))
        .map((e) => ({ ...e, source: hydrate(e.source) })),
    );
    state.run = data;
    $("sampleBanner").classList.toggle("hidden", !data.sample);
    if (data.sample)
      $("sampleBanner").innerHTML =
        `<strong>실제 저장 기록 재생 · ${esc(data.sample.title)}</strong><p>${esc(data.sample.description)}</p><small>지금 모델을 계산하는 화면이 아닙니다. ${esc(data.sample.redaction)}</small>`;
    $("requestSummary").classList.remove("hidden");
    $("summaryPrompt").textContent = data.prompt;
    $("summaryMeta").textContent =
      `${data.conditions?.model || "로컬 모델"} · 주 모델 ${data.model_calls}회 · 주 도구 ${data.tool_calls}회${data.agents?.length ? " · 하위 " + data.agents.length + "개 / 전체 모델 " + data.totals.model_calls + "회" : ""}${data.implementation ? " · 하네스 " + data.implementation.version.slice(0, 10) : ""}`;
    $("scrubber").max = Math.max(0, state.events.length - 1);
    $("eventCount").textContent = state.events.length + " 기록";
    $("runStatus").textContent =
      {
        running: "실행 중",
        paused: "일시 정지",
        completed: "완료",
        stopped: "중지",
        stopping: "중지 중",
        failed: "오류",
        interrupted: "중단된 기록",
      }[data.status] || data.status;
    $("runStatus").className = "status " + data.status;
    if (data.sample) $("runStatus").textContent = "저장 기록";
    $("controls").classList.toggle(
      "hidden",
      !["running", "paused", "stopping"].includes(data.status),
    );
    $("controlState").textContent =
      data.status === "paused"
        ? "다음 단계를 기다리는 중입니다."
        : "실행 중입니다.";
    $("next").disabled = data.status !== "paused";
    $("continue").disabled = data.status !== "paused";
    $("run").disabled = busy() || !state.health.connected;
    $("export").disabled = false;
    $("showAnswer").disabled = !data.answer && !data.error;
    $("showInput").disabled = !state.events.length;
    $("showAnswer").classList.toggle("ready", !!data.answer);
    $("saveFile").disabled = busy() || !state.file?.editable;
    $("undoFile").disabled =
      busy() || !state.file || !state.edits.has(state.file.path);
    $("backend").disabled = busy() || state.health.mode === "sample";
    $("agentMode").disabled = busy() || state.health.mode === "sample";
    if (state.live && state.events.length) select(state.events.length - 1);
    else if (oldCount !== state.events.length) renderTimeline();
    renderAnswer(data);
    $("answerMeta").textContent = data.error
      ? "완료되지 않은 실행"
      : data.status === "completed"
        ? data.requirements?.length
          ? "지정한 완료 조건 통과"
          : "실행 종료 · 실제 작업 결과는 기록에서 확인"
        : "";
    $("metrics").innerHTML = [
      `전체 모델 ${data.totals?.model_calls ?? data.model_calls}회`,
      `전체 도구 ${data.totals?.tool_calls ?? data.tool_calls}회`,
      `입력 ${num(data.totals?.prompt_tokens ?? data.usage?.prompt_tokens)}토큰`,
      `출력 ${num(data.totals?.completion_tokens ?? data.usage?.completion_tokens)}토큰`,
      `활동 ${num(data.active_seconds)}초`,
    ]
      .map((s) => `<span>${s}</span>`)
      .join("");
  }
  // BROWSER_RENDER_START
  function renderAnswer(data) {
    $("answerPanel").classList.toggle("hidden", !data.answer && !data.error);
    $("answer").textContent = data.error || data.answer;
  }
  // BROWSER_RENDER_END
  async function poll(id) {
    const generation = state.generation || 0;
    clearTimeout(state.timer);
    try {
      const data = await api(
        "/api/runs/" + id + "?compact=1&after=" + state.events.length,
      );
      if (
        generation !== (state.generation || 0) ||
        (state.run && state.run.id !== id)
      )
        return;
      merge(data);
      if (["running", "paused", "stopping"].includes(data.status))
        state.timer = setTimeout(() => poll(id), 650);
      else {
        if (
          ["completed", "failed"].includes(data.status) &&
          !state.renderAcks?.has(id)
        ) {
          state.renderAcks ??= new Set();
          await api("/api/runs/" + id + "/render", {
            text: $("answer").textContent,
            reported_at: new Date().toISOString(),
          });
          state.renderAcks.add(id);
          merge(
            await api(
              "/api/runs/" + id + "?compact=1&after=" + state.events.length,
            ),
          );
        }
        await history();
        await loadProject();
        state.catalog = await api("/api/implementation");
        learn();
        await health();
        await refreshActivity();
      }
    } catch (error) {
      toast(error.message);
      state.timer = setTimeout(() => poll(id), 2000);
    }
  }
  async function loadRun(id) {
    state.agentFilter = "all";
    const generation = (state.generation || 0) + 1;
    state.generation = generation;
    clearTimeout(state.timer);
    stopPlay();
    state.events = [];
    state.sources = {};
    state.live = false;
    state.cursor = -1;
    state.inputMessage = -1;
    state.timelineRendered = false;
    $("journeyView").classList.add("focus");
    state.openGroups = new Set();
    state.openSteps = new Set();
    state.run = { id, status: "loading" };
    $("runStatus").textContent = "기록 불러오는 중";
    const data = await api("/api/runs/" + id + "?compact=1");
    if (generation !== state.generation) return;
    state.agentFilter =
      data.mode === "team" ? data.sample?.source_run_id || id : "all";
    state.run = data;
    merge(data);
    select(0);
    view("journey");
    if (["running", "paused", "stopping"].includes(data.status))
      state.timer = setTimeout(() => poll(id), 600);
  }
  // BROWSER_INPUT_START
  function observeInput(event) {
    if (event?.type === "input") {
      state.inputStart ??= new Date().toISOString();
      state.inputChanges++;
      $("requirements").value = "[]";
      features.writes = false;
      featureUI();
    }
    const text = $("prompt").value;
    $("draftInfo").textContent =
      `브라우저 문자열 ${[...text].length}자 → UTF-8 ${new TextEncoder().encode(text).length}바이트 · 아직 모델에 전달하지 않았습니다.`;
  }
  // BROWSER_INPUT_END
  // BROWSER_SUBMIT_START
  async function submit(event) {
    event?.preventDefault();
    if (busy()) {
      await refreshActivity();
      toast("진행 중인 요청은 화면 위의 실행 상태에서 계속하거나 중지하세요.");
      return;
    }
    saveDraft();
    state.generation = (state.generation || 0) + 1;
    stopPlay();
    const payload = {
      prompt: $("prompt").value,
      session_id: state.session,
      step: $("stepMode").checked,
      features: { ...features },
      engine: engine(),
      requirements: JSON.parse($("requirements").value),
      mode: $("agentMode").value,
      client_trace: {
        first_input_at: state.inputStart,
        input_events: state.inputChanges,
        submitted_at: new Date().toISOString(),
        characters: [...$("prompt").value].length,
        utf8_bytes: new TextEncoder().encode($("prompt").value).length,
      },
    };
    const data = await api("/api/runs", payload);
    state.events = [];
    state.agentFilter = payload.mode === "team" ? data.id : "all";
    state.sources = {};
    state.run = { id: data.id, status: "running" };
    state.live = true;
    state.timelineRendered = false;
    state.cursor = -1;
    $("journeyView").classList.add("focus");
    state.openGroups = new Set();
    state.openSteps = new Set();
    state.inputStart = null;
    state.inputChanges = 0;
    await poll(data.id);
  }
  // BROWSER_SUBMIT_END
  function box(label, value) {
    return `<div class="databox"><div class="datalabel"><i></i>${esc(label)}</div><pre>${esc(pretty(value))}</pre></div>`;
  }
  function stats(items) {
    return `<div class="statgrid">${items.map(([label, value]) => `<div class="stat"><strong>${esc(value)}</strong><small>${esc(label)}</small></div>`).join("")}</div>`;
  }
  function piece(value) {
    return Array.isArray(value)
      ? "bytes " + value.map((v) => v.toString(16).padStart(2, "0")).join(" ")
      : String(value ?? "")
          .replaceAll("\n", "↵")
          .replaceAll(" ", "␠");
  }
  function transform(e) {
    let before = e.input,
      after = e.output,
      bl = "입력",
      al = "결과";
    const past = state.events.slice(0, state.cursor + 1),
      previous = (kind) =>
        past
          .slice(0, -1)
          .findLast(
            (item) =>
              item.kind === kind &&
              (!e.agent_id || !item.agent_id || item.agent_id === e.agent_id) &&
              (!kind.startsWith("model.") ||
                AgentInspection.purposeOf(item) ===
                  AgentInspection.purposeOf(e)) &&
              (kind !== "file.read" || item.input?.path === e.input?.path),
          );
    switch (e.kind) {
      case "rules.wrap":
        before = e.input?.raw_text;
        after = e.output?.instruction_block;
        bl = "파일 원문";
        al = "경계를 붙인 지침";
        break;
      case "file.decode":
        before = previous("file.read")?.output;
        after = e.output?.text;
        bl = "파일에서 읽은 바이트";
        al = "UTF-8 문자열";
        break;
      case "command.expand":
        before = e.input?.raw_prompt;
        after = e.output?.effective_prompt;
        bl = "사용자 입력";
        al = "명령어 템플릿 적용";
        break;
      case "context.assemble":
        before = e.output?.segments;
        after = e.output?.messages;
        bl = "선택한 지침과 맥락";
        al = "역할이 붙은 messages";
        break;
      case "model.template":
        before = previous("model.request")?.input;
        after = e.output?.prompt;
        bl = "messages + tools";
        al = "모델용 템플릿 문자열";
        break;
      case "model.tokenize":
        before = previous("model.template")?.output?.prompt;
        bl = "템플릿 문자열";
        al = "토큰 ID 배열";
        break;
      case "tool.validate":
        before = past.find((p) => p.id === e.parent)?.input?.function
          ?.arguments;
        after = { arguments: e.input?.arguments, ...e.output };
        bl = "모델이 만든 JSON 인자";
        al = "파싱과 실행 조건 검사";
        break;
      case "context.append":
        before = e.input;
        after = e.output?.appended_message;
        bl = "추가 전 메시지 수";
        al = "다음 호출에 추가할 tool 메시지";
        break;
      case "model.request":
        before =
          previous("model.request")?.input?.messages || state.run?.prompt;
        after = e.input?.messages;
        bl = previous("model.request") ? "이전 모델 입력" : "사용자 요청";
        al = "이번 모델 호출의 messages";
        break;
      case "model.response":
        before = previous("model.request")?.input?.messages?.at(-1);
        after = e.output?.choices?.[0]?.message;
        bl = "입력의 마지막 메시지";
        al = "생성한 응답 객체";
        break;
      case "reference.protocol":
        before = e.output?.raw_text;
        after = e.output?.message;
        bl = "모델 생성 원문";
        al = "답변과 도구 요청으로 해석";
        break;
      case "reference.sample":
        before = e.output?.candidates;
        after = {
          selected_id: e.output?.id,
          piece: e.output?.piece,
          text: e.output?.text_so_far,
        };
        bl = "필터를 거친 토큰 후보";
        al = "선택한 토큰 → 누적 출력";
        break;
      case "output.answer":
        before = previous("model.response")?.output?.choices?.[0]?.message;
        after = e.output?.answer;
        bl = "최종 응답 객체";
        al = "사용자에게 표시할 본문";
        break;
    }
    if (e.kind === "tool.result" && e.output?.after !== undefined) {
      before = e.output.before;
      after = e.output.after;
      bl = "수정 전";
      al = "수정 후";
    }
    return { before, after, bl, al };
  }
  function bars(items) {
    return `<div class="bars">${items.map((item) => `<div class="barrow"><code>${esc(item.label)}</code><div class="bartrack"><div class="barfill" data-width="${Math.max(0, Math.min(100, item.value * 100))}"></div></div><span>${(item.value * 100).toFixed(2)}%</span></div>`).join("")}</div>`;
  }
  function matrix(values, title, caption) {
    if (!values?.length) return "";
    return `<h3>${title}</h3><div class="heatgrid">${values
      .flat()
      .map((value) => `<span data-heat="${value}" title="${value}"></span>`)
      .join("")}</div><p class="matrixnote">${esc(caption)}</p>`;
  }
  function specialized(e) {
    const o = e.output || {};
    if (
      e.kind === "tool.result" &&
      o.protocol === "lsp" &&
      Array.isArray(o.diagnostics)
    )
      return (
        stats([
          ["실제 진단", o.diagnostic_count],
          ["문서 버전", o.document_version],
        ]) +
        o.diagnostics
          .map(
            (d) =>
              `<div class="diagnostic"><strong>${esc(d.message)}</strong><p>${esc(d.source)} · ${d.range.start.line + 1}행 · UTF-16 열 ${d.range.start.character + 1}</p></div>`,
          )
          .join("") +
        `<button class="secondary" data-file="demo_workspace/${esc(o.path)}">진단한 파일 열기 ↗</button>` +
        box("언어 서버의 실제 진단", o.diagnostics)
      );
    if (e.kind === "model.tokenize") {
      let offset = 0;
      const range = o.user_prompt_byte_range,
        tokens = o.tokens || [];
      return (
        stats([
          ["입력 토큰", num(o.token_count)],
          [
            "UTF-8 조각 복원",
            o.pieces_match_utf8 ? "원문 일치" : "일치 확인 안 됨",
          ],
        ]) +
        `<h3>문자열 → 토큰 조각 → ID</h3><p class="note">토큰을 선택하면 위치와 실제 UTF-8 바이트를 확인할 수 있습니다. 초록 테두리는 현재 사용자 요청과 겹치는 바이트입니다.</p><div class="tokenlist">${tokens
          .map((t, i) => {
            const bytes = Array.isArray(t.piece)
                ? t.piece
                : new TextEncoder().encode(t.piece || ""),
              start = offset;
            offset += bytes.length;
            return `<button class="tokenchip ${range && start < range[1] && offset > range[0] ? "user" : ""}" data-token="${i}" data-start="${start}">${esc(piece(t.piece))}<small>${t.id}</small></button>`;
          })
          .join(
            "",
          )}</div><div id="tokenInfo" class="tokeninfo">${tokens.length}개 토큰 · 숫자 ID는 단어 번호가 아닙니다.</div>`
      );
    }
    if (e.kind === "reference.cache.select")
      return (
        stats([
          ["전체 입력", num(o.prompt_tokens)],
          ["실제 접두부 재사용", num(o.cached_tokens)],
        ]) +
        box("캐시 사용 설정", {
          use_kv: o.use_kv,
          cache_prompt: o.cache_prompt,
          capture_layer: o.capture_layer,
        })
      );
    if (e.kind === "reference.cache.write") {
      const c = o.cache || {},
        layer = c.layers?.find((l) => l.layer === c.captured_layer);
      return (
        stats([
          ["캐시 위치 수", num(c.tokens)],
          ["실제 K/V 메모리", (c.bytes / 1048576).toFixed(2) + " MiB"],
          ["이번 forward 입력", num(o.forward_input_tokens)],
          ["누적 forward 입력", num(o.cumulative_forward_tokens)],
          [
            "forward 벽시간 (hook 포함)",
            o.observed_forward_ms == null
              ? "미기록"
              : num(o.observed_forward_ms) + " ms",
          ],
          [
            "제외한 사용자 정지",
            o.user_pause_ms == null ? "미기록" : num(o.user_pause_ms) + " ms",
          ],
        ]) +
        `<p class="note">${c.enabled ? "K/V 텐서의 numel × element_size를 합산했습니다. 가중치·임시 attention 메모리는 포함하지 않습니다." : "KV를 끈 실행입니다. 매 토큰마다 전체 접두부를 다시 계산합니다."}</p>` +
        matrix(
          layer?.k_slice,
          "K · 실제 텐서 표본",
          `layer ${c.captured_layer} · head 0 · positions ${layer?.slice_positions?.join(",")} · dims 0–15`,
        ) +
        matrix(
          layer?.v_slice,
          "V · 실제 텐서 표본",
          "행=토큰 위치 · 열=head 차원. 색은 각 표본의 최대 절댓값에 맞췄습니다.",
        ) +
        box(
          "전체 레이어 shape",
          c.layers?.map((l) => ({
            layer: l.layer,
            K: l.k_shape,
            V: l.v_shape,
            bytes: l.bytes,
          })),
        )
      );
    }
    if (e.kind === "reference.attention")
      return (
        stats([
          ["레이어 / head", o.layer + " / " + o.head],
          ["전체 가중치 행 합", num(o.row_sum)],
        ]) +
        `<h3>마지막 query가 참고한 위치</h3>` +
        bars(
          (o.top_positions || []).map((p, i) => ({
            label: "position " + p,
            value: o.top_weights[i],
          })),
        ) +
        box("관찰 범위", {
          shape: o.shape,
          query_position: o.query_position,
          last_positions: o.last_positions,
          last_weights: o.last_weights,
        }) +
        '<p class="note">attention 값은 의미적 판단의 이유를 설명하지 않습니다.</p>'
      );
    if (e.kind === "reference.sample")
      return (
        stats([
          ["선택 토큰 ID", o.id],
          ["선택 방식", o.strategy],
        ]) +
        `<h3>실제 후보 분포 · 상위 ${o.candidates?.length || 0}개</h3>` +
        bars(
          (o.candidates || []).map((c) => ({
            label: piece(c.piece) + " · " + c.id,
            value: c.prob,
          })),
        ) +
        box("이번에 생성된 내용", o.text_so_far) +
        box("적용한 설정", {
          temperature: o.temperature,
          top_k: o.top_k,
          top_p: o.top_p,
        })
      );
    if (["reference.embedding", "reference.projection"].includes(e.kind)) {
      const v = o.last_position;
      return (
        stats([
          ["출력 shape", (o.tensor_shape || []).join(" × ")],
          ["관찰 위치", o.projection || "embedding"],
        ]) +
        matrix(
          [v?.sample || []],
          "마지막 위치 · 실제 수치 표본",
          `전체 벡터 shape ${v?.shape} · 처음 ${v?.sample?.length}개 값`,
        ) +
        box("벡터 수치", v)
      );
    }
    if (e.kind === "runtime.cache")
      return (
        stats([
          ["실제 재사용", num(o.cached_tokens ?? o.cache_n)],
          ["새 입력", num(o.fresh_prompt_tokens ?? o.prompt_n)],
        ]) + box("실행기 보고", o)
      );
    return "";
  }
  function inspectionLink(record, label, tab = "input", message = -1) {
    return `<button class="inspection-link" data-inspection-event="${record.event.id}" data-inspection-tab="${tab}" data-inspection-message="${message}">${esc(label)}</button>`;
  }
  function changeLabel(change) {
    return {
      added: "추가",
      removed: "제거",
      changed: "변경",
      unchanged: "유지",
    }[change];
  }
  function renderInputView() {
    const inspected = AgentInspection.snapshot(
      state.events,
      state.cursor,
      rootAgentId(),
    );
    if (!inspected.request)
      return `<div class="input-empty"><h3>모델 입력 확정 전</h3><p>${inspected.draft ? `컨텍스트 조립 기록 #${inspected.draft.id}가 있습니다. 아직 model.request가 기록되지 않았습니다.` : "이 에이전트와 요청 유형의 model.request가 아직 없습니다."}</p><p class="note">이후 단계의 입력을 현재 값으로 표시하지 않습니다. 단계를 진행하거나 저장된 요청을 선택하세요.</p>${inspected.next ? inspectionLink(inspected.next, `다음 요청 #${inspected.next.event.id}`) : ""}</div>`;
    const { request, previous, next } = inspected,
      payload = request.event.input;
    const segments = request.event.output?.segments || [];
    const tools = payload.tools || [];
    const counts = inspected.messageDiff.reduce(
      (all, row) => ({ ...all, [row.change]: (all[row.change] || 0) + 1 }),
      {},
    );
    const changed = inspected.messageDiff.filter(
      (row) => row.change !== "unchanged",
    );
    const changedTools = inspected.toolDiff.filter(
      (row) => row.change !== "unchanged",
    );
    const check = (value) =>
      value === true ? "일치" : value === false ? "불일치" : "미확인";
    const body = payload.messages
      .map((message, index) => {
        const labels = [
          ...new Set(
            segments
              .filter((part) => part.message_index === index)
              .map((part) => {
                const label =
                  !part.origin &&
                  part.label === "모델 응답" &&
                  message.role === "user"
                    ? "출처 미기록 (이전 기록)"
                    : part.label;
                return (
                  label +
                  (part.origin
                    ? " · 작성: " +
                      ({
                        user: "사용자",
                        harness: "하네스",
                        model: "모델",
                        tool: "도구",
                        unknown: "미기록",
                      }[part.origin] || part.origin)
                    : "")
                );
              }),
          ),
        ];
        const matches = AgentInspection.origins(
          state.events,
          request.index,
          index,
          rootAgentId(),
        );
        const declared = AgentInspection.declaredOrigins(
          state.events,
          request.index,
          index,
          rootAgentId(),
        );
        const pieces = AgentInspection.fragments(
          message,
          segments.filter((part) => part.message_index === index),
        );
        const content =
          pieces.length > 1
            ? pieces
                .map(
                  (part) =>
                    `<details class="message-fragment" ${state.inputMessage === index && part.kind === "rules" ? "open" : ""}><summary>${esc(part.label || "본문")} · ${num([...part.text].length)}자</summary>${box("본문 원문", part.text)}</details>`,
                )
                .join("")
            : box("content", message.content);
        const tags =
          labels.length && pieces.length < 2
            ? `<div class="message-labels">${labels.map((label) => `<span>${esc(label)}</span>`).join("")}${declared.map((record) => inspectionLink(record, `추가 기록 #${record.event.id} ${eventName(record.event)}`, "change")).join("")}</div>`
            : "";
        const messageSummary = [
          typeof message.content === "string"
            ? num([...message.content].length) + "자"
            : null,
          message.tool_calls?.length
            ? "도구 요청 " + message.tool_calls.length + "개"
            : null,
        ]
          .filter(Boolean)
          .join(" · ");
        return `<details class="input-message" ${index === state.inputMessage || (state.inputMessage < 0 && index === payload.messages.length - 1) ? "open" : ""}><summary><code>messages[${index}]</code><strong>${esc(message.role)}</strong><span>${esc(messageSummary)}</span></summary>${tags}${content}${message.tool_calls?.length ? box("tool_calls", message.tool_calls) : ""}<details class="message-json"><summary>메시지 JSON</summary>${box("전체 메시지", message)}</details>${matches.length ? `<div class="input-origins"><small>값이 일치하는 기록</small>${matches.map((record) => inspectionLink(record, `#${record.event.id} ${eventName(record.event)}${record.path ? " · " + record.path.replaceAll("\\", "/").split("/").at(-1) : ""}`, "change")).join("")}</div>` : ""}</details>`;
      })
      .join("");
    const diff = previous
      ? `<details class="input-diff" open><summary>이전 입력 #${previous.event.id}과 비교</summary><p class="note">같은 에이전트·요청 유형의 직전 요청입니다. messages의 배열 위치별 값을 대조합니다.</p><div class="input-diff-counts">${["unchanged", "added", "removed", "changed"].map((key) => `<span>${changeLabel(key)} ${counts[key] || 0}</span>`).join("")}</div>${changed.map((row) => `<details class="input-change"><summary><span class="change-${row.change}">${changeLabel(row.change)}</span><code>messages[${row.index}]</code><span>${esc((row.after || row.before).role)}</span></summary>${row.before !== undefined ? box("이전", row.before) : ""}${row.after !== undefined ? box("현재", row.after) : ""}</details>`).join("") || '<p class="note">메시지 변경이 없습니다.</p>'}${changedTools.length ? `<h4>도구 정의 변경</h4>${changedTools.map((row) => `<details class="input-change"><summary><span class="change-${row.change}">${changeLabel(row.change)}</span><code>${esc(row.name)}</code></summary>${row.before ? box("이전 schema", row.before) : ""}${row.after ? box("현재 schema", row.after) : ""}</details>`).join("")}` : '<p class="note">도구 정의 변경이 없습니다.</p>'}</details>`
      : '<p class="input-first">같은 에이전트·요청 유형의 첫 입력입니다.</p>';
    const parameters = Object.fromEntries(
      [
        "model",
        "temperature",
        "max_tokens",
        "seed",
        "top_k",
        "top_p",
        "cache_prompt",
        "stream",
      ]
        .filter((key) => payload[key] !== undefined)
        .map((key) => [key, payload[key]]),
    );
    return `<div class="input-snapshot"><header><strong>마지막 확정 입력 #${request.event.id}</strong><span>${esc(request.event.agent_name || "주 에이전트")} · ${inspected.purpose === "compaction" ? "대화 요약" : "일반 답변"}</span></header><div class="input-navigation">${previous ? inspectionLink(previous, "← 이전 입력") : ""}${inspectionLink(request, "요청 원본", "values")}${next ? inspectionLink(next, "다음 입력 →") : ""}</div><p class="note">선택한 단계까지 기록된 model.request입니다. 이후 도구 결과가 자동으로 이 입력에 추가되지는 않습니다.</p><div class="input-stats"><span><b>${payload.messages.length}</b> 메시지</span><span><b>${tools.length}</b> 도구</span><span><b>${num(inspected.diagnosticTokens)}</b> 진단 토큰</span><span><b>${num(inspected.reportedTokens)}</b> 보고 토큰</span></div><div class="input-audit">토큰 수 대조: ${check(inspected.audit?.countMatches)} · 템플릿 대조: ${check(inspected.audit?.templateMatches)}</div>${diff}<h3>messages</h3>${body}<details class="input-tools"><summary>모델에 공개한 tools · ${tools.length}개</summary>${box("tools 원문", tools)}</details><details class="input-tools"><summary>생성 설정</summary>${box("기록된 요청 필드", parameters)}</details><p class="note">진단 토큰과 생성 응답의 보고 토큰은 별도 값입니다. —는 아직 수집되지 않은 값입니다. 파일 연결은 문자열·메시지 값의 일치이며, 모델이 내용을 이해하거나 따랐다는 판정은 아닙니다.</p></div>`;
  }
  function clearInspectionExtras() {
    $("inputLinks").classList.add("hidden");
    $("evidenceKind").textContent = "";
    $("evidenceKind").title = "";
    $("detailExplanation").classList.remove("hidden");
  }
  function renderDetail() {
    $("inspector").classList.toggle("input-view", state.tab === "input");
    document.querySelector(".detailtabs").classList.remove("hidden");
    const e = state.events[state.cursor];
    $("detailEmpty").classList.toggle("hidden", !!e);
    $("detailBody").classList.toggle("hidden", !e);
    if (!e) return;
    const node = AgentArchitecture.locate(e).node;
    const related = state.events
      .map((event, index) => ({ event, index }))
      .filter(
        ({ event }) =>
          AgentArchitecture.locate(event).node === node &&
          (event.agent_id || rootAgentId()) === (e.agent_id || rootAgentId()),
      );
    $("componentNavigator").classList.remove("hidden");
    $("componentNavigator").innerHTML =
      `<label for="componentEvent">${esc(AgentArchitecture.nodes.find((n) => n.id === node)?.title)} · 저장된 관찰 ${related.length}개</label><select id="componentEvent">${related.map(({ event, index }) => `<option value="${index}" ${event.id === e.id ? "selected" : ""}>#${event.id} ${esc(eventName(event))}</option>`).join("")}</select>`;
    $("componentEvent").onchange = () => {
      state.live = false;
      stopPlay();
      select(Number($("componentEvent").value), { open: true });
    };
    $("detailIndex").textContent =
      "#" + e.id + " · " + num(e.elapsed_ms) + " ms";
    $("detailActor").textContent =
      (actors[e.actor] || actors.harness)[1] +
      (e.status === "ok" ? "" : " · " + e.status);
    const evidence = AgentInspection.evidence(e);
    $("evidenceKind").textContent = evidence.label;
    $("evidenceKind").title = evidence.explanation;
    $("evidenceKind").dataset.evidence = evidence.type;
    $("detailTitle").textContent = eventName(e);
    $("detailKind").textContent = e.kind;
    const filePath = e.kind.startsWith("file.")
      ? e.input?.path || e.input?.requested_path || e.output?.absolute_path
      : null;
    $("detailPath").classList.toggle("hidden", !filePath);
    $("detailPath").textContent = filePath || "";
    $("detailExplanation").textContent = e.detail;
    $("detailExplanation").classList.toggle("hidden", state.tab === "input");
    const targets = AgentInspection.consumers(
      state.events,
      state.cursor,
      rootAgentId(),
    );
    $("inputLinks").classList.toggle(
      "hidden",
      !targets.length || state.tab === "input",
    );
    $("inputLinks").innerHTML = targets.length
      ? inspectionLink(
          targets[0],
          `모델 입력 #${targets[0].event.id}에서 확인`,
          "input",
          targets[0].indices[0],
        ) + `<small>동일한 값이 포함된 요청 ${targets.length}개</small>`
      : "";
    const source = e.source,
      peek = $("sourcePeek");
    peek.classList.toggle(
      "hidden",
      !source || ["code", "input"].includes(state.tab),
    );
    if (source && state.tab !== "code") {
      const lines = source.code.split("\n"),
        position = source.line - source.start_line;
      const start = Math.max(0, position - 2),
        end = Math.min(lines.length, position + 3);
      peek.innerHTML = `<button id="peekCode" class="sourcepath">${esc(source.file.replaceAll("\\", "/"))}:${source.line} ↗</button><div class="sourcecode">${lines
        .slice(start, end)
        .map(
          (line, i) =>
            `<div class="codeline ${start + i === position ? "hit" : ""}"><span>${source.start_line + start + i}</span><code>${esc(line) || " "}</code></div>`,
        )
        .join("")}</div>`;
      $("peekCode").onclick = () => {
        state.tab = "code";
        renderDetail();
      };
    }
    document
      .querySelectorAll("[data-detail]")
      .forEach((b) =>
        b.classList.toggle("active", b.dataset.detail === state.tab),
      );
    const c = $("detailContent");
    if (state.tab === "input") c.innerHTML = renderInputView();
    else if (state.tab === "values")
      c.innerHTML = box("전체 이벤트 · 원본 값", e);
    else if (state.tab === "code") {
      const frames = e.source?.stack?.length
          ? e.source.stack
          : [e.source].filter(Boolean),
        frame = frames[state.frame < 0 ? frames.length - 1 : state.frame];
      c.innerHTML = frame
        ? `<div class="codeactions"><span>이벤트 당시의 소스 스냅샷</span><button class="quiet" id="editSource">파일 열기 ↗</button></div><div class="stackframes">${frames.map((f, i) => `<button data-frame="${i}" class="${f === frame ? "selected" : ""}">${esc(f.function)} · ${esc(f.file.replaceAll("\\", "/"))}:${f.line}</button>`).join("")}</div><p class="sourcepath">${esc(frame.file)}:${frame.line}</p><div class="sourcecode">${frame.code
            .split("\n")
            .map(
              (line, i) =>
                `<div class="codeline ${frame.start_line + i === frame.line ? "hit" : ""}"><span>${frame.start_line + i}</span><code>${esc(line) || " "}</code></div>`,
            )
            .join(
              "",
            )}</div><p class="note">강조는 기록을 남긴 실제 코드 위치입니다. 소스의 모든 줄을 추적하는 디버거와는 관찰 범위가 다릅니다.</p>`
        : '<p class="note">이 기록에는 소스 스냅샷이 없습니다.</p>';
      if (frame)
        $("editSource").onclick = action(() =>
          openFile(frame.file.replaceAll("\\", "/")),
        );
    } else {
      const special = specialized(e);
      if (special) c.innerHTML = special;
      else {
        const t = transform(e);
        c.innerHTML =
          box(t.bl, t.before) +
          '<div class="direction">↓</div>' +
          box(t.al, t.after);
      }
      if (e.source)
        c.insertAdjacentHTML(
          "beforeend",
          `<button id="inlineCode" class="secondary">실행 코드 · ${esc(e.source.function)}:${e.source.line} →</button>`,
        );
      if ($("inlineCode"))
        $("inlineCode").onclick = () => {
          state.tab = "code";
          renderDetail();
        };
    }
    c.querySelectorAll("[data-width]").forEach(
      (el) => (el.style.width = el.dataset.width + "%"),
    );
    c.querySelectorAll(".heatgrid").forEach((grid) => {
      const cells = [...grid.children],
        max = Math.max(
          0.0001,
          ...cells.map((el) => Math.abs(Number(el.dataset.heat))),
        );
      for (const cell of cells) {
        const value = Number(cell.dataset.heat),
          alpha = 0.12 + (0.88 * Math.abs(value)) / max;
        cell.style.background =
          value >= 0
            ? `rgba(65,102,194,${alpha})`
            : `rgba(197,125,82,${alpha})`;
      }
    });
    c.querySelectorAll("[data-frame]").forEach(
      (b) =>
        (b.onclick = () => {
          state.frame = Number(b.dataset.frame);
          renderDetail();
        }),
    );
    c.querySelectorAll("[data-token]").forEach(
      (b) =>
        (b.onclick = () => {
          c.querySelectorAll(".tokenchip").forEach((b) =>
            b.classList.remove("selected"),
          );
          b.classList.add("selected");
          const t = e.output.tokens[Number(b.dataset.token)],
            bytes = Array.isArray(t.piece)
              ? t.piece
              : [...new TextEncoder().encode(t.piece || "")];
          $("tokenInfo").textContent =
            `위치 ${b.dataset.token} · ID ${t.id} · byte offset ${b.dataset.start} · UTF-8 ${bytes.map((v) => v.toString(16).padStart(2, "0")).join(" ")} · 원본 조각 ${pretty(t.piece)}`;
        }),
    );
  }
  function highlightFiles() {
    const e = state.events[state.cursor],
      path = pathOf(e),
      read = path
        ? path.startsWith("demo_workspace/")
          ? path
          : "demo_workspace/" + path
        : null,
      code = e?.source?.file?.replaceAll("\\", "/");
    const counts = new Map();
    for (const item of state.events.slice(0, state.cursor + 1)) {
      const p = pathOf(item);
      if (!p) continue;
      const key = p.startsWith("demo_workspace/") ? p : "demo_workspace/" + p;
      const value = counts.get(key) || { r: 0, w: 0 };
      if (item.kind === "file.read") value.r++;
      if (
        item.kind === "tool.result" &&
        item.output?.written_bytes !== undefined
      )
        value.w++;
      counts.set(key, value);
    }
    $("fileTree")
      .querySelectorAll("[data-file]")
      .forEach((b) => {
        b.classList.toggle(
          "hit",
          b.dataset.file === read &&
            (e?.kind.startsWith("file.") ||
              (e?.kind === "tool.result" &&
                e.output?.written_bytes !== undefined)),
        );
        b.dataset.hitLabel =
          {
            "file.resolve": "경로",
            "file.exists": "확인",
            "file.open": "열기",
            "file.read": "읽기",
            "file.close": "닫기",
            "file.decode": "변환",
          }[e?.kind] || "쓰기";
        let count = b.querySelector(".filecounts");
        if (!count) {
          count = document.createElement("small");
          count.className = "filecounts";
          b.append(count);
        }
        const value = counts.get(b.dataset.file);
        count.textContent =
          value && (value.r || value.w)
            ? `R${value.r}${value.w ? " W" + value.w : ""}`
            : "";
        b.classList.toggle(
          "selected",
          b.dataset.file ===
            (state.view === "editor" ? state.file?.path : code),
        );
      });
    const selectedTool = state.events
      .slice(0, state.cursor + 1)
      .findLast(
        (item) =>
          item.kind === "tool.dispatch" && item.id === (e?.parent || e?.id),
      )?.input?.function?.name;
    $("toolList")
      .querySelectorAll("[data-tool]")
      .forEach((b) =>
        b.classList.toggle("active", b.dataset.tool === selectedTool),
      );
  }
  async function loadProject() {
    state.files = (await api("/api/project")).files;
    const groups = [
      ["harness", "하네스 코드"],
      ["workspace", "규칙 · 스킬 · 작업 파일"],
      ["engine", "추론 실행기"],
      ["reference", "구현 참고"],
    ];
    $("fileTree").innerHTML = groups
      .map(
        ([kind, label]) =>
          `<details ${kind === "harness" || kind === "workspace" ? "open" : ""}><summary>${label}</summary>${state.files
            .filter((f) => f.kind === kind)
            .map(
              (f) =>
                `<button data-file="${esc(f.path)}" title="${esc(f.path)}${f.editable ? " · 편집 가능" : " · 읽기 전용"}"><i>${f.editable ? "◇" : "·"}</i><span>${esc(f.path.replace("local_agent_lab/", "").replace("demo_workspace/", ""))}</span></button>`,
            )
            .join("")}</details>`,
      )
      .join("");
    highlightFiles();
  }
  function feedbackOptions() {
    const chosen = $("feedbackRun").value || state.run?.id,
      items = [...(state.history || []), ...(state.samples || [])];
    $("feedbackRun").innerHTML = items
      .map(
        (r) =>
          `<option value="${r.id}">${r.id.startsWith("sample-") ? "[샘플] " : ""}${esc((r.prompt || r.title).slice(0, 45))}</option>`,
      )
      .join("");
    if (items.some((r) => r.id === chosen)) $("feedbackRun").value = chosen;
  }
  async function loadSamples() {
    state.samples = await api("/api/samples");
    $("samples").innerHTML = state.samples
      .map(
        (r) =>
          `<button data-run="${r.id}">${esc(r.title)}<small>실제 기록 재생 · ${r.events}단계</small></button>`,
      )
      .join("");
    feedbackOptions();
  }
  async function history() {
    const items = await api("/api/history");
    state.history = items;
    feedbackOptions();
    $("history").innerHTML =
      items
        .slice(0, 8)
        .map(
          (r) =>
            `<button data-run="${r.id}">${esc(r.prompt.slice(0, 42))}<small>${{ completed: "완료", failed: "오류", stopped: "중지", running: "실행 중", paused: "정지" }[r.status] || r.status} · ${num(r.event_count)}단계</small></button>`,
        )
        .join("") || '<p class="note">아직 실행 기록이 없습니다.</p>';
  }
  async function openFile(path, { discard = false } = {}) {
    const ticket = (state.fileTicket || 0) + 1;
    state.fileTicket = ticket;
    if (!discard && state.view === "editor" && state.file) {
      if ($("codeEditor").value === state.file.text)
        state.drafts.delete(state.file.path);
      else state.drafts.set(state.file.path, $("codeEditor").value);
    }
    const revision = state.fileRevisions?.get(path) || 0;
    let file = await api("/api/project/file?path=" + encodeURIComponent(path));
    if (state.fileTicket !== ticket) return;
    if ((state.fileRevisions?.get(path) || 0) !== revision) {
      file = await api("/api/project/file?path=" + encodeURIComponent(path));
      if (state.fileTicket !== ticket) return;
    }
    state.file = file;
    if (state.drafts.get(path) === file.text) state.drafts.delete(path);
    view("editor");
    $("editorTitle").textContent = path.split("/").at(-1);
    $("editorKind").textContent = path;
    $("codeEditor").value = state.drafts.get(path) ?? file.text;
    $("codeEditor").readOnly = !file.editable;
    $("checkFile").disabled = !file.editable;
    $("saveFile").disabled = !file.editable || busy();
    $("undoFile").disabled = !state.edits.has(path) || busy();
    $("editState").textContent =
      (file.editable ? "편집 가능" : "읽기 전용") +
      " · SHA " +
      file.sha256.slice(0, 10);
    $("diff").classList.add("hidden");
    $("editMessage").textContent = "";
    const component = state.catalog.components.find(
      (c) => c.edit_path === path || c.source?.file === path,
    );
    $("fileDescription").innerHTML = component
      ? `<strong>${esc(component.label)} · 이 파일의 역할</strong><p>${esc(component.purpose)}</p><p>${esc(component.when)}</p>`
      : `<strong>${path.startsWith("demo_workspace/") ? "모델이 읽거나 도구가 작업하는 실제 파일" : "실행에 사용되는 소스"}</strong><p>수정 전후의 차이와 다음 실행 기록을 함께 확인하세요.</p>`;
    editorLines();
    highlightFiles();
  }
  function editorLines() {
    $("lineNumbers").textContent = Array.from(
      { length: $("codeEditor").value.split("\n").length },
      (_, i) => i + 1,
    ).join("\n");
    $("lineNumbers").scrollTop = $("codeEditor").scrollTop;
  }
  function editMessage(text, error = false) {
    $("editMessage").textContent = text;
    $("editMessage").classList.toggle("error", error);
  }
  async function checkFile() {
    const submitted = { path: state.file.path, text: $("codeEditor").value };
    const r = await api("/api/project/check", {
      ...submitted,
    });
    if (
      state.file?.path !== submitted.path ||
      $("codeEditor").value !== submitted.text
    )
      return false;
    editMessage(r.passed ? r.checker + " 통과" : r.message, !r.passed);
    return r.passed;
  }
  function diff() {
    const a = state.file.text.split("\n"),
      b = $("codeEditor").value.split("\n");
    let prefix = 0;
    while (prefix < a.length && prefix < b.length && a[prefix] === b[prefix])
      prefix++;
    let suffix = 0;
    while (
      suffix < a.length - prefix &&
      suffix < b.length - prefix &&
      a[a.length - 1 - suffix] === b[b.length - 1 - suffix]
    )
      suffix++;
    const old = a.slice(prefix, a.length - suffix),
      next = b.slice(prefix, b.length - suffix);
    $("diff").innerHTML =
      old.length || next.length
        ? `<p class="matrixnote">${prefix + 1}번째 줄부터 변경 · 지운 줄 ${old.length} / 추가한 줄 ${next.length}</p>` +
          old.map((t) => `<div class="removed">− ${esc(t)}</div>`).join("") +
          next.map((t) => `<div class="added">+ ${esc(t)}</div>`).join("")
        : "변경 사항이 없습니다.";
    $("diff").classList.remove("hidden");
  }
  async function saveFile() {
    const submitted = { ...state.file, text: $("codeEditor").value };
    const path = submitted.path;
    state.pendingSaves ||= new Set();
    if (state.pendingSaves.has(path)) return;
    state.pendingSaves.add(path);
    let r;
    try {
      r = await api("/api/project/save", {
        path,
        text: submitted.text,
        expected_sha256: submitted.sha256,
      });
    } finally {
      state.pendingSaves.delete(path);
    }
    if (!r.saved) {
      if (state.file?.path === path) editMessage(r.validation.message, true);
      return;
    }
    state.edits.set(path, r.edit_id);
    state.fileRevisions ||= new Map();
    state.fileRevisions.set(path, (state.fileRevisions.get(path) || 0) + 1);
    if (state.file?.path !== path) {
      if (state.drafts.get(path) === submitted.text) state.drafts.delete(path);
      return;
    }
    const currentText = $("codeEditor").value;
    if (currentText === submitted.text) state.drafts.delete(path);
    else state.drafts.set(path, currentText);
    state.file.text = submitted.text;
    state.file.sha256 = r.sha256;
    $("undoFile").disabled = false;
    $("editState").textContent =
      (currentText === submitted.text ? "저장됨" : "추가 편집 미저장") +
      " · SHA " +
      r.sha256.slice(0, 10);
    editMessage(
      r.applies +
        (currentText === submitted.text
          ? " 이전 내용도 로컬에 보관했습니다."
          : " 저장을 누른 뒤 입력한 내용은 아직 저장하지 않았습니다."),
    );
  }
  async function undoFile() {
    const path = state.file.path,
      submittedText = $("codeEditor").value,
      r = await api("/api/project/restore", {
        edit_id: state.edits.get(path),
        expected_sha256: state.file.sha256,
      });
    state.edits.delete(path);
    state.fileRevisions ||= new Map();
    state.fileRevisions.set(path, (state.fileRevisions.get(path) || 0) + 1);
    if (state.file?.path !== path) return;
    if ($("codeEditor").value === submittedText) state.drafts.delete(path);
    else state.drafts.set(path, $("codeEditor").value);
    await openFile(path, { discard: true });
    editMessage("직전 저장 이전의 내용으로 되돌렸습니다. " + r.applies);
  }
  function learn() {
    $("toolCount").textContent = state.catalog.tools.length;
    $("toolList").innerHTML = state.catalog.tools
      .map(
        (t) =>
          `<button data-tool="${esc(t.name)}"><code>${esc(t.name)}</code><small>${esc(t.label)}</small></button>`,
      )
      .join("");
    const components = state.catalog.components;
    $("componentCards").innerHTML = components
      .map(
        (c) =>
          `<article class="componentcard"><header><h3>${esc(c.label)}</h3><span class="pill">${c.status === "implemented" ? "구현됨" : "미구현"}</span></header><p>${esc(c.purpose)}</p><details><summary>언제 동작하나 · 확인할 것</summary><p>${esc(c.when)}</p><p>${esc(c.exercise)}</p></details>${c.edit_path ? `<button data-file="${esc(c.edit_path)}">관련 파일 열기 ↗</button>` : '<span class="unavailable">현재 전용 클라이언트가 없습니다.</span>'}</article>`,
      )
      .join("");
  }
  async function loadFeedback() {
    const id = $("feedbackRun").value || state.run?.id;
    const ticket = (state.feedbackTicket || 0) + 1;
    state.feedbackTicket = ticket;
    if (!id) {
      $("feedbackReport").innerHTML =
        '<p class="note">먼저 요청을 실행하거나 실행 기록을 선택하세요.</p>';
      return;
    }
    const data = await api("/api/feedback/" + id);
    if (state.feedbackTicket !== ticket) return;
    state.feedbackCurrent = data;
    const sampleNotice = data.sample
      ? `<div class="samplebanner"><strong>실제 저장 기록 재생</strong><p>${esc(data.sample.description)}</p></div>`
      : "";
    const rows = data.memory?.feedback || [];
    $("feedbackReport").innerHTML =
      sampleNotice +
      stats([
        ["이번 실행의 보완 지적", data.repairs.length],
        ["저장된 지적", data.memory ? data.memory.stored_feedback : "—"],
        [
          "이번 입력에서 확인",
          data.memory ? data.memory.included_feedback : "—",
        ],
        ["가중치 재학습", "실행 단계 없음"],
      ]) +
      `<p class="note">${esc(data.model || "로컬 모델")} · ${esc(data.prompt)}</p>` +
      data.repairs
        .map(
          (r, i) =>
            `<section class="repaircard"><header><strong>${esc(r.agent_name || "주 에이전트")} · 보완 ${i + 1}</strong><span class="pill">${r.next_check?.passed ? "후속 검사 통과" : r.next_check ? "추가 보완 필요" : "후속 검사 기록 확인"}</span></header><div class="repairgrid">${box("확정하지 않은 실제 모델 답변", r.rejected_answer)}${box("다음 입력에 추가한 지적", r.correction)}</div><div class="evidencerow"><span>${r.feedback_in_next_request ? "✓ 다음 model.request에 같은 지적이 포함됨" : "다음 입력 포함을 아직 확인하지 못함"}</span>${r.next_request_event ? `<button class="quiet" data-feedback-event="${r.next_request_event}">실제 입력 #${r.next_request_event} →</button>` : ""}</div><h3>그 뒤 관측한 행동</h3>${r.observed_tools.map((t) => `<button class="evidencebutton" data-feedback-event="${t.event_id}">${esc(t.title)} <code>#${t.event_id} →</code></button>`).join("") || '<p class="note">후속 도구 결과가 없습니다.</p>'}</section>`,
        )
        .join("") +
      (data.repairs.length
        ? ""
        : '<div class="notice">이 실행에는 보완 지적이 없었습니다. 개선 과정을 임의로 만들어 표시하지 않습니다.</div>') +
      `<section class="memoryaudit"><h2>과거 지적 · 저장과 입력을 대조</h2><p class="note">${esc(data.memory?.unavailable_reason || (!data.memory ? "이전 형식의 기록에는 기억 대조 정보가 없습니다." : "저장한 지적 문자열이 실제 messages에 포함됐는지 대조했습니다."))}</p>${rows.map((row) => `<details class="memoryrow"><summary><span class="pill ${row.in_model_input ? "included" : "omitted"}">${row.in_model_input ? "입력에 포함됨" : "저장됨 · 이번 입력에는 없음"}</span><code>${row.run_id.slice(0, 8)} #${row.event_id}</code></summary><p class="note">${esc(row.reason)}${row.message_indices.length ? " · 메시지 " + row.message_indices.join(", ") : ""}${!row.complete ? " · 저장한 발췌문 기준" : ""}</p>${box("저장한 지적", row.text)}</details>`).join("")}</section><button id="prepareRecall" class="secondary">이 요청으로 재실험 준비 · 지적 재포함 켜기</button><p class="note">${esc(data.note)}</p>`;
    $("prepareRecall").onclick = action(async () => {
      if (busy()) throw Error("현재 실행이 끝난 뒤 준비하세요.");
      state.session = data.sample
        ? "sample-retry-" + crypto.randomUUID().slice(0, 12)
        : data.session_id;
      Object.keys(features).forEach(
        (key) => (features[key] = data.features[key] ?? false),
      );
      features.memory = true;
      features.feedback_recall = true;
      $("prompt").value = data.prompt;
      $("requirements").value = JSON.stringify(data.requirements, null, 2);
      view("journey");
      $("journeyView").classList.remove("focus");
      $("configuration").open = true;
      featureUI();
      toast(
        "요청과 대화를 불러왔습니다. 기억 재포함 설정을 바꿔 실행할 수 있습니다.",
      );
    });
  }
  const experimentNotes = {
    agent_deep:
      "같은 모델과 하네스에서 호출 예산과 추가 점검을 바꿉니다. 실제 점검 메시지와 늘어난 호출·토큰을 확인합니다.",
    agent_team:
      "같은 요청을 독립 하위 작업과 대화로 나누고 결과를 합칩니다. 모델 대기·권한·주 대화로 돌아온 보고와 전체 비용을 확인합니다.",
    mcp_tools:
      "기본 하네스를 같게 유지하고 MCP 연결만 바꿉니다. 공개한 도구 목록·원본 RPC·입력 토큰·실제 도구 사용 여부를 비교합니다.",
    lsp_diagnostics:
      "같은 Python 파일을 구문 검사와 실제 언어 서버로 확인합니다. 모델이 스스로 문제를 추측한 경우와 언어 서버가 반환한 근거를 구분합니다.",
    arithmetic:
      "A는 지침·도구 없이, B는 하네스를 연결해 실행합니다. 답이 맞는지와 실제 도구를 썼는지를 따로 확인합니다.",
    grounding:
      "파일에만 있는 값을 확인하는 예제입니다. 같은 요청으로 실제 파일 읽기와 답변의 차이를 확인합니다.",
    kv_cache:
      "두 실행의 메시지는 같습니다. A는 접두부 재사용을 끄고 B는 켭니다. 생성 중 KV는 둘 다 사용합니다.",
    decode_kv:
      "교육용 실행기에서 같은 입력을 실행합니다. A는 매 토큰마다 전체를 다시 계산하고 B는 KV를 사용합니다. CPU에서 시간이 걸릴 수 있습니다.",
  };
  async function startCompare() {
    $("startCompare").disabled = true;
    $("compareResult").innerHTML =
      '<p class="compareprogress">비교 실행을 준비하고 있습니다.</p>';
    const id = $("experimentCase").value;
    if (id === "decode_kv" && state.health.backend !== "reference")
      throw Error("교육용 실행기를 선택하세요.");
    const cases = await api("/api/experiments/cases");
    const base = Object.fromEntries(
      Object.keys(features).map((k) => [k, false]),
    );
    const payload =
      id === "decode_kv"
        ? {
            case_id: id,
            prompt: "한국어로 안녕이라고만 답해.",
            left: base,
            right: base,
            left_engine: {
              ...engine(),
              max_tokens: 12,
              use_kv: false,
              cache_prompt: false,
              temperature: 0,
            },
            right_engine: {
              ...engine(),
              max_tokens: 12,
              use_kv: true,
              cache_prompt: false,
              temperature: 0,
            },
          }
        : {
            case_id: id,
            prompt: cases[id].prompt,
            left_engine: { ...engine(), cache_prompt: false },
            right_engine: { ...engine(), cache_prompt: id === "kv_cache" },
          };
    if (id === "kv_cache") {
      payload.left = base;
      payload.right = base;
    }
    const r = await api("/api/experiments", payload);
    state.experiment = r.id;
    $("stopCompare").classList.remove("hidden");
    $("startCompare").disabled = true;
    await pollCompare();
  }
  async function pollCompare() {
    if (!state.experiment) return;
    const item = await api("/api/experiments/" + state.experiment),
      results = item.results || {};
    $("compareResult").innerHTML =
      (item.status === "running"
        ? `<p class="compareprogress">${item.phase} 실행 중 · ${num(item.progress?.events)}단계 관측</p>`
        : "") +
      `<div class="comparison">${["A", "B"]
        .map((label) => {
          const r = results[label];
          return `<div><h3>${label} · ${r ? r.status : "대기"}</h3>${
            r
              ? `<p>${esc(r.error || r.answer)}</p><dl>${[
                  ["전체 모델 호출", r.metrics.model_calls],
                  ["하위 에이전트", r.metrics.subagents],
                  ["계산 차례 대기 ms", r.metrics.model_queue_ms],
                  ["도구 실행", r.metrics.tool_calls],
                  ["공개 도구", r.metrics.tool_definitions],
                  ["MCP 전송·수신", r.metrics.mcp_messages],
                  ["LSP 전송·수신", r.metrics.lsp_messages],
                  ["입력 토큰", r.metrics.prompt_tokens],
                  ["출력 토큰", r.metrics.completion_tokens],
                  ["접두부 재사용", r.metrics.cached_tokens],
                  ["새 입력", r.metrics.fresh_prompt_tokens],
                  ["prefill ms", r.metrics.prefill_ms],
                  ["decode ms", r.metrics.decode_ms],
                  ["forward 입력 누적", r.metrics.forward_tokens],
                  ["실행 초", r.metrics.active_seconds],
                ]
                  .map(([k, v]) => `<dt>${k}</dt><dd>${num(v)}</dd>`)
                  .join(
                    "",
                  )}</dl>${r.evaluation?.criteria.map((c) => `<p>${c.passed ? "✓" : "×"} ${esc(c.label)}</p>`).join("") || ""}<button data-run="${r.run_id}">이 실행의 전체 흐름 →</button>`
              : "<p>결과를 기다리고 있습니다.</p>"
          }</div>`;
        })
        .join("")}</div>` +
      (item.error ? `<p class="note">${esc(item.error)}</p>` : "") +
      (item.controls
        ? `<div class="checks">${Object.entries(item.controls)
            .map(
              ([k, v]) =>
                `${v ? "✓" : "×"} ${esc({ same_prompt: "같은 요청", same_model: "같은 모델", same_generation: "같은 생성 설정", same_workspace: "같은 작업 파일", isolated_sessions: "분리한 대화 이력", same_model_input: "동일한 모델 입력" }[k] || k)}`,
            )
            .join(
              " · ",
            )}<p>${esc(item.note)}</p><p class="note">Python 실행기의 prefill/decode는 사용자 정지를 뺀 forward 벽시간이며 텐서 추출·hook 기록 비용을 포함합니다. 모델 계산만의 벤치마크가 아닙니다.</p>${item.generation_diff && Object.keys(item.generation_diff).length ? box("다른 실행 설정 (A / B)", item.generation_diff) : ""}</div>`
        : "");
    if (item.status === "running")
      setTimeout(() => pollCompare().catch((e) => toast(e.message)), 1200);
    else {
      state.experiment = null;
      $("startCompare").disabled = false;
      $("stopCompare").classList.add("hidden");
      await history();
      await health();
    }
  }
  function stopPlay() {
    clearInterval(state.playing);
    state.playing = null;
    $("play").textContent = "▶";
  }
  document.addEventListener(
    "click",
    action(async (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.dataset.memoryEvent) {
        view("journey");
        state.live = false;
        select(
          state.events.findIndex((e) => e.id === Number(b.dataset.memoryEvent)),
          { open: true },
        );
      }
      if (b.dataset.activity) {
        const action = b.dataset.activity,
          run = state.activity.run,
          experiment = state.activity.experiment;
        if (action === "open") {
          if (run) {
            await loadRun(run.id);
            state.live = true;
            select(state.events.length - 1);
          }
          if (experiment) {
            state.experiment = experiment.id;
            view("learn");
            await pollCompare();
          }
        } else if (experiment) {
          await api("/api/experiments/" + experiment.id + "/stop", {});
          await refreshActivity();
        } else if (run) {
          await api("/api/runs/" + run.id + "/control", { action });
          await refreshActivity();
          if (state.run?.id === run.id) {
            state.live = true;
            await poll(run.id);
          }
          toast(
            action === "stop"
              ? "중지를 요청했습니다."
              : action === "continue"
                ? "계속 실행합니다."
                : "다음 단계를 실행합니다.",
          );
        }
      }
      if (b.dataset.agent) {
        state.agentFilter = b.dataset.agent;
        if (state.agentFilter !== "all") {
          state.live = false;
          const at = state.events.findLastIndex(
            (e) => (e.agent_id || rootAgentId()) === state.agentFilter,
          );
          if (at >= 0) select(at, { open: true });
        }
        renderTimeline();
        renderAgents();
      }
      if (b.dataset.inspectionEvent) {
        const at = state.events.findIndex(
          (event) => event.id === Number(b.dataset.inspectionEvent),
        );
        if (at >= 0) {
          state.live = false;
          stopPlay();
          state.tab = b.dataset.inspectionTab || "input";
          state.inputMessage = Number(b.dataset.inspectionMessage ?? -1);
          select(at, { open: true });
        }
      }
      if (b.dataset.view) view(b.dataset.view);
      if (b.dataset.feedbackEvent) {
        const id = state.feedbackCurrent.run_id;
        if (state.run?.id !== id) await loadRun(id);
        view("journey");
        state.live = false;
        select(
          state.events.findIndex(
            (e) => e.id === Number(b.dataset.feedbackEvent),
          ),
          { open: true },
        );
      }
      if (b.dataset.extension) {
        const at = state.events.findIndex(
          (e) => e.actor === b.dataset.extension,
        );
        if (at >= 0) {
          state.live = false;
          select(at, { open: true });
        }
      }
      if (b.dataset.file) await openFile(b.dataset.file);
      if (b.dataset.run) await loadRun(b.dataset.run);
      if (b.dataset.tool) {
        const tool = state.catalog.tools.find((t) => t.name === b.dataset.tool);
        if (tool) {
          $("detailEmpty").classList.add("hidden");
          $("detailBody").classList.remove("hidden");
          $("componentNavigator").classList.add("hidden");
          clearInspectionExtras();
          $("detailPath").classList.add("hidden");
          $("sourcePeek").classList.add("hidden");
          $("detailTitle").textContent = tool.name;
          $("detailIndex").textContent = "현재 정의";
          $("detailExplanation").textContent =
            "현재 등록된 도구 정의입니다. 실행 기록의 코드 히트와 구분합니다.";
          $("detailKind").textContent = "도구 정의 · 현재 적용된 하네스";
          $("detailActor").textContent = "등록된 Python 함수";
          $("detailContent").innerHTML =
            box("모델에 공개하는 JSON schema", tool.schema) +
            box("실제 구현 함수", tool.source.code) +
            `<button id="editTool" class="secondary">구현 파일 열기 ↗</button>`;
          $("editTool").onclick = action(() => openFile(tool.source.file));
          $("inspector").classList.add("open");
        }
      }
      if (b.dataset.example) {
        if (b.dataset.example === "team") {
          $("agentMode").value = "team";
          modeUI();
        }
        $("prompt").value = examples[b.dataset.example];
        observeInput();
        features.writes = b.dataset.example === "write";
        features.mcp = b.dataset.example === "mcp";
        features.lsp = b.dataset.example === "lsp";
        features.planning = b.dataset.example === "plan";
        if (b.dataset.example !== "rules") features.tools = true;
        if (b.dataset.example === "skill") features.skills = true;
        $("requirements").value = JSON.stringify(
          exampleRequirements[b.dataset.example],
          null,
          2,
        );
        featureUI();
        $("prompt").focus();
      }
      if (b.dataset.event) {
        state.live = false;
        stopPlay();
        select(
          state.events.findIndex((e) => e.id === Number(b.dataset.event)),
          { open: true },
        );
      }
      if (b.dataset.stage) {
        const index = state.events.findIndex(
          (e) => stage(e) === b.dataset.stage,
        );
        if (index >= 0) {
          state.live = false;
          select(index, { open: true });
        }
      }
      if (b.dataset.detail) {
        state.tab = b.dataset.detail;
        renderDetail();
      }
    }),
  );
  $("timeline").addEventListener("click", (e) => {
    const group = e.target.closest(".chapter>summary")?.parentElement;
    if (group) {
      state.openGroups ??= new Set();
      if (group.open) state.openGroups.delete(group.dataset.group);
      else state.openGroups.add(group.dataset.group);
    }
    const d = e.target.closest(".tokenstep>summary")?.parentElement;
    if (d) {
      state.openSteps ??= new Set();
      if (d.open) state.openSteps.delete(d.dataset.step);
      else state.openSteps.add(d.dataset.step);
    }
  });
  $("features").addEventListener("change", (e) => {
    if (e.target.dataset.feature) {
      features[e.target.dataset.feature] = e.target.checked;
      featureUI();
    }
  });
  $("requirements").oninput = featureUI;
  $("baseline").onclick = () => {
    Object.keys(features).forEach((k) => (features[k] = false));
    featureUI();
  };
  $("full").onclick = () => {
    Object.keys(features).forEach(
      (k) =>
        (features[k] = [
          "rules",
          "memory",
          "skills",
          "tools",
          "commands",
        ].includes(k)),
    );
    featureUI();
  };
  $("backend").onchange = action(async () => {
    await api("/api/backend", { backend: $("backend").value });
    await health();
  });
  $("useKv").onchange = () => {
    if (!$("useKv").checked) $("prefixCache").checked = false;
  };
  $("prompt").oninput = observeInput;
  $("promptForm").onsubmit = action(submit);
  $("prompt").onkeydown = action((e) => {
    if (e.ctrlKey && e.key === "Enter") return submit(e);
  });
  for (const key of ["next", "continue", "stop"])
    $(key).onclick = action(async () => {
      await api("/api/runs/" + state.run.id + "/control", { action: key });
      state.live = true;
      await poll(state.run.id);
    });
  $("scrubber").oninput = () => {
    state.live = false;
    stopPlay();
    select(Number($("scrubber").value));
  };
  $("first").onclick = () => {
    state.live = false;
    select(0);
  };
  $("prev").onclick = () => {
    state.live = false;
    select(state.cursor - 1);
  };
  $("nextEvent").onclick = () => {
    state.live = false;
    select(state.cursor + 1);
  };
  $("live").onclick = () => {
    stopPlay();
    state.live = true;
    select(state.events.length - 1);
  };
  $("play").onclick = () => {
    if (state.playing) {
      stopPlay();
      return;
    }
    if (!state.events.length) return;
    state.live = false;
    if (state.cursor >= state.events.length - 1) select(0);
    $("play").textContent = "Ⅱ";
    state.playing = setInterval(() => {
      if (state.cursor >= state.events.length - 1) {
        stopPlay();
        return;
      }
      select(state.cursor + 1);
    }, 650);
  };
  $("eventSearch").oninput = renderTimeline;
  $("export").onclick = () => {
    if (state.run)
      window.location.assign("/api/runs/" + state.run.id + "/export");
  };
  $("openDetail").onclick = () => $("inspector").classList.add("open");
  $("showInput").onclick = () => {
    state.tab = "input";
    state.inputMessage = -1;
    renderDetail();
    $("inspector").classList.add("open");
  };
  $("zoomIn").onclick = () => AgentArchitecture.scale(1);
  $("zoomOut").onclick = () => AgentArchitecture.scale(-1);
  $("zoomFit").onclick = () => AgentArchitecture.fit();
  $("toggleRecords").onclick = () => {
    document.querySelector(".execution").classList.toggle("records-open");
    renderTimeline();
  };
  $("showAnswer").onclick = () => {
    clearInspectionExtras();
    $("componentNavigator").classList.add("hidden");
    $("detailPath").classList.add("hidden");
    $("detailEmpty").classList.add("hidden");
    $("detailBody").classList.remove("hidden");
    $("sourcePeek").classList.add("hidden");
    document.querySelector(".detailtabs").classList.add("hidden");
    $("detailTitle").textContent = state.run?.error ? "실행 오류" : "답변";
    $("detailActor").textContent = state.run?.sample
      ? "저장 기록"
      : "모델 응답";
    $("detailKind").textContent = "";
    $("detailExplanation").textContent = "";
    $("detailIndex").textContent = "";
    $("detailContent").innerHTML = '<div class="answer-readout"></div>';
    $("detailContent").firstChild.textContent =
      state.run?.error || state.run?.answer || "";
    $("inspector").classList.add("open");
  };
  $("closeDetail").onclick = () => $("inspector").classList.remove("open");
  $("menu").onclick = () => $("project").classList.add("open");
  $("closeProject").onclick = () => $("project").classList.remove("open");
  $("help").onclick = () => $("guide").showModal();
  $("closeGuide").onclick = () => $("guide").close();
  $("newSession").onclick = () => {
    state.session = "session-" + crypto.randomUUID().slice(0, 12);
    toast(
      "새 대화에서 시작합니다. 이전 대화 이력은 다음 입력에 넣지 않습니다.",
    );
    $("journeyView").classList.remove("focus");
    saveDraft();
  };
  $("editPrompt").onclick = () => {
    if (state.run?.prompt) {
      $("agentMode").value = state.run.mode || "single";
      modeUI();
      $("prompt").value = state.run.prompt;
      $("requirements").value = JSON.stringify(
        state.run.requirements || [],
        null,
        2,
      );
      Object.keys(features).forEach(
        (key) => (features[key] = state.run.features?.[key] ?? false),
      );
      featureUI();
    }
    $("journeyView").classList.remove("focus");
    $("prompt").focus();
  };
  function modeUI() {
    const mode = $("agentMode").value;
    if (mode === "team") {
      features.tools = true;
      featureUI();
    }
    $("modeNote").textContent = {
      single: "한 대화에서 모델과 도구를 반복 호출합니다. 최대 8회 호출합니다.",
      plan: "자료를 확인하고 계획을 작성합니다. 쓰기 스위치를 켰어도 하네스가 파일 수정 도구를 비공개로 바꿉니다.",
      deep: "같은 에이전트가 최대 16회 작업하고 최종 답변을 한 번 더 점검합니다. 모델 자체의 추론 강도를 바꾸는 설정은 아닙니다.",
      team: "주 에이전트가 작업을 나누고 최대 3개의 독립 하위 대화가 결과를 돌려줍니다. 같은 로컬 모델의 계산 차례를 공유합니다.",
    }[mode];
  }
  $("agentMode").onchange = modeUI;
  $("agentFocus").onchange = () => {
    state.agentFilter = $("agentFocus").value;
    if (state.agentFilter !== "all") {
      state.live = false;
      const at = state.events.findLastIndex(
        (e) => (e.agent_id || rootAgentId()) === state.agentFilter,
      );
      if (at >= 0) select(at, { open: true });
    }
    renderTimeline();
    renderAgents();
  };
  $("reloadFiles").onclick = action(loadProject);
  $("reloadMemory").onclick = action(loadMemory);
  $("refreshHistory").onclick = action(history);
  $("codeEditor").oninput = () => {
    editorLines();
    if ($("codeEditor").value === state.file.text)
      state.drafts.delete(state.file.path);
    else state.drafts.set(state.file.path, $("codeEditor").value);
    $("editState").textContent = "저장 전 변경 · " + state.file.path;
  };
  $("codeEditor").onscroll = () =>
    ($("lineNumbers").scrollTop = $("codeEditor").scrollTop);
  $("codeEditor").onkeydown = (e) => {
    if (e.key === "Tab" && !e.target.readOnly) {
      e.preventDefault();
      const t = e.target,
        a = t.selectionStart,
        b = t.selectionEnd;
      t.setRangeText("    ", a, b, "end");
      t.dispatchEvent(new Event("input"));
    }
  };
  $("checkFile").onclick = action(checkFile);
  $("showDiff").onclick = diff;
  $("saveFile").onclick = action(saveFile);
  $("undoFile").onclick = action(undoFile);
  $("experimentCase").onchange = () =>
    ($("experimentDescription").textContent =
      experimentNotes[$("experimentCase").value]);
  $("loadFeedback").onclick = action(loadFeedback);
  $("feedbackRun").onchange = action(loadFeedback);
  $("startCompare").onclick = action(startCompare);
  $("stopCompare").onclick = action(() =>
    api("/api/experiments/" + state.experiment + "/stop", {}),
  );
  window.addEventListener("beforeunload", (e) => {
    if (
      state.drafts.size ||
      (state.file && $("codeEditor").value !== state.file.text)
    ) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
  async function init() {
    restoreDraft();
    const execution = document.querySelector(".execution"),
      tray = document.createElement("div");
    tray.className = "recordtray";
    tray.append(document.querySelector(".eventtoolbar"), $("timeline"));
    execution.append(tray);
    AgentArchitecture.init($("architecture"), { select: selectComponent });
    featureUI();
    renderRoute();
    $("experimentCase").onchange();
    const results = await Promise.allSettled([
      health(),
      loadProject(),
      history(),
      api("/api/implementation"),
      loadSamples(),
    ]);
    if (results[3].status === "fulfilled") {
      state.catalog = results[3].value;
      learn();
    }
    for (const r of results)
      if (r.status === "rejected") toast(r.reason.message);
    state.ready = true;
    modeUI();
    const selection = stored("selection"),
      activity = await refreshActivity({ recover: true });
    if (!activity.run && !activity.experiment) {
      if (selection?.id) {
        try {
          await loadRun(selection.id);
          select(selection.cursor || 0);
        } catch {
          store("selection", null);
        }
      } else if (state.health.mode === "sample")
        await loadRun("sample-verified-write");
    }
    renderArchitecture();
    document.addEventListener("input", (e) => {
      if (e.target.closest("#promptForm,#configuration")) saveDraft();
    });
    document.addEventListener("change", saveDraft);
    setInterval(() => health().catch(() => {}), 5000);
    setInterval(() => refreshActivity().catch(() => {}), 1500);
  }
  init().catch((e) => toast(e.message));
})();
