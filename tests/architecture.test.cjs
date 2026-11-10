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
    assert.ok(node.y >= 0 && node.y + node.h <= 600, node.id);
  }
});

test("protocol direction, file ownership and summary requests are distinct", () => {
  assert.equal(graph.locate({ kind: "context.check" }).node, "compaction");
  assert.equal(graph.locate({ kind: "context.prune" }).node, "compaction");
  assert.deepEqual(graph.locate({ kind: "context.compact.result" }).edges, ["compact-context"]);
  assert.equal(graph.locate({ kind: "checkpoint.save" }).node, "memory");
  assert.equal(graph.locate({ kind: "config.resolve" }).node, "loop");
  assert.equal(graph.locate({ kind: "run.resume" }).node, "loop");
  assert.equal(graph.locate({ kind: "rules.directory" }).node, "rules");
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

test("the diagram area is separate from anything that grows or shrinks", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const html = fs.readFileSync(path.join(__dirname, "..", "web", "index.html"), "utf8");
  const css = fs.readFileSync(path.join(__dirname, "..", "web", "diagram.css"), "utf8");
  const rail = html.indexOf('class="rail"');
  const execution = html.indexOf('class="execution');
  const stage = html.indexOf('id="architecture"');
  assert.ok(execution < stage && stage < rail);
  for (const id of ["coach", "requestSummary", "promptForm", "inspector"]) {
    const at = html.indexOf(`id="${id}"`);
    assert.ok(at > rail, id);
  }
  // 구조도 위아래 줄은 높이를 정해 두고, 구조도만 남은 칸을 채운다.
  const rule = (selector) => {
    const start = css.lastIndexOf(`\n${selector} {`);
    assert.ok(start >= 0, selector);
    return css.slice(start, css.indexOf("}", start));
  };
  assert.match(rule(".architecture"), /flex: 1 1 0/);
  assert.match(rule(".currentstep"), /\bheight: 64px/);
  assert.match(rule(".diagram-caption"), /\bheight: 30px/);
  assert.match(rule(".execution .replay"), /\bheight: 44px/);
  assert.match(rule(".execution .controls"), /position: absolute/);
});

test("the prompt box stays in one place at the end of the right column", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const html = fs.readFileSync(path.join(__dirname, "..", "web", "index.html"), "utf8");
  const css = fs.readFileSync(path.join(__dirname, "..", "web", "diagram.css"), "utf8");
  const dock = html.indexOf('class="composerdock"');
  assert.ok(dock > html.indexOf('id="inspector"'));
  for (const id of ["promptForm", "prompt", "run", "newSession"]) assert.ok(html.indexOf(`id="${id}"`) > dock, id);
  assert.ok(html.indexOf('id="answerPanel"') > dock);
  assert.match(css, /^\.composerdock \{\s+position: sticky/m);
  const open = html.indexOf('id="prompt"');
  assert.ok(html.slice(open, html.indexOf("</textarea>", open)).trimEnd().endsWith(">"), "입력란은 비어 있는 상태로 시작한다");
});

test("internet search has its own place, separate from local tools", () => {
  assert.ok(graph.nodes.some((node) => node.id === "web"));
  assert.deepEqual(graph.locate({ kind: "web.request" }), { node: "web", edges: ["tool-web"] });
  assert.deepEqual(graph.locate({ kind: "web.response" }), { node: "web", edges: ["web-tool"] });
  const edges = new Set(graph.edges.map(([id]) => id));
  assert.ok(edges.has("tool-web") && edges.has("web-tool"));
});
