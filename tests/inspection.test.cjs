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

test("declared origins resolve child-local IDs without crossing agents or time", () => {
  const events = [
    { id: 9, agent_id: "root", agent_event_id: 3 },
    { id: 10, agent_id: "child", agent_event_id: 3 },
    request(11, [{ role: "user", content: "repair" }], {
      agent_id: "child",
      output: {
        segments: [{ message_index: 0, origin: "harness", source_event_id: 3 }],
      },
    }),
    { id: 12, agent_id: "child", agent_event_id: 3 },
  ];
  assert.deepEqual(
    inspection.declaredOrigins(events, 2, 0, "root").map((row) => row.event.id),
    [10],
  );
  assert.deepEqual(inspection.declaredOrigins(events, 2, 1, "root"), []);
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
