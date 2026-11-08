/* 저장된 이벤트만으로 입력과 변경을 조회한다. 새 추론이나 실행을 만들지 않는다. */
((scope) => {
  "use strict";
  const agentOf = (event, root) => event?.agent_id || root;
  const purposeOf = (event) => event?.model_purpose || "answer";
  const canonical = (value) =>
    JSON.stringify(value, (_, v) =>
      v && typeof v === "object" && !Array.isArray(v)
        ? Object.fromEntries(
            Object.keys(v)
              .sort()
              .map((key) => [key, v[key]]),
          )
        : v,
    );
  const same = (a, b) => canonical(a) === canonical(b);
  const isRequest = (event) =>
    event?.kind === "model.request" && Array.isArray(event.input?.messages);
  const finite = (value) =>
    typeof value === "number" && Number.isFinite(value) ? value : null;
  function diffMessages(before, after) {
    const rows = [];
    for (
      let index = 0;
      index < Math.max(before.length, after.length);
      index++
    ) {
      const change =
        index >= before.length
          ? "added"
          : index >= after.length
            ? "removed"
            : same(before[index], after[index])
              ? "unchanged"
              : "changed";
      rows.push({ index, change, before: before[index], after: after[index] });
    }
    return rows;
  }
  function diffTools(before, after) {
    const map = (tools) =>
      new Map(tools.map((tool, i) => [tool.function?.name || String(i), tool]));
    const a = map(before),
      b = map(after);
    return [...new Set([...a.keys(), ...b.keys()])].map((name) => ({
      name,
      change: !a.has(name)
        ? "added"
        : !b.has(name)
          ? "removed"
          : same(a.get(name), b.get(name))
            ? "unchanged"
            : "changed",
      before: a.get(name),
      after: b.get(name),
    }));
  }
  function snapshot(events, cursor, root) {
    const current = events[cursor],
      agent = agentOf(current, root),
      purpose = current?.kind?.startsWith("memory.compaction.")
        ? "compaction"
        : purposeOf(current);
    const requests = events.flatMap((event, index) =>
      isRequest(event) &&
      agentOf(event, root) === agent &&
      purposeOf(event) === purpose
        ? [{ event, index }]
        : [],
    );
    const at = requests.findLastIndex((record) => record.index <= cursor);
    const request = requests[at] || null;
    const result = {
      agent,
      purpose,
      requests,
      request,
      previous: requests[at - 1] || null,
      next: requests[at + 1] || null,
      diagnosticTokens: null,
      reportedTokens: null,
      audit: null,
      messageDiff: [],
      toolDiff: [],
      draft: null,
    };
    if (!request) {
      result.draft =
        events
          .slice(0, Math.max(0, cursor + 1))
          .findLast(
            (e) =>
              e.kind === "context.assemble" &&
              agentOf(e, root) === agent &&
              purposeOf(e) === purpose,
          ) || null;
      return result;
    }
    const next = events.findIndex(
      (event, index) =>
        index > request.index &&
        isRequest(event) &&
        agentOf(event, root) === agent,
    );
    const span = events
      .slice(
        request.index + 1,
        Math.min(cursor + 1, next < 0 ? events.length : next),
      )
      .filter(
        (event) =>
          agentOf(event, root) === agent && purposeOf(event) === purpose,
      );
    const tokenize = span.findLast((event) => event.kind === "model.tokenize");
    result.diagnosticTokens = Array.isArray(tokenize?.output?.tokens)
      ? tokenize.output.tokens.length
      : finite(tokenize?.output?.token_count);
    const cache = span.findLast((event) => event.kind === "runtime.cache");
    const response = span.findLast((event) => event.kind === "model.response");
    result.reportedTokens =
      finite(cache?.output?.prompt_tokens) ??
      finite(response?.output?.usage?.prompt_tokens);
    result.audit = cache
      ? {
          event: cache,
          countMatches: cache.output?.diagnostic_count_matches ?? null,
          templateMatches:
            cache.output?.template_matches_generation_prompt ?? null,
        }
      : null;
    if (result.previous) {
      result.messageDiff = diffMessages(
        result.previous.event.input.messages,
        request.event.input.messages,
      );
      result.toolDiff = diffTools(
        result.previous.event.input.tools || [],
        request.event.input.tools || [],
      );
    }
    return result;
  }
  function fragments(message, segments) {
    if (typeof message?.content !== "string") return [];
    const content = message.content;
    const candidates = segments
      .flatMap((part) => {
        const start =
          typeof part.text === "string" && part.text.length
            ? content.indexOf(part.text)
            : -1;
        return start < 0
          ? []
          : [{ ...part, start, end: start + part.text.length }];
      })
      .sort((a, b) => a.start - b.start);
    const result = [];
    let end = 0;
    for (const part of candidates) {
      if (part.start < end) continue;
      if (part.start > end)
        result.push({ text: content.slice(end, part.start) });
      result.push(part);
      end = part.end;
    }
    if (end < content.length) result.push({ text: content.slice(end) });
    return result.length ? result : [{ text: content }];
  }
  function contentValues(message) {
    if (typeof message?.content !== "string") return [];
    const values = [message.content];
    try {
      const parsed = JSON.parse(message.content);
      if (typeof parsed?.content === "string") values.push(parsed.content);
    } catch {
      /* 파일 본문은 JSON이 아닐 수도 있다. */
    }
    return values;
  }
  const includesText = (message, text) =>
    typeof text === "string" &&
    text.length > 0 &&
    contentValues(message).some((value) => value.includes(text));
  function origins(events, requestIndex, messageIndex, root) {
    const request = events[requestIndex],
      message = request?.input?.messages?.[messageIndex];
    if (!message) return [];
    const found = new Map();
    events.slice(0, requestIndex).forEach((event, index) => {
      if (agentOf(event, root) !== agentOf(request, root)) return;
      let path = event.input?.path || event.output?.source_path || "";
      // appended_message(완료 조건·보완 요청·점검 지시·지적 재포함·도구 결과)와 message(요약)를 가진
      // 모든 하네스 이벤트를 출처 후보로 본다. kind를 열거하면 새 이벤트가 생길 때마다 링크가 빠졌다.
      const matches =
        event.kind === "file.decode"
          ? includesText(message, event.output?.text)
          : event.kind === "rules.wrap"
            ? includesText(message, event.output?.instruction_block)
            : event.output?.appended_message
              ? same(message, event.output.appended_message)
              : event.kind === "memory.summary.include"
                ? same(message, event.output?.message)
                : event.kind === "model.response"
                  ? same(message, event.output?.choices?.[0]?.message)
                  : false;
      if (matches) found.set(event.kind + ":" + path, { event, index, path });
    });
    return [...found.values()].sort((a, b) => a.index - b.index);
  }
  function declaredOrigins(events, requestIndex, messageIndex, root) {
    const request = events[requestIndex];
    const parts = request?.output?.segments || [];
    const sources = new Set(
      parts
        .filter(
          (part) =>
            part.message_index === messageIndex && part.source_event_id != null,
        )
        .map((part) => part.source_event_id),
    );
    return events
      .slice(0, requestIndex)
      .flatMap((event, index) =>
        // 하위 에이전트 이벤트는 주 기록에서 child_event_id로 식별된다(agent_event_id는 발행되지 않는 필드였다).
        agentOf(event, root) === agentOf(request, root) &&
        sources.has(event.child_event_id ?? event.id)
          ? [{ event, index }]
          : [],
      );
  }
  function consumers(events, cursor, root) {
    const origin = events[cursor];
    if (!origin) return [];
    const text =
      origin.kind === "file.decode"
        ? origin.output?.text
        : origin.kind === "rules.wrap"
          ? origin.output?.instruction_block
          : null;
    const message =
      origin.output?.appended_message ||
      (origin.kind === "memory.summary.include" ? origin.output?.message : null);
    if (!text && !message) return [];
    return events.flatMap((event, index) => {
      if (
        index <= cursor ||
        !isRequest(event) ||
        agentOf(event, root) !== agentOf(origin, root)
      )
        return [];
      const indices = event.input.messages.flatMap((candidate, i) =>
        (message ? same(message, candidate) : includesText(candidate, text))
          ? [i]
          : [],
      );
      return indices.length ? [{ event, index, indices }] : [];
    });
  }
  function evidence(event) {
    const kind = event?.kind || "";
    if (kind.startsWith("validation."))
      return {
        label: "실행 검사",
        type: "validation",
        explanation:
          "정책 코드가 모델 출력과 도구 결과·실행 상태를 대조한 결과입니다.",
      };
    if (kind.endsWith(".unavailable"))
      return {
        label: "미수집",
        type: "missing",
        explanation: "실행기가 이 값을 제공하지 않았습니다.",
      };
    if (kind.startsWith("reference."))
      return {
        label: "Python 계측",
        type: "direct",
        explanation:
          "로컬 Python 생성 루프와 hook에서 기록했습니다. 텐서는 선택한 레이어·위치의 표본입니다.",
      };
    if (["model.template", "model.tokenize", "model.detokenize"].includes(kind))
      return {
        label: "입력 진단",
        type: "diagnostic",
        explanation:
          "템플릿·tokenizer 진단 결과입니다. 생성 요청에서 보고한 값과는 별도로 대조합니다.",
      };
    if (kind === "runtime.cache")
      return {
        label: "실행기 보고 + 하네스 대조",
        type: "reported",
        explanation:
          "prompt_tokens·cached_tokens·timings는 실행기가 보고한 값이고, *_matches 필드는 하네스가 진단값과 대조한 계산값입니다. value_sources에 구분을 적었습니다. 외부 실행기의 내부 텐서를 직접 관찰한 값은 아닙니다.",
      };
    if (
      kind === "runtime.slots" ||
      kind === "runtime.info" ||
      kind === "runtime.prefill" ||
      kind === "model.decode.report" ||
      kind.startsWith("http.inference")
    )
      return {
        label: "실행기 보고",
        type: "reported",
        explanation:
          "실행기가 반환한 설정·상태·사용량·진행 보고입니다. 외부 실행기의 내부 텐서를 직접 관찰한 값은 아닙니다.",
      };
    if (
      kind === "model.stream.assemble" ||
      kind.startsWith("context.") ||
      kind === "rules.wrap"
    )
      return {
        label: "하네스 변환",
        type: "derived",
        explanation: "하네스가 입력을 조립하거나 응답을 변환한 결과입니다.",
      };
    if (kind.startsWith("mcp.") || kind.startsWith("lsp."))
      return {
        label: "연결 기록",
        type: "protocol",
        explanation:
          "로컬 연결 설정과 프로토콜 요청·응답의 기록입니다. 외부 서버 내부 연산까지 추적하지는 않습니다.",
      };
    if (kind.startsWith("file."))
      return {
        label: "파일 I/O",
        type: "direct",
        explanation:
          "파일 경로 확인, 읽기와 디코딩 결과입니다. 모델 입력 포함 여부는 후속 요청에서 확인합니다.",
      };
    if (kind === "model.response" || kind.startsWith("model.stream"))
      return {
        label: "실행기 응답",
        type: "reported",
        explanation:
          "생성 결과 또는 스트림 프레임을 기록했습니다. 전송 조각과 토큰은 일대일이 아닙니다.",
      };
    return {
      label: "하네스 기록",
      type: "harness",
      explanation:
        "하네스가 남긴 이벤트입니다. 전체 값 탭에서 원본 데이터를 확인할 수 있습니다.",
    };
  }
  const api = {
    agentOf,
    purposeOf,
    canonical,
    diffMessages,
    diffTools,
    snapshot,
    fragments,
    origins,
    declaredOrigins,
    consumers,
    evidence,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  scope.AgentInspection = api;
})(typeof window !== "undefined" ? window : globalThis);
