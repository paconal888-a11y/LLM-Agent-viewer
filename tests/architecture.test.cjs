const test = require("node:test");
const assert = require("node:assert/strict");
const graph = require("../web/architecture.js");

test("every event mapping points to an existing component and edge", () => {
  const ids = new Set(graph.nodes.map((node) => node.id));
  const edges = new Set(graph.edges.map(([id]) => id));
  const kinds = [
    "file.read",
    "file.decode",
    "rules.wrap",
    "context.assemble",
    "context.append",
    "model.request",
    "model.response",
    "model.stream.chunk",
    "model.tokenize",
    "reference.embedding",
    "reference.cache.write",
    "reference.attention",
    "reference.sample",
    "tool.validate",
    "tool.result",
    "mcp.send",
    "mcp.receive",
    "lsp.send",
    "lsp.receive",
    "agent.spawn",
    "agent.join",
    "memory.compaction.start",
    "memory.summary.include",
    "browser.render",
  ];
  for (const kind of kinds) {
    const location = graph.locate({ kind, input: { path: "AGENTS.md" } });
    assert.ok(ids.has(location.node), kind);
    location.edges.forEach((edge) =>
      assert.ok(edges.has(edge), `${kind}: ${edge}`),
    );
  }
  for (const node of graph.nodes) {
    assert.ok(node.x >= 0 && node.x + node.w <= 1100, node.id);
    assert.ok(node.y >= 0 && node.y + node.h <= 430, node.id);
  }
});

test("protocol direction, file ownership and summary requests are distinct", () => {
  assert.deepEqual(graph.locate({ kind: "mcp.send" }).edges, ["tool-mcp"]);
  assert.deepEqual(graph.locate({ kind: "mcp.receive" }).edges, ["mcp-tool"]);
  assert.deepEqual(graph.locate({ kind: "lsp.receive" }).edges, ["lsp-tool"]);
  assert.deepEqual(
    graph.locate({ kind: "file.read", input: { path: "unknown.json" } }).edges,
    [],
  );
  assert.deepEqual(
    graph.locate({ kind: "file.read", input: { path: "AGENTS.md" } }).edges,
    ["rules-read"],
  );
  assert.equal(
    graph.locate({ kind: "model.request", model_purpose: "compaction" }).node,
    "compaction",
  );
  assert.equal(
    graph.locate({ kind: "model.request", model_purpose: "answer" }).node,
    "loop",
  );
});
