const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");
const inspection = require("../web/inspection.js");
const request = (id, messages, extra = {}) => ({
  id,
  kind: "model.request",
  input: { messages, tools: [] },
  ...extra,
});

test("decision owner separates what the model chose from code, settings and the runtime", () => {
  const owner = (kind, extra = {}, events = []) =>
    inspection.decision({ kind, ...extra }, events).owner;
  for (const kind of ["model.response", "model.stream", "output.answer", "tool.dispatch", "memory.compaction.result", "context.compact.result"])
    assert.equal(owner(kind), "model", kind);
  for (const kind of ["validation.requirements", "validation.final", "tool.validate", "tool.result", "context.append", "context.repair", "model.request", "run.complete", "context.check", "context.prune", "checkpoint.save", "run.resume"])
    assert.equal(owner(kind), "code", kind);
  for (const kind of ["config.apply", "config.resolve", "rules.wrap", "rules.resolve", "rules.directory", "skills.catalog", "command.expand", "rules.disabled", "context.requirements", "mcp.tool.filtered"])
    assert.equal(owner(kind), "config", kind);
  for (const kind of ["runtime.cache", "model.tokenize", "reference.attention", "http.inference.send"])
    assert.equal(owner(kind), "runtime", kind);
  assert.equal(owner("reference.sample"), "model");
  assert.equal(owner("mcp.send"), "external");
  assert.equal(owner("input.receive"), "user");
});

test("a file read counts as the model's choice only when a tool request caused it", () => {
  const dispatch = { id: 5, kind: "tool.dispatch" },
    loader = { id: 6, kind: "function.enter" };
  assert.equal(
    inspection.decision({ id: 7, kind: "file.read", parent: 5 }, [dispatch, loader]).owner,
    "model",
  );
  assert.equal(
    inspection.decision({ id: 8, kind: "file.read", parent: 6 }, [dispatch, loader]).owner,
    "code",
  );
  assert.equal(inspection.decision({ id: 9, kind: "file.read" }, []).owner, "code");
});

test("every recorded event kind in the samples gets a decision owner", () => {
  const root = path.join(__dirname, "..", "samples");
  const known = new Set(["model", "code", "config", "user", "runtime", "external"]);
  for (const name of fs.readdirSync(root).filter((f) => f.endsWith(".json.gz"))) {
    const data = JSON.parse(zlib.gunzipSync(fs.readFileSync(path.join(root, name))));
    for (const event of data.events.slice(0, 2000)) {
      const result = inspection.decision(event, data.events);
      assert.ok(known.has(result.owner), event.kind);
      assert.ok(result.label && result.explanation, event.kind);
    }
  }
});

test("declared origins resolve child-local IDs without crossing agents or time", () => {
  // 주 기록에 비친 하위 에이전트 이벤트는 child_event_id(하위 기록의 번호)로 식별된다.
  // 하위 에이전트의 model.request가 적은 source_event_id는 그 번호를 가리킨다.
  const events = [
    { id: 9, agent_id: "root", child_event_id: null },
    { id: 10, agent_id: "child", child_event_id: 3 },
    request(11, [{ role: "user", content: "repair" }], {
      agent_id: "child",
      child_event_id: 4,
      output: {
        segments: [{ message_index: 0, origin: "harness", source_event_id: 3 }],
      },
    }),
    { id: 12, agent_id: "child", child_event_id: 3 },
  ];
  assert.deepEqual(
    inspection.declaredOrigins(events, 2, 0, "root").map((row) => row.event.id),
    [10],
  );
  assert.deepEqual(inspection.declaredOrigins(events, 2, 1, "root"), []);
  // 주 에이전트 자신의 이벤트는 id로 식별된다.
  const rootEvents = [
    { id: 3, agent_id: "root", child_event_id: null },
    request(4, [{ role: "user", content: "x" }], {
      agent_id: "root",
      output: { segments: [{ message_index: 0, source_event_id: 3 }] },
    }),
  ];
  assert.deepEqual(
    inspection.declaredOrigins(rootEvents, 1, 0, "root").map((row) => row.event.id),
    [3],
  );
});

test("every harness event that appends a message is linked to the requests that carry it", () => {
  // 완료 조건 안내·보완 요청·점검 지시·지적 재포함은 context.append가 아닌 이벤트로 기록된다.
  // 이전에는 kind를 열거해 context.append만 연결했기 때문에 '출처 없음'으로 보였다.
  const requirement = { role: "user", content: "완료 조건: check_file을 실행하세요." };
  const repair = { role: "user", content: "검사 결과가 없습니다. 다시 확인하세요." };
  const review = { role: "user", content: "결과를 점검하세요." };
  const recall = { role: "user", content: "이전 지적: 경로를 확인하세요." };
  const summary = { role: "system", content: "요약: 이전 대화에서 파일을 읽었습니다." };
  const events = [
    { id: 1, kind: "context.requirements", output: { appended_message: requirement } },
    { id: 2, kind: "memory.feedback.recall", output: { appended_message: recall } },
    { id: 3, kind: "memory.summary.include", output: { message: summary } },
    request(4, [summary, requirement, recall]),
    { id: 5, kind: "context.repair", output: { appended_message: repair } },
    { id: 6, kind: "agent.review", output: { appended_message: review } },
    request(7, [summary, requirement, recall, repair, review]),
  ];
  assert.deepEqual(inspection.consumers(events, 0, "root").map((r) => r.event.id), [4, 7]);
  assert.deepEqual(inspection.consumers(events, 1, "root")[0].indices, [2]);
  assert.deepEqual(inspection.consumers(events, 2, "root").map((r) => r.event.id), [4, 7]);
  assert.deepEqual(inspection.consumers(events, 4, "root").map((r) => r.event.id), [7]);
  assert.deepEqual(inspection.consumers(events, 5, "root")[0].indices, [4]);
  assert.equal(inspection.origins(events, 6, 0, "root")[0].event.kind, "memory.summary.include");
  assert.equal(inspection.origins(events, 6, 1, "root")[0].event.kind, "context.requirements");
  assert.equal(inspection.origins(events, 6, 3, "root")[0].event.kind, "context.repair");
  assert.equal(inspection.origins(events, 6, 4, "root")[0].event.kind, "agent.review");
  // 요청 이후의 이벤트는 출처가 될 수 없다.
  assert.deepEqual(inspection.origins(events, 3, 1, "root").map((r) => r.event.id), [1]);
});

test("snapshot does not expose a future request, token count or response", () => {
  const events = [
    request(1, [{ role: "user", content: "first" }]),
    {
      id: 2,
      kind: "model.tokenize",
      output: { tokens: [{ id: 1 }, { id: 2 }] },
    },
    { id: 3, kind: "model.response", output: { usage: { prompt_tokens: 7 } } },
    request(4, [{ role: "user", content: "future" }]),
  ];
  assert.equal(inspection.snapshot(events, -1, "root").request, null);
  const start = inspection.snapshot(events, 0, "root");
  assert.equal(start.request.event.id, 1);
  assert.equal(start.diagnosticTokens, null);
  assert.equal(start.reportedTokens, null);
  assert.equal(inspection.snapshot(events, 1, "root").diagnosticTokens, 2);
  assert.equal(inspection.snapshot(events, 2, "root").reportedTokens, 7);
});

test("interleaved children and summary calls use their own input and token accounting", () => {
  const events = [
    request(1, [{ role: "user", content: "summary" }], {
      agent_id: "root",
      model_purpose: "compaction",
    }),
    {
      id: 2,
      kind: "model.tokenize",
      agent_id: "root",
      model_purpose: "compaction",
      output: { token_count: 99 },
    },
    request(3, [{ role: "user", content: "main" }], { agent_id: "root" }),
    request(4, [{ role: "user", content: "child" }], { agent_id: "child" }),
    {
      id: 5,
      kind: "model.tokenize",
      agent_id: "child",
      output: { token_count: 200 },
    },
    {
      id: 6,
      kind: "model.response",
      agent_id: "root",
      output: { usage: { prompt_tokens: 12 } },
    },
  ];
  const main = inspection.snapshot(events, 5, "root");
  assert.equal(main.request.event.id, 3);
  assert.equal(main.previous, null);
  assert.equal(main.diagnosticTokens, null);
  assert.equal(main.reportedTokens, 12);
  assert.equal(inspection.snapshot(events, 4, "root").request.event.id, 4);
  const summaryBoundary = [
    request(1, [{ role: "user", content: "summarize" }], {
      model_purpose: "compaction",
    }),
    { id: 2, kind: "memory.compaction.result", model_purpose: "answer" },
    request(3, [{ role: "user", content: "answer" }]),
  ];
  assert.equal(
    inspection.snapshot(summaryBoundary, 1, "root").request.event.id,
    1,
  );
  assert.equal(inspection.snapshot(summaryBoundary, 2, "root").previous, null);
});

test("missing usage stays unknown, zero stays zero, and drafts are not requests", () => {
  const events = [
    { id: 1, kind: "context.assemble", output: { messages: [] } },
    request(2, []),
    {
      id: 3,
      kind: "runtime.cache",
      output: {
        prompt_tokens: 0,
        diagnostic_count_matches: false,
        template_matches_generation_prompt: null,
      },
    },
  ];
  assert.equal(inspection.snapshot(events, 0, "root").request, null);
  assert.equal(inspection.snapshot(events, 0, "root").draft.id, 1);
  const end = inspection.snapshot(events, 2, "root");
  assert.equal(end.reportedTokens, 0);
  assert.equal(end.audit.countMatches, false);
  assert.equal(end.audit.templateMatches, null);
});

test("message diff preserves ordering and distinguishes tool calls and arguments", () => {
  const before = [
    { role: "system", content: "same" },
    { role: "user", content: "A" },
    {
      role: "assistant",
      tool_calls: [
        { function: { name: "read_file", arguments: '{"path":"a"}' } },
      ],
    },
  ];
  const after = [
    { content: "same", role: "system" },
    { role: "user", content: "B" },
    {
      role: "assistant",
      tool_calls: [
        { function: { name: "read_file", arguments: '{"path":"b"}' } },
      ],
    },
    { role: "tool", content: "result" },
  ];
  assert.deepEqual(
    inspection.diffMessages(before, after).map((r) => r.change),
    ["unchanged", "changed", "changed", "added"],
  );
  assert.equal(inspection.diffMessages(after, before).at(-1).change, "removed");
  assert.deepEqual(
    inspection
      .diffMessages([before[0], before[1]], [before[1], before[0]])
      .map((r) => r.change),
    ["changed", "changed"],
  );
});

test("tool diff finds schema changes as well as additions and removals", () => {
  const tool = (name, type) => ({
    type: "function",
    function: { name, parameters: { type } },
  });
  assert.deepEqual(
    inspection
      .diffTools(
        [tool("a", "string"), tool("b", "string")],
        [tool("a", "integer"), tool("c", "string")],
      )
      .map((r) => [r.name, r.change]),
    [
      ["a", "changed"],
      ["b", "removed"],
      ["c", "added"],
    ],
  );
});

test("file matching handles tool JSON, excludes other agents and unread future files", () => {
  const raw = "규칙\nORBIT-731";
  const events = [
    {
      id: 1,
      kind: "file.decode",
      agent_id: "other",
      input: { path: "other.md" },
      output: { text: raw },
    },
    {
      id: 2,
      kind: "file.decode",
      input: { path: "rules.md" },
      output: { text: raw },
    },
    request(3, [
      {
        role: "tool",
        content: JSON.stringify({ path: "rules.md", content: raw }),
      },
    ]),
    {
      id: 4,
      kind: "file.decode",
      input: { path: "future.md" },
      output: { text: raw },
    },
  ];
  assert.deepEqual(
    inspection.origins(events, 2, 0, "root").map((r) => r.event.id),
    [2],
  );
  assert.deepEqual(
    inspection.consumers(events, 1, "root").map((r) => r.indices),
    [[0]],
  );
  assert.deepEqual(inspection.consumers(events, 0, "root"), []);
  assert.deepEqual(inspection.consumers(events, 3, "root"), []);
});

test("tool message links compare complete objects, not coincidental answer words", () => {
  const message = { role: "tool", tool_call_id: "abc", content: "385" };
  const events = [
    { id: 1, kind: "context.append", output: { appended_message: message } },
    request(2, [{ role: "user", content: "385" }, message]),
  ];
  assert.deepEqual(inspection.consumers(events, 0, "root")[0].indices, [1]);
  assert.deepEqual(inspection.origins(events, 1, 0, "root"), []);
  assert.equal(inspection.origins(events, 1, 1, "root")[0].event.id, 1);
});

test("real public trace links decoded AGENTS.md to the frozen system message", () => {
  const record = JSON.parse(
    zlib.gunzipSync(
      fs.readFileSync(
        path.join(__dirname, "../samples/5eb8a17eef4a4594.json.gz"),
      ),
    ),
  );
  const at = record.events.findIndex(
    (e) => e.kind === "file.decode" && e.input?.path?.endsWith("AGENTS.md"),
  );
  const targets = inspection.consumers(record.events, at, record.id);
  assert.ok(targets.length > 0);
  assert.equal(
    targets[0].event.input.messages[targets[0].indices[0]].role,
    "system",
  );
  assert.ok(
    inspection
      .origins(record.events, targets[0].index, 0, record.id)
      .some((r) => r.index === at),
  );
  const final = inspection.snapshot(
    record.events,
    record.events.length - 1,
    record.id,
  );
  assert.ok(final.previous);
  assert.ok(final.messageDiff.some((r) => r.change === "added"));
});

test("measurement labels distinguish diagnostics, runtime reports and direct samples", () => {
  assert.equal(
    inspection.evidence({ kind: "model.tokenize" }).type,
    "diagnostic",
  );
  assert.equal(inspection.evidence({ kind: "runtime.cache" }).type, "reported");
  assert.equal(inspection.evidence({ kind: "runtime.prefill" }).type, "reported");
  assert.equal(inspection.evidence({ kind: "model.decode.report" }).type, "reported");
  assert.equal(inspection.evidence({ kind: "http.inference.response" }).type, "reported");
  assert.equal(
    inspection.evidence({ kind: "reference.cache.write" }).type,
    "direct",
  );
  assert.equal(
    inspection.evidence({ kind: "model.preview.unavailable" }).type,
    "missing",
  );
});

test("message sections preserve every character and reject mismatched source offsets", () => {
  const message = { content: "🧩 before\n규칙\nafter" };
  const parts = inspection.fragments(message, [
    { label: "규칙", kind: "rules", start: 2, text: "규칙" },
    { label: "없는 값", text: "absent" },
  ]);
  assert.equal(parts.map((part) => part.text).join(""), message.content);
  assert.equal(
    parts.find((part) => part.label === "규칙").start,
    message.content.indexOf("규칙"),
  );
  assert.equal(
    parts.some((part) => part.label === "없는 값"),
    false,
  );
});
