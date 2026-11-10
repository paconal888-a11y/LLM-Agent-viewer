/* 실행 기록을 사람이 따라갈 수 있는 장면으로 묶는다.
   문장은 기록된 값으로만 만든다. 모델에게 요약시키지 않고, 원인을 단정하지 않는다. */
((scope) => {
  "use strict";
  const REQUEST = new Set([
    "browser.input",
    "browser.serialize",
    "http.body.read",
    "http.json.decode",
    "harness.modules",
    "thread.dispatch",
    "input.receive",
    "request.route",
  ]);
  const COMPACTION = /^(context\.prune|context\.compact\.|memory\.compaction\.(start|result|failed))/;
  const TOOL_LABELS = {
    list_files: "파일 목록",
    read_file: "파일 읽기",
    write_file: "파일 쓰기",
    check_file: "파일 검사",
    add_numbers: "덧셈",
    search_text: "파일 내용 검색",
    web_search: "인터넷 검색",
    load_skill: "스킬 불러오기",
    set_plan: "계획 기록",
    complete_plan_step: "계획 완료 표시",
    delegate_tasks: "작업 위임",
    lsp_diagnostics: "코드 진단",
    lsp_symbols: "코드 심볼",
    lsp_definition: "정의 찾기",
  };
  const STATUS = {
    completed: "완료",
    failed: "실패",
    error: "실패",
    stopped: "중지됨",
    stopping: "중지 중",
    running: "실행 중",
    paused: "단계 대기",
    loading: "불러오는 중",
  };
  // 이 장면의 결과를 바꾸고 싶을 때 먼저 볼 곳
  const CHANGE = {
    prepare: ["demo_workspace/AGENTS.md", "규칙 파일 고치기"],
    context: ["local_agent_lab/context.py", "입력 조립 코드 보기"],
    model: ["demo_workspace/agent-lens.json", "실행 설정 파일 열기"],
    tool: ["local_agent_lab/policy.py", "도구 검사 코드 보기"],
    agents: ["demo_workspace/agents.json", "하위 에이전트 설정 열기"],
    compaction: ["demo_workspace/agent-lens.json", "압축 기준 파일 열기"],
    repair: ["local_agent_lab/policy.py", "완료 판정 코드 보기"],
    finish: ["local_agent_lab/policy.py", "완료 판정 코드 보기"],
  };

  function short(value, limit = 90) {
    const text = String(value ?? "")
      .replace(/\s+/g, " ")
      .trim();
    return text.length > limit ? text.slice(0, limit - 1) + "…" : text;
  }
  function plain(value) {
    return String(value ?? "")
      .replace(/<think>[\s\S]*?<\/think>/g, "")
      .trim();
  }
  function count(value) {
    return Number(value).toLocaleString("en-US");
  }
  function toolLabel(name) {
    return TOOL_LABELS[name] ? `${TOOL_LABELS[name]} (${name})` : name;
  }
  function parseArguments(value) {
    if (typeof value !== "string") return value ?? {};
    try {
      return JSON.parse(value);
    } catch {
      return value;
    }
  }
  function compact(value, limit = 110) {
    if (value === null || value === undefined) return "";
    return short(typeof value === "string" ? value : JSON.stringify(value), limit);
  }
  function fileName(path) {
    const text = String(path ?? "").replaceAll("\\", "/");
    const at = text.indexOf("demo_workspace/");
    return at >= 0 ? text.slice(at + "demo_workspace/".length) : text.split("/").slice(-2).join("/");
  }
  // 도구 인자를 "이름: 값" 한 줄로 쓴다.
  function describeArguments(value) {
    if (value === null || typeof value !== "object") return compact(value);
    return short(
      Object.entries(value)
        .map(([key, item]) => `${key}: ${compact(item, 40)}`)
        .join(" · "),
      110,
    );
  }
  // 도구 결과에서 사람이 먼저 알고 싶은 값을 꺼낸다.
  function describeResult(output) {
    if (output === null || typeof output !== "object") return compact(output);
    const where = output.path ? `${fileName(output.path)} · ` : "";
    if ("result" in output) return `결과 ${compact(output.result, 60)}`;
    if (typeof output.content === "string") return `${where}${count(output.content.length)}자 읽음`;
    if ("written_bytes" in output) return `${where}${count(output.written_bytes)}바이트 저장`;
    if ("passed" in output) return `${where}${output.passed ? "검사 통과" : "검사 실패"}`;
    if (Array.isArray(output.results)) return `${short(output.query, 40)} · 결과 ${output.results.length}건`;
    if (Array.isArray(output.matches)) return `${where}${output.matches.length}건 찾음`;
    if (Array.isArray(output.files)) return `파일 ${output.files.length}개`;
    return compact(output);
  }
  function unique(items) {
    return [...new Set(items.filter(Boolean))];
  }
  function responseMessage(event) {
    return event?.output?.choices?.[0]?.message || {};
  }

  // 기록 전체를 빠짐없이, 겹치지 않게 연속된 구간으로 나눈다.
  function partition(events) {
    const list = [];
    let scene = null,
      inModel = false,
      delegating = 0,
      assembled = false,
      modelSeen = false;
    const open = (kind, index) => {
      scene = { kind, start: index, end: index };
      list.push(scene);
    };
    const nameOf = (event) => event.input?.function?.name || event.input?.name || "";
    const outcome = (from) => {
      for (let i = from; i < events.length; i++) {
        const kind = events[i].kind;
        if (kind === "context.repair" || kind === "agent.review") return "repair";
        if (kind === "output.answer" || kind === "run.complete" || kind === "run.error") return "finish";
        if (kind === "model.request") return "repair";
      }
      return "finish";
    };
    events.forEach((event, index) => {
      const kind = event.kind;
      const extend = () => {
        if (scene) scene.end = index;
        else open("prepare", index);
      };
      if (delegating) {
        extend();
        if (kind === "agent.join") delegating = 2;
        else if (delegating === 2 && kind === "context.append") delegating = 0;
        return;
      }
      if (inModel && kind !== "run.error" && kind !== "run.complete") {
        scene.end = index;
        if (kind === "model.response") inModel = false;
        return;
      }
      inModel = false;
      if (kind === "runtime.cache" && (scene?.kind === "model" || scene?.kind === "compaction")) {
        scene.end = index;
        return;
      }
      if (kind === "model.request") {
        modelSeen = true;
        inModel = true;
        if (event.model_purpose === "compaction") {
          if (scene?.kind === "compaction") scene.end = index;
          else open("compaction", index);
        } else open("model", index);
        return;
      }
      if (COMPACTION.test(kind)) {
        if (scene?.kind === "compaction") scene.end = index;
        else open("compaction", index);
        return;
      }
      if (kind === "validation.final") {
        open(outcome(index + 1), index);
        return;
      }
      if (kind === "tool.dispatch") {
        open(nameOf(event) === "delegate_tasks" ? "agents" : "tool", index);
        if (scene.kind === "agents") delegating = 1;
        return;
      }
      if (kind === "agent.delegate" || kind === "agent.spawn") {
        open("agents", index);
        delegating = 1;
        return;
      }
      if (!modelSeen && !assembled) {
        if (REQUEST.has(kind) && (!scene || scene.kind === "request")) {
          if (scene) scene.end = index;
          else open("request", index);
          return;
        }
        if (kind === "context.assemble") {
          assembled = true;
          open("context", index);
          return;
        }
        if (!scene || scene.kind === "request" || scene.kind === "compaction") open("prepare", index);
        else scene.end = index;
        return;
      }
      extend();
    });
    return list;
  }

  function chatItems(request) {
    const messages = request?.input?.messages || [],
      segments = request?.output?.segments || [],
      role = { system: "지시문", user: "사용자", assistant: "모델", tool: "도구 결과" };
    return messages.map((message, index) => {
      const labels = unique(segments.filter((s) => s.message_index === index).map((s) => s.label)),
        text =
          typeof message.content === "string"
            ? message.content
            : message.content
              ? JSON.stringify(message.content)
              : (message.tool_calls || []).map((c) => c.function?.name).join(", ");
      return {
        role: labels.length ? labels.join(" · ") : role[message.role] || message.role,
        text: short(plain(text), 150),
        characters: String(text ?? "").length,
      };
    });
  }

  const builders = {
    request({ range, run }) {
      const input = range.find((e) => e.kind === "browser.input"),
        prompt = run?.prompt || input?.input?.text || input?.output?.text || "";
      return {
        title: "요청을 받았습니다",
        summary: prompt ? `“${short(prompt)}”` : "",
        owner: "user",
        focus: input || range[0],
        note: "입력한 글이 그대로 서버로 전달됩니다. 아직 모델은 호출되지 않았습니다.",
        blocks: prompt ? [{ type: "quote", label: "입력한 요청", text: prompt }] : [],
      };
    },
    prepare({ range }) {
      const files = unique(range.filter((e) => e.kind === "file.read").map((e) => fileName(e.input?.path))),
        resumed = range.some((e) => e.kind === "run.resume"),
        rules = range.findLast((e) => e.kind === "rules.wrap"),
        rest = files.length - 3;
      return {
        title: resumed
          ? "저장된 상태를 불러왔습니다"
          : files.length
            ? `설정과 규칙 파일 ${files.length}개를 읽었습니다`
            : "설정과 규칙을 확인했습니다",
        summary: files.length ? files.slice(0, 3).join(", ") + (rest > 0 ? ` 외 ${rest}개` : "") : "",
        owner: "config",
        focus: rules || range.at(-1),
        note: "어떤 파일을 읽을지는 코드가 정하고, 내용은 파일에서 옵니다. 파일을 고치면 다음 실행의 입력이 달라집니다.",
        blocks: files.length ? [{ type: "list", label: "읽은 파일", items: files }] : [],
      };
    },
    context({ range, after, findAfter }) {
      const assemble = range.find((e) => e.kind === "context.assemble"),
        request = after.find((e) => e.kind === "model.request"),
        response = findAfter(request, "model.response"),
        messages = request?.input?.messages || assemble?.output?.messages || [],
        tools = (request?.input?.tools || []).map((tool) => tool.function?.name).filter(Boolean),
        tokens = response?.output?.usage?.prompt_tokens,
        parts = [`메시지 ${messages.length}개`];
      if (tokens) parts.push(`${count(tokens)}토큰`);
      if (request) parts.push(`쓸 수 있는 도구 ${tools.length}개`);
      const blocks = [];
      if (request) blocks.push({ type: "chat", label: "모델이 받는 글", items: chatItems(request) });
      if (tools.length) blocks.push({ type: "list", label: "모델에게 알려 준 도구", items: tools.map(toolLabel) });
      return {
        title: "모델에게 보낼 입력을 만들었습니다",
        summary: parts.join(" · "),
        owner: "code",
        focus: assemble || range.at(-1),
        note: "모델은 이 글만 봅니다. 여기에 없는 내용은 모델이 알 수 없습니다.",
        blocks,
        inputEvent: request?.id,
      };
    },
    model({ range, modelNumber }) {
      const request = range.find((e) => e.kind === "model.request"),
        response = range.findLast((e) => e.kind === "model.response"),
        message = responseMessage(response),
        calls = message.tool_calls || [],
        text = plain(message.content),
        usage = response?.output?.usage || {},
        names = calls.map((call) => call.function?.name),
        stats = [];
      if (usage.prompt_tokens) stats.push(`입력 ${count(usage.prompt_tokens)}토큰`);
      if (usage.completion_tokens) stats.push(`출력 ${count(usage.completion_tokens)}토큰`);
      const blocks = [];
      if (text) blocks.push({ type: "quote", label: "모델이 쓴 글", text });
      calls.forEach((call) =>
        blocks.push({
          type: "call",
          label: "모델이 요청한 도구",
          name: toolLabel(call.function?.name),
          value: parseArguments(call.function?.arguments),
        }),
      );
      return {
        title: !response
          ? "모델의 응답을 기다리고 있습니다"
          : calls.length === 1
            ? `모델이 ${names[0]} 도구를 요청했습니다`
            : calls.length
              ? `모델이 도구 ${calls.length}개를 요청했습니다`
              : "모델이 도구를 쓰지 않고 답을 썼습니다",
        summary: calls.length ? describeArguments(parseArguments(calls[0].function?.arguments)) : text ? `“${short(text)}”` : "",
        owner: "model",
        focus: response || range.at(-1),
        note: calls.length
          ? "모델은 도구를 직접 실행하지 않습니다. 실행해 달라는 요청을 글로 쓸 뿐입니다."
          : response
            ? "모델이 어떤 도구도 요청하지 않아서 이 글로 작업이 끝날 수 있습니다."
            : "",
        badge: `모델 호출 ${modelNumber}`,
        stats: stats.join(" · "),
        blocks,
        inputEvent: request?.id,
      };
    },
    tool({ range }) {
      const dispatch = range.find((e) => e.kind === "tool.dispatch"),
        validate = range.find((e) => e.kind === "tool.validate"),
        result = range.findLast((e) => e.kind === "tool.result"),
        error = range.findLast((e) => e.kind === "tool.error"),
        name = dispatch?.input?.function?.name || validate?.input?.name || "도구",
        args = validate?.input?.arguments ?? parseArguments(dispatch?.input?.function?.arguments),
        blocked = validate?.output?.allowed === false,
        external = range.some((e) => ["mcp", "lsp", "web"].includes(e.actor)),
        web = range.find((e) => e.kind === "web.request"),
        reason = validate?.output?.reason || validate?.output?.error || error?.output?.error || error?.detail,
        blocks = [{ type: "call", label: "실행 요청", name: toolLabel(name), value: args }];
      if (web) blocks.push({ type: "text", label: "PC 밖으로 보낸 검색어", text: `${web.input?.query} → ${web.input?.provider}` });
      if (Array.isArray(result?.output?.results))
        blocks.push({
          type: "list",
          label: "찾은 결과",
          items: result.output.results.map((item) => item.title),
        });
      else if (result?.output !== undefined && result?.output !== null)
        blocks.push({ type: "value", label: "실행 결과", value: result.output });
      if (blocked || error) blocks.push({ type: "text", label: blocked ? "거부 이유" : "오류", text: String(reason ?? "기록된 이유 없음") });
      return {
        title: blocked
          ? `코드가 ${name} 도구 요청을 막았습니다`
          : error
            ? `${name} 도구 실행이 실패했습니다`
            : result
              ? `코드가 검사한 뒤 ${name} 도구를 실행했습니다`
              : `${name} 도구를 실행하고 있습니다`,
        summary: blocked || error ? short(reason) : describeResult(result?.output),
        owner: external ? "external" : "code",
        focus: result || error || validate || range.at(-1),
        note: web
          ? "검색어는 모델이 정했고, 보낼 주소는 코드에 고정되어 있습니다. 받은 것은 제목과 짧은 요약이며 웹페이지 전체를 읽은 것은 아닙니다."
          : "도구 이름, 인자, 경로는 코드가 먼저 검사합니다. 실제 함수도 코드가 실행하고, 결과는 다음 모델 입력에 들어갑니다.",
        blocks,
        change: web ? { file: "local_agent_lab/websearch.py", label: "검색 서비스 코드 보기" } : undefined,
      };
    },
    agents({ range }) {
      const spawns = range.filter((e) => e.kind === "agent.spawn"),
        names = unique(spawns.map((e) => e.output?.agent_name)),
        join = range.find((e) => e.kind === "agent.join"),
        models = range.filter((e) => e.kind === "model.request").length,
        tools = range.filter((e) => e.kind === "tool.dispatch").length - 1;
      return {
        title: names.length ? `하위 에이전트 ${names.length}개가 각자 일했습니다` : "작업을 하위 에이전트에게 맡겼습니다",
        summary: names.join(", "),
        owner: "code",
        focus: join || range.at(-1),
        note: "위임 요청은 모델이 썼고, 코드가 별도 대화로 실행합니다. 합류할 때는 각 에이전트의 보고만 돌아옵니다.",
        stats: `모델 호출 ${models}회 · 도구 ${Math.max(0, tools)}회`,
        blocks: names.length ? [{ type: "list", label: "맡은 에이전트", items: names }] : [],
      };
    },
    compaction({ range }) {
      const result = range.find((e) => e.kind === "context.compact.result"),
        start = range.find((e) => e.kind === "context.compact.start"),
        prune = range.find((e) => e.kind === "context.prune"),
        failed = range.find((e) => e.kind === "context.compact.failed" || e.kind === "memory.compaction.failed"),
        memory = range.find((e) => e.kind === "memory.compaction.result"),
        pruned = prune?.output?.pruned?.length || 0,
        summary = [];
      if (pruned) summary.push(`오래된 도구 결과 ${pruned}개 생략`);
      if (start?.output?.source_messages)
        summary.push(`메시지 ${start.output.source_messages}개(${count(start.output.source_characters || 0)}자)를 요약`);
      return {
        title: failed
          ? "요약에 실패해 원문을 유지했습니다"
          : memory
            ? "이전 대화를 요약했습니다"
            : result
              ? "입력이 커져서 앞선 작업을 요약했습니다"
              : "입력이 커져서 오래된 결과를 줄였습니다",
        summary: summary.join(" · "),
        owner: "code",
        focus: result || memory || failed || range.at(-1),
        note: "언제 줄일지는 코드가 정합니다. 요약문을 쓰는 것은 모델이고, 내용이 맞는지는 코드가 보증하지 않습니다.",
        blocks: [],
      };
    },
    repair({ range }) {
      const requirements = range.find((e) => e.kind === "validation.requirements"),
        final = range.find((e) => e.kind === "validation.final"),
        repeat = range.find((e) => e.kind === "validation.repeat"),
        repair = range.find((e) => e.kind === "context.repair"),
        review = range.find((e) => e.kind === "agent.review"),
        missing = unique((requirements?.output?.missing || []).map((item) => item.tool)),
        unchecked = final?.output?.unchecked_files || [],
        parts = [];
      if (missing.length) parts.push(`빠진 도구 결과: ${missing.join(", ")}`);
      if (unchecked.length) parts.push(`검사하지 않은 파일: ${unchecked.map(fileName).join(", ")}`);
      if (repeat?.output?.streak > 1) parts.push(`같은 답 ${repeat.output.streak}회`);
      return {
        title: review ? "코드가 추가 점검을 요청했습니다" : "코드가 아직 끝나지 않았다고 돌려보냈습니다",
        summary: parts.join(" · "),
        owner: "code",
        focus: repair || review || range.at(-1),
        note: "모델이 끝났다고 답해도 코드가 조건을 확인합니다. 빠진 것이 있으면 그 내용을 입력에 넣고 다시 호출합니다.",
        blocks: repair?.output?.appended_message?.content
          ? [{ type: "quote", label: "모델에게 다시 보낸 글", text: repair.output.appended_message.content }]
          : [],
      };
    },
    finish({ range, run }) {
      const failure = range.find((e) => e.kind === "run.error"),
        requirements = range.find((e) => e.kind === "validation.requirements"),
        answer = range.find((e) => e.kind === "output.answer"),
        evidence = requirements?.output?.evidence?.length || 0,
        expected = run?.requirements?.length ?? evidence;
      return {
        title: failure
          ? "실행이 오류로 끝났습니다"
          : !answer && range.every((e) => !e.kind.startsWith("run."))
            ? "완료 검사를 진행하고 있습니다"
            : expected
              ? `코드가 완료 조건 ${expected}개를 확인했습니다`
              : "검사할 조건 없이 완료로 끝났습니다",
        summary: failure
          ? short(failure.output?.error || failure.detail)
          : expected
            ? "확인한 범위는 조건에 적은 도구 결과까지입니다."
            : "완료 조건이 없어서 답의 내용은 확인하지 않았습니다.",
        owner: "code",
        focus: failure || answer || range.at(-1),
        note: "‘완료’는 코드가 검사한 범위까지만 뜻합니다. 답이 사실인지는 따로 확인해야 합니다.",
        blocks: [],
      };
    },
  };

  function scenes(events = [], run = null) {
    const list = partition(events);
    let modelNumber = 0;
    list.forEach((scene, order) => {
      const range = events.slice(scene.start, scene.end + 1),
        after = events.slice(scene.end + 1),
        findAfter = (from, kind) => from && events.slice(events.indexOf(from)).find((e) => e.kind === kind);
      if (scene.kind === "model") modelNumber++;
      const built = builders[scene.kind]({ range, after, findAfter, run, modelNumber }),
        previous = scene.start ? events[scene.start - 1].elapsed_ms : 0;
      Object.assign(scene, built, {
        number: order + 1,
        count: range.length,
        focus: scene.start + Math.max(0, range.indexOf(built.focus)),
        ms: Math.max(0, (events[scene.end].elapsed_ms || 0) - (previous || 0)),
        change: built.change || (CHANGE[scene.kind] ? { file: CHANGE[scene.kind][0], label: CHANGE[scene.kind][1] } : null),
      });
    });
    return list;
  }

  function overview(events = [], run = null) {
    const responses = events.filter((e) => e.kind === "model.response"),
      sum = (key) => responses.reduce((total, e) => total + (e.output?.usage?.[key] || 0), 0),
      status = run?.status || "",
      seconds = run?.active_seconds ?? run?.elapsed_seconds ?? (events.at(-1)?.elapsed_ms || 0) / 1000;
    return {
      status,
      statusLabel: STATUS[status] || status,
      prompt: run?.prompt || "",
      answer: plain(run?.answer),
      error: run?.error || "",
      seconds,
      modelCalls: run?.model_calls ?? events.filter((e) => e.kind === "model.request").length,
      toolCalls: run?.tool_calls ?? events.filter((e) => e.kind === "tool.dispatch").length,
      promptTokens: run?.usage?.prompt_tokens ?? sum("prompt_tokens"),
      completionTokens: run?.usage?.completion_tokens ?? sum("completion_tokens"),
    };
  }

  function sceneAt(list, cursor) {
    const found = list.findIndex((scene) => cursor >= scene.start && cursor <= scene.end);
    return found < 0 ? list.length - 1 : found;
  }

  const api = { scenes, overview, sceneAt, short, fileName };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  scope.AgentStory = api;
})(typeof window !== "undefined" ? window : globalThis);
