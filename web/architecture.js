/* 실행 기록을 시스템 구성과 연결한다. 연결선은 처리 중인 데이터의 경계다. */
((scope) => {
  "use strict";
  const nodes = [
    ["browser", 24, 75, 148, 62, "브라우저", "프롬프트 · 전송"],
    ["api", 218, 75, 140, 62, "요청 수신", "HTTP · 작업 스레드"],
    ["reader", 218, 175, 140, 62, "파일 로더", "경로 · 읽기 · UTF-8"],
    ["context", 420, 75, 174, 62, "입력 구성", "system · user · tool"],
    ["compaction", 420, 170, 174, 46, "컨텍스트 압축", "크기 확인 · 생략 · 요약"],
    ["rules", 24, 225, 148, 46, "AGENTS.md", "프로젝트 규칙"],
    ["memory", 24, 284, 148, 46, "대화 · resume", "저장한 맥락"],
    ["skills", 24, 343, 148, 46, "skills/", "목록 → 본문"],
    ["commands", 24, 402, 148, 46, "commands/", "/명령어 확장"],
    ["loop", 420, 231, 174, 72, "에이전트 루프", "답변인가, 도구 요청인가"],
    ["gate", 218, 260, 140, 62, "실행 검사", "도구 · 인자 · 경로"],
    ["tools", 218, 397, 140, 62, "도구 함수", "읽기 · 쓰기 · 검색 · 검사"],
    ["agents", 420, 397, 174, 62, "하위 에이전트", "별도 대화 · 제한한 도구"],
    ["result", 420, 506, 174, 62, "완료 검사 · 저장", "조건 확인 → 최종 답변"],
    ["output", 24, 506, 148, 62, "화면 출력", "최종 응답"],
    ["tokenizer", 700, 75, 168, 62, "템플릿 · 토큰화", "문자열 → 토큰 ID"],
    ["embedding", 902, 75, 168, 62, "임베딩", "ID → 벡터"],
    ["weights", 700, 175, 168, 58, "모델 가중치", "로컬 모델 파일"],
    ["forward", 902, 187, 168, 108, "모델 연산", "Q/K/V · Attention"],
    ["cache", 700, 265, 168, 62, "KV 캐시", "과거 위치의 K · V"],
    ["sample", 902, 358, 168, 62, "다음 토큰 선택", "logits → 분포 → ID"],
    ["response", 700, 358, 168, 62, "응답 어댑터", "하네스 · 응답 조립"],
    ["mcp", 700, 506, 168, 62, "MCP 서버", "별도 프로세스의 도구"],
    ["lsp", 902, 506, 168, 62, "언어 서버 · LSP", "진단 · 심볼 · 정의"],
  ].map(([id, x, y, w, h, title, description]) => ({
    id,
    x,
    y: y * 0.7,
    w,
    h: h <= 46 ? 35 : h === 108 ? 88 : 52,
    title,
    description,
  }));
  const edges = [
    ["input", "M172 106 H218"],
    ["api-context", "M358 106 H420"],
    ["api-reader", "M288 137 V175"],
    ["rules-read", "M172 248 H190 V206 H218"],
    ["memory-read", "M172 307 H190 V206 H218"],
    ["skills-read", "M172 366 H190 V206 H218"],
    ["commands-read", "M172 425 H190 V206 H218"],
    ["read-context", "M358 206 H390 V106 H420"],
    ["context-loop", "M507 137 V231"],
    ["loop-model", "M594 255 H650 V106 H700"],
    ["token-embed", "M868 106 H902"],
    ["embed-forward", "M986 137 V187"],
    ["weights-forward", "M868 204 H902"],
    ["forward-kv", "M902 271 H868"],
    ["kv-forward", "M868 308 H887 V288 H902"],
    ["forward-sample", "M986 295 V358"],
    ["sample-response", "M902 389 H868"],
    ["response-loop", "M700 389 H650 V279 H594"],
    ["loop-gate", "M420 280 H358"],
    ["gate-tool", "M288 322 V397"],
    ["tool-reader", "M218 420 H202 V222 H218"],
    ["tool-context", "M358 428 H378 V120 H420"],
    ["loop-agents", "M507 303 V397"],
    ["agents-context", "M594 428 H621 V91 H594"],
    ["tool-mcp", "M288 459 V462 H832 V506"],
    ["tool-lsp", "M288 459 V462 H986 V506"],
    ["loop-result", "M594 280 H610 V537 H594"],
    ["result-output", "M420 537 H172"],
    ["memory-compact", "M172 307 H378 V194 H420"],
    ["compact-model", "M594 194 H635 V106 H700"],
    ["compact-context", "M507 170 V137"],
    ["mcp-tool", "M832 506 V462 H288 V459"],
    ["lsp-tool", "M986 506 V462 H288 V459"],
  ].map(([id, path]) => [
    id,
    path
      .replace(
        /([ML])([\d.]+) ([\d.]+)/g,
        (_, command, x, y) => command + x + " " + Number(y) * 0.7,
      )
      .replace(/V([\d.]+)/g, (_, y) => "V" + Number(y) * 0.7),
  ]);
  const definitions = {
    compaction: [
      "입력이 커지면 줄입니다. 요청 사이에는 이전 대화를 요약해 저장하고, 요청 처리 중에는 오래된 도구 결과의 본문을 빼거나 앞선 작업을 요약합니다. 원본은 남고 모델에 보낼 입력만 달라집니다.",
      "local_agent_lab/compaction.py",
    ],
    browser: [
      "브라우저가 입력 문자열과 실행 설정을 JSON으로 전송합니다.",
      "web/workbench.js",
    ],
    api: [
      "서버가 요청 본문을 읽고 Run과 작업 스레드를 만듭니다. 새로고침해도 이 작업은 남아 있습니다.",
      "local_agent_lab/web.py",
    ],
    reader: [
      "Python이 경로를 확인하고 파일을 열어 바이트를 읽습니다. UTF-8 해석 후 문자열이 됩니다.",
      "local_agent_lab/files.py",
    ],
    rules: [
      "하네스가 규칙 파일을 읽어 system 메시지에 넣습니다. 자연어 지침은 실행 권한 검사와 별개입니다.",
      "demo_workspace/AGENTS.md",
    ],
    memory: [
      "저장된 대화와 resume.md를 선택해 다시 입력합니다. 중단된 실행을 이어가기 위한 상태 파일도 여기에 속합니다. 텍스트 이력과 KV 캐시는 별개입니다.",
      "local_agent_lab/harness.py",
    ],
    skills: [
      "목록의 이름·설명을 먼저 공개합니다. 모델이 load_skill을 요청하면 본문을 전달합니다.",
      "demo_workspace/skills/file-summary/SKILL.md",
    ],
    commands: [
      "/명령어를 프롬프트 템플릿으로 바꿉니다. 셸 명령을 실행하는 기능은 아닙니다.",
      "demo_workspace/commands/summary.md",
    ],
    context: [
      "규칙·이력·사용자 요청·도구 결과를 역할별 messages로 조립합니다.",
      "local_agent_lab/context.py",
    ],
    loop: [
      "모델 출력을 해석합니다. 도구 요청이면 검사와 실행을 거쳐 결과를 추가하고 다시 모델을 호출합니다.",
      "local_agent_lab/agent_loop.py",
    ],
    gate: [
      "모델이 만든 도구 이름·인자·권한·경로를 Python 코드로 검사합니다.",
      "local_agent_lab/policy.py",
    ],
    tools: [
      "등록된 실제 함수를 호출합니다. 함수의 반환값이 다음 모델 요청의 tool 메시지에 들어갑니다.",
      "local_agent_lab/toolkit.py",
    ],
    agents: [
      "주 에이전트가 작업을 나누면 독립 messages와 제한된 도구를 가진 하위 실행이 생성됩니다. 모델 계산 차례는 공유합니다.",
      "demo_workspace/agents.json",
    ],
    result: [
      "수정 파일과 명시한 완료 조건을 검사한 뒤 답변·이력을 저장합니다.",
      "local_agent_lab/policy.py",
    ],
    output: [
      "확정한 답변을 브라우저 본문에 반영합니다. 저장 기록을 탐색할 때는 새 계산을 실행하지 않습니다.",
      "web/workbench.js",
    ],
    tokenizer: [
      "대화 템플릿을 적용하고 문자열을 tokenizer로 ID 배열로 바꿉니다. 문자 수와 토큰 수는 다릅니다.",
      "local_agent_lab/inference.py",
    ],
    embedding: [
      "토큰 ID에 해당하는 실제 임베딩 벡터를 계산합니다. 교육용 실행기에서 수치 표본을 관찰합니다.",
      "local_agent_lab/reference_engine.py",
    ],
    weights: [
      "다운로드한 모델 가중치를 메모리에 불러와 연산에 사용합니다. 이 프로젝트의 추론 경로는 가중치를 재학습하지 않습니다.",
      "local_agent_lab/reference_engine.py",
    ],
    forward: [
      "PyTorch/Transformers 또는 llama.cpp가 신경망을 계산합니다. Q/K/V·attention 표본은 교육용 실행기에서만 제공합니다.",
      "local_agent_lab/reference_engine.py",
    ],
    cache: [
      "과거 위치의 K/V를 보관해 반복 계산을 줄입니다. 요청 간 접두부 재사용과 생성 중 KV 사용은 별도 설정입니다.",
      "local_agent_lab/reference_engine.py",
    ],
    sample: [
      "다음 토큰의 logits에서 설정한 분포와 선택 규칙으로 ID를 고릅니다. 숨겨진 사고를 해독하는 단계는 아닙니다.",
      "local_agent_lab/reference_engine.py",
    ],
    response: [
      "생성된 ID와 전송 조각을 문자열·응답 객체로 조립하고 도구 호출 형식을 해석합니다. SSE 조각과 토큰은 일대일이 아닙니다.",
      "local_agent_lab/inference.py",
    ],
    mcp: [
      "하네스의 MCP 클라이언트가 서버의 도구를 발견하고 schema를 등록한 뒤 JSON-RPC로 호출합니다.",
      "demo_workspace/integrations.json",
    ],
    lsp: [
      "Python 언어 서버에 문서와 버전을 전달하고 진단·심볼·정의를 받습니다. 일반 구문 검사와 다른 근거입니다.",
      "demo_workspace/integrations.json",
    ],
  };
  function locate(e) {
    if (!e) return { node: "browser", edges: [] };
    const k = e.kind || "",
      o = e.output || {},
      category = e.category;
    if (k.startsWith("memory.compaction"))
      return {
        node: "compaction",
        edges: k === "memory.compaction.start" ? ["memory-compact"] : [],
      };
    if (k === "memory.summary.include")
      return { node: "context", edges: ["compact-context"] };
    if (k === "context.check" || k === "context.prune")
      return { node: "compaction", edges: [] };
    if (k === "context.compact.result")
      return { node: "compaction", edges: ["compact-context"] };
    if (k.startsWith("context.compact"))
      return { node: "compaction", edges: [] };
    if (k.startsWith("checkpoint.")) return { node: "memory", edges: [] };
    if (k === "model.request" && e.model_purpose === "compaction")
      return { node: "compaction", edges: ["compact-model"] };
    if (k.startsWith("mcp."))
      return {
        node: "mcp",
        edges:
          k === "mcp.send"
            ? ["tool-mcp"]
            : k === "mcp.receive"
              ? ["mcp-tool"]
              : [],
      };
    if (k.startsWith("lsp."))
      return {
        node: "lsp",
        edges:
          k === "lsp.send"
            ? ["tool-lsp"]
            : k === "lsp.receive"
              ? ["lsp-tool"]
              : [],
      };
    if (k === "browser.input") return { node: "browser", edges: [] };
    if (k === "browser.serialize") return { node: "browser", edges: ["input"] };
    if (k === "browser.render")
      return { node: "output", edges: ["result-output"] };
    if (
      k.startsWith("http.body") ||
      k.startsWith("http.json") ||
      k === "thread.dispatch" ||
      k === "input.receive" ||
      k === "request.route"
    )
      return { node: "api", edges: ["input"] };
    if (k.startsWith("file.")) {
      const file = String(
        o.path ||
          o.relative_path ||
          e.input?.path ||
          e.input?.relative_path ||
          e.input?.requested_path ||
          "",
      )
        .replaceAll("\\", "/")
        .toLowerCase();
      const owner =
        category === "tools"
          ? "tools"
          : file.includes("skill")
            ? "skills"
            : file.includes("command")
              ? "commands"
              : file.includes("agent") && file.endsWith(".md")
                ? "rules"
                : category === "memory" ||
                    file.includes("resume") ||
                    file.includes("sessions/")
                  ? "memory"
                  : null;
      return {
        node: "reader",
        related: owner ? [owner] : [],
        edges:
          owner && ["file.open", "file.read"].includes(k)
            ? [owner === "tools" ? "tool-reader" : owner + "-read"]
            : [],
      };
    }
    if (k.startsWith("rules."))
      return {
        node: "rules",
        edges: k === "rules.wrap" ? ["read-context"] : [],
      };
    if (k.startsWith("skills."))
      return {
        node: "skills",
        edges: k === "skills.catalog" ? ["read-context"] : [],
      };
    if (k.startsWith("command"))
      return {
        node: "commands",
        edges: k === "command.expand" ? ["read-context"] : [],
      };
    if (k.startsWith("memory.save") || k === "memory.child")
      return { node: "result", edges: [] };
    if (k.startsWith("memory."))
      return {
        node: "memory",
        edges: k === "memory.feedback.recall" ? ["read-context"] : [],
      };
    if (k === "context.append")
      return { node: "context", edges: ["tool-context"] };
    if (k.startsWith("context."))
      return { node: "context", edges: ["read-context"] };
    if (k === "agent.delegate" || k === "agent.spawn")
      return { node: "agents", edges: ["loop-agents"] };
    if (k === "agent.join")
      return { node: "agents", edges: ["agents-context"] };
    if (k === "agent.queue" || k === "agent.compute")
      return { node: "loop", edges: [] };
    if (
      k === "agent.review" ||
      k === "agent.mode" ||
      k === "config.apply" ||
      k === "config.resolve" ||
      k === "run.resume" ||
      k === "harness.modules"
    )
      return { node: "loop", edges: [] };
    if (
      k.startsWith("tool.validate") ||
      k === "tool.dispatch" ||
      k === "tool.error"
    )
      return { node: "gate", edges: ["loop-gate"] };
    if (k.startsWith("tool.")) return { node: "tools", edges: ["gate-tool"] };
    if (k === "reference.embedding")
      return { node: "embedding", edges: ["token-embed"] };
    if (k.startsWith("reference.cache"))
      return {
        node: "cache",
        edges: k === "reference.cache.write" ? ["forward-kv"] : [],
      };
    if (k === "reference.sample")
      return { node: "sample", edges: ["forward-sample"] };
    if (
      k.startsWith("reference.forward") ||
      k.startsWith("reference.projection") ||
      k.startsWith("reference.attention")
    )
      return { node: "forward", edges: ["embed-forward"] };
    if (k.startsWith("reference.protocol"))
      return { node: "response", edges: ["response-loop"] };
    if (
      k.startsWith("model.template") ||
      k.startsWith("model.token") ||
      k.startsWith("model.detoken") ||
      k.startsWith("model.preview")
    )
      return { node: "tokenizer", edges: ["loop-model"] };
    if (k === "model.request" || k === "http.inference.send")
      return { node: "loop", edges: ["loop-model"] };
    if (
      k === "model.response" ||
      k.startsWith("model.stream") ||
      k.startsWith("model.decode") ||
      k.startsWith("model.audit") ||
      k === "http.inference.json"
    )
      return { node: "response", edges: ["sample-response"] };
    if (k === "runtime.info") return { node: "weights", edges: [] };
    if (k === "runtime.slots" || k === "runtime.cache")
      return { node: "cache", edges: [] };
    if (k.startsWith("runtime.")) return { node: "forward", edges: [] };
    if (
      k.startsWith("output.") ||
      k.startsWith("validation.") ||
      k.startsWith("run.")
    )
      return { node: "result", edges: ["loop-result"] };
    return { node: "loop", edges: [] };
  }
  const esc = (value) =>
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
  const short = (value, n = 95) => {
    const text = typeof value === "string" ? value : JSON.stringify(value);
    return (text ?? "없음").replace(/\s+/g, " ").slice(0, n);
  };
  function measurement(e) {
    const o = e?.output || {},
      i = e?.input || {},
      k = e?.kind || "";
    if (k === "file.read")
      return (o.bytes_read ?? o.bytes ?? o.byte_count ?? "바이트") + " 읽기";
    if (k === "file.decode")
      return (
        (o.characters ?? o.text?.length ?? o.content?.length ?? "") +
        "자 · UTF-8"
      );
    if (k === "model.request") return i.messages?.length + "개 메시지";
    if (k === "context.append" || k === "context.assemble")
      return (o.message_count_after ?? o.messages?.length ?? "") + "개 메시지";
    if (k === "model.tokenize")
      return (o.token_count ?? o.tokens?.length ?? "") + "개 토큰";
    if (k === "reference.cache.write")
      return (o.tokens ?? o.cache?.tokens ?? "") + "위치 · KV 갱신";
    if (k === "reference.sample")
      return "ID " + o.id + " · " + short(o.piece, 16);
    if (k === "reference.forward.start")
      return short(o.phase ?? i.phase ?? "forward", 15);
    if (k === "agent.compute") return "대기 " + (o.wait_ms ?? 0) + " ms";
    if (k === "agent.queue") return "모델 차례 대기";
    if (k === "agent.mode") return o.label || o.mode;
    if (k === "tool.result") return e.title.replace(/ 실행 결과$/, "");
    if (k === "context.check")
      return (o.estimated_tokens ?? "") + " 토큰 추정";
    return (
      {
        "file.resolve": "경로 확인",
        "file.exists": "파일 존재 확인",
        "file.open": "파일 열기",
        "file.close": "파일 닫기",
        "rules.wrap": "규칙 → system",
        "skills.catalog": "이름·설명 목록",
        "model.template": "messages → 문자열",
        "model.detokenize": "ID → 문자열",
        "reference.embedding": "임베딩 벡터",
        "reference.projection": "Q/K/V projection",
        "reference.attention": "attention 가중치",
        "agent.spawn": "하위 대화 생성",
        "agent.join": "하위 보고 수집",
        "browser.render": "본문 반영",
        "validation.requirements": "완료 조건 확인",
        "config.resolve": "층별 값 결정",
        "rules.resolve": "규칙 순서 결정",
        "rules.directory": "폴더 규칙 추가",
        "context.prune": "도구 결과 본문 생략",
        "context.compact.start": "앞선 작업 요약 중",
        "context.compact.result": "요약으로 입력 교체",
        "context.compact.failed": "요약 실패 · 원문 유지",
        "checkpoint.save": "상태 저장",
        "run.resume": "저장 상태 복원",
      }[k] ||
      e?.kind ||
      ""
    );
  }
  // 노드 제목(16px)이 화면에서 11.5px 이상이 되는 배율. 이보다 작아지면 글자를 읽을 수 없다.
  const READABLE_SCALE = 11.5 / 16;
  let callbacks = {},
    svg,
    host,
    lastEvent = null,
    zoom = 1,
    pan = { x: 0, y: 0 },
    auto = true,
    focus = null;
  function init(element, handlers = {}) {
    host = element;
    callbacks = handlers;
    const group = (x, y, w, h, title, sub) =>
      `<g class="arch-group"><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="10"/><text x="${x + 16}" y="${y + 25}" class="group-title">${title}</text>${sub ? `<text x="${x + w - 16}" y="${y + 25}" text-anchor="end" class="group-sub">${sub}</text>` : ""}</g>`;
    host.innerHTML = `<svg viewBox="0 0 1100 600" role="img" aria-label="에이전트 전체 구조도. 단계에 따라 구성 요소와 데이터 경로가 강조됩니다."><defs><marker id="arch-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0 0 L7 3.5 L0 7"/></marker><marker id="arch-hot-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0 0 L7 3.5 L0 7"/></marker></defs><g class="arch-world">${group(202, 24, 410, 562, "하네스", "Python")}${group(682, 24, 406, 430, "추론 경로", "Python / llama.cpp")}${group(682, 474, 406, 112, "외부 프로세스", "")}<text x="24" y="58" class="group-title">사용자 · 브라우저</text><text x="24" y="207" class="group-title">프로젝트 파일</text><g class="arch-edges">${edges.map(([id, path]) => `<path data-arch-edge="${id}" d="${path}" fill="none" marker-end="url(#arch-arrow)"/>`).join("")}</g>${nodes.map((n) => `<g data-arch-node="${n.id}" class="arch-node" role="button" tabindex="0" aria-label="${n.title} 구성과 기록"><title>${n.title} · ${n.description}</title><rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="7"/><circle class="node-dot" cx="${n.x + n.w - 12}" cy="${n.y + 13}" r="3"/><text x="${n.x + 12}" y="${n.y + 22}" class="node-title">${n.title}</text><text x="${n.x + 12}" y="${n.y + 41}" class="node-meta">${n.description}</text>${n.id === "forward" ? `<text x="${n.x + 12}" y="${n.y + 66}" class="model-stage">선택 레이어의 수치 표본</text><text x="${n.x + 12}" y="${n.y + 86}" class="model-stage">실행기에 따라 노출 범위 다름</text>` : ""}</g>`).join("")}<g class="arch-pulse hidden"><circle r="5"/></g></g></svg>`;
    svg = host.querySelector("svg");
    svg.setAttribute("viewBox", "0 0 1100 430");
    svg.setAttribute("role", "group");
    svg.querySelectorAll(".arch-group").forEach((group) => {
      const rect = group.querySelector("rect");
      rect.setAttribute("y", Number(rect.getAttribute("y")) * 0.7);
      rect.setAttribute("height", Number(rect.getAttribute("height")) * 0.7);
      group
        .querySelectorAll("text")
        .forEach((t) => t.setAttribute("y", Number(t.getAttribute("y")) * 0.7));
    });
    svg
      .querySelectorAll(".arch-world > text")
      .forEach((t) => t.setAttribute("y", Number(t.getAttribute("y")) * 0.7));
    nodes.forEach((n) => {
      const el = svg.querySelector(`[data-arch-node="${n.id}"]`);
      el.querySelector(".node-title").setAttribute(
        "y",
        n.y + (n.h < 40 ? 15 : 20),
      );
      el.querySelector(".node-meta").setAttribute(
        "y",
        n.y + (n.h < 40 ? 29 : 38),
      );
      el.querySelectorAll(".model-stage").forEach((t, i) =>
        t.setAttribute("y", n.y + 62 + i * 16),
      );
    });
    const activate = (event) => {
      const node = event.target.closest("[data-arch-node]");
      if (node) callbacks.select?.(node.dataset.archNode);
    };
    host.addEventListener("click", activate);
    host.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        activate(e);
      }
    });
    let drag;
    svg.addEventListener("pointerdown", (e) => {
      if (e.target.closest("[data-arch-node]")) return;
      drag = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y };
      svg.setPointerCapture(e.pointerId);
    });
    svg.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const bounds = svg.getBoundingClientRect();
      pan.x = drag.px + ((e.clientX - drag.x) * 1100) / bounds.width;
      pan.y = drag.py + ((e.clientY - drag.y) * 430) / bounds.height;
      transform();
    });
    svg.addEventListener("pointerup", () => {
      drag = null;
    });
    svg.addEventListener("pointercancel", () => {
      drag = null;
    });
    if (typeof ResizeObserver !== "undefined")
      new ResizeObserver(transform).observe(host);
  }
  function view() {
    const bounds = svg.getBoundingClientRect(),
      base = Math.min(bounds.width / 1100, bounds.height / 430);
    if (!base) return null;
    return {
      base,
      width: bounds.width / base,
      height: bounds.height / base,
      effective: auto ? Math.max(zoom, READABLE_SCALE / base) : zoom,
    };
  }
  function transform() {
    const current = svg && view();
    if (!current) return;
    let followX = 0,
      followY = 0;
    // 공간이 좁으면 전체를 줄이는 대신 읽을 수 있는 배율을 유지하고 현재 단계가 보이도록 옮긴다.
    if (auto && focus && current.base < READABLE_SCALE) {
      const limit = (value, range) =>
        Math.max(-Math.max(0, range), Math.min(Math.max(0, range), value));
      followX = limit(
        -(focus.x - 550) * current.effective,
        (1100 * current.effective - current.width) / 2,
      );
      followY = limit(
        -(focus.y - 215) * current.effective,
        (430 * current.effective - current.height) / 2,
      );
    }
    svg
      .querySelector(".arch-world")
      .setAttribute(
        "transform",
        `translate(${pan.x + followX} ${pan.y + followY}) translate(550 215) scale(${current.effective}) translate(-550 -215)`,
      );
    svg.classList.toggle("arch-compact", current.base * current.effective < 0.79);
  }
  function scale(direction) {
    // 사용자가 배율을 직접 바꾸면 자동 맞춤을 멈춘다.
    const current = svg && view();
    zoom = Math.min(
      3.5,
      Math.max(0.7, (current ? current.effective : zoom) + direction * 0.2),
    );
    auto = false;
    transform();
  }
  function fit() {
    // 자동 맞춤 → 전체 보기 → 자동 맞춤 순서로 전환한다.
    const full = !auto && zoom === 1 && pan.x === 0 && pan.y === 0;
    zoom = 1;
    pan = { x: 0, y: 0 };
    auto = full;
    transform();
  }
  function update({ events = [], cursor = -1, run, health }) {
    if (!svg) return;
    const current = events[cursor],
      location = locate(current),
      past = events.slice(0, cursor + 1),
      agent = current?.agent_id;
    const relevant = past.filter(
        (e) => !agent || !e.agent_id || e.agent_id === agent,
      ),
      observed = new Map();
    relevant.forEach((e) => observed.set(locate(e).node, e));
    const external =
      run?.conditions?.backend === "llama" ||
      (!run && health?.backend === "llama");
    if (external && location.node === "response")
      location.edges =
        current?.kind === "model.response" ? ["response-loop"] : [];
    const effectiveFeatures =
      relevant.findLast((e) => e.kind === "config.apply")?.output?.features ||
      run?.features;
    const effectiveMode =
      relevant.findLast((e) => e.kind === "agent.mode")?.output?.mode ||
      run?.mode;
    const disabled = new Set();
    if (run && !effectiveFeatures?.compaction && !effectiveFeatures?.turn_compaction)
      disabled.add("compaction");
    for (const feature of [
      "rules",
      "memory",
      "skills",
      "commands",
      "mcp",
      "lsp",
    ])
      if (run && effectiveFeatures?.[feature] === false) disabled.add(feature);
    if (run && !effectiveFeatures?.tools) {
      disabled.add("tools");
      disabled.add("gate");
    }
    if (
      run &&
      effectiveMode !== "team" &&
      !relevant.some((e) => e.kind === "agent.spawn")
    )
      disabled.add("agents");
    if (external)
      for (const id of ["embedding", "forward", "sample"]) disabled.add(id);
    for (const n of nodes) {
      const el = svg.querySelector(`[data-arch-node="${n.id}"]`),
        record = observed.get(n.id);
      el.classList.toggle("current", location.node === n.id && !!current);
      el.classList.toggle(
        "related",
        !!current && !!location.related?.includes(n.id),
      );
      el.classList.toggle("visited", !!record);
      el.classList.toggle("unavailable", disabled.has(n.id));
      const label =
        n.id === "weights"
          ? run?.conditions?.model || health?.model || n.description
          : disabled.has(n.id)
            ? external && ["embedding", "forward", "sample"].includes(n.id)
              ? "서버 내부 · 수치 미노출"
              : "이번 실행에서 사용 안 함"
            : record
              ? measurement(record)
              : n.description;
      el.querySelector(".node-meta").textContent = short(
        label,
        svg.classList.contains("arch-compact") ? 16 : 25,
      );
    }
    for (const [id] of edges) {
      const edge = svg.querySelector(`[data-arch-edge="${id}"]`),
        hot = !!current && location.edges.includes(id);
      edge.classList.toggle("current", hot);
      edge.setAttribute(
        "marker-end",
        `url(#${hot ? "arch-hot-arrow" : "arch-arrow"})`,
      );
    }
    const pulse = svg.querySelector(".arch-pulse"),
      key = current ? `${run?.id}:${current.id}` : null;
    pulse.classList.add("hidden");
    if (
      key &&
      key !== lastEvent &&
      location.edges.length &&
      !scope.matchMedia?.("(prefers-reduced-motion: reduce)").matches
    ) {
      const edge = svg.querySelector(`[data-arch-edge="${location.edges[0]}"]`);
      if (edge) {
        const point = edge.getPointAtLength(edge.getTotalLength() / 2);
        pulse.setAttribute("transform", `translate(${point.x} ${point.y})`);
        pulse.classList.remove("hidden");
      }
    }
    lastEvent = key;
    const active = nodes.find((n) => n.id === location.node);
    focus = active
      ? { x: active.x + active.w / 2, y: active.y + active.h / 2 }
      : null;
    transform();
    return {
      current,
      location,
      component: active?.title,
      measurement: measurement(current),
    };
  }
  const api = {
    nodes,
    edges,
    definitions,
    locate,
    measurement,
    init,
    update,
    scale,
    fit,
    short,
    escape: esc,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  scope.AgentArchitecture = api;
})(typeof window !== "undefined" ? window : globalThis);
