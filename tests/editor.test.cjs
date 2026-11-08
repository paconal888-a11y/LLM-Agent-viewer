const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../web/workbench.js"), "utf8");

function setup() {
  const state = {
    file: { path: "a.py", text: "old", sha256: "sha-a", editable: true },
    drafts: new Map(),
    edits: new Map(),
    catalog: { components: [] },
    view: "editor",
  };
  const nodes = new Map();
  const $ = (id) => {
    if (!nodes.has(id))
      nodes.set(id, {
        value: "submitted",
        textContent: "",
        classList: { add() {}, toggle() {} },
      });
    return nodes.get(id);
  };
  const pending = [];
  const api = (url, payload) =>
    new Promise((resolve, reject) =>
      pending.push({ url, payload, resolve, reject }),
    );
  const sandbox = vm.createContext({
    state,
    $,
    api,
    busy: () => false,
    view: () => {},
    editorLines() {},
    highlightFiles() {},
  });
  for (const name of ["openFile", "saveFile", "editMessage"]) {
    const start = source.search(new RegExp(`  (?:async )?function ${name}\\(`));
    const rest = source.slice(start + 1);
    const end = rest.search(/\n  (?:async )?function /);
    vm.runInContext(rest.slice(0, end), sandbox);
  }
  return { state, $, pending, sandbox };
}
const saved = {
  saved: true,
  sha256: "new-sha",
  edit_id: "edit-a",
  applies: "다음 실행에 적용",
};

test("typing during a delayed save stays dirty against submitted text", async () => {
  const { state, $, pending, sandbox } = setup();
  const done = sandbox.saveFile();
  assert.equal(pending[0].payload.text, "submitted");
  $("codeEditor").value = "newer unsaved";
  pending[0].resolve(saved);
  await done;
  assert.equal(state.file.text, "submitted");
  assert.equal(state.file.sha256, "new-sha");
  assert.equal(state.drafts.get("a.py"), "newer unsaved");
  assert.match($("editState").textContent, /미저장/);
});

test("saving A cannot overwrite B after switching files", async () => {
  const { state, pending, sandbox } = setup();
  state.drafts.set("a.py", "submitted");
  const done = sandbox.saveFile();
  state.file = { path: "b.py", text: "B", sha256: "sha-b" };
  pending[0].resolve(saved);
  await done;
  assert.equal(state.file.sha256, "sha-b");
  assert.equal(state.file.text, "B");
  assert.equal(state.drafts.has("a.py"), false);
  assert.equal(state.edits.get("a.py"), "edit-a");
});

test("failed or rejected saves preserve editor and baseline", async () => {
  for (const rejected of [false, true]) {
    const { state, $, pending, sandbox } = setup();
    state.drafts.set("a.py", "newer unsaved");
    const done = sandbox.saveFile();
    $("codeEditor").value = "newer unsaved";
    if (rejected) {
      pending[0].reject(new Error("network"));
      await assert.rejects(done);
    } else {
      pending[0].resolve({ saved: false, validation: { message: "invalid" } });
      await done;
    }
    assert.equal(state.file.text, "old");
    assert.equal(state.file.sha256, "sha-a");
    assert.equal(state.drafts.get("a.py"), "newer unsaved");
    assert.equal(state.pendingSaves.size, 0);
  }
});

test("older open response cannot replace the latest selected file", async () => {
  const { state, $, pending, sandbox } = setup();
  $("codeEditor").value = "old";
  const a = sandbox.openFile("a.py");
  const b = sandbox.openFile("b.py");
  pending[1].resolve({
    path: "b.py",
    text: "B",
    sha256: "sha-b",
    editable: true,
  });
  await b;
  pending[0].resolve({
    path: "a.py",
    text: "A",
    sha256: "sha-a",
    editable: true,
  });
  await a;
  assert.equal(state.file.path, "b.py");
  assert.equal($("codeEditor").value, "B");
  assert.equal(state.drafts.size, 0);
});

test("open refetches when an in-flight save changed the disk revision", async () => {
  const { state, pending, sandbox } = setup();
  const save = sandbox.saveFile();
  const open = sandbox.openFile("a.py");
  pending[0].resolve(saved);
  await save;
  pending[1].resolve({
    path: "a.py",
    text: "old",
    sha256: "sha-a",
    editable: true,
  });
  await new Promise(setImmediate);
  assert.equal(pending.length, 3);
  pending[2].resolve({
    path: "a.py",
    text: "submitted",
    sha256: "new-sha",
    editable: true,
  });
  await open;
  assert.equal(state.file.sha256, "new-sha");
  assert.equal(state.file.text, "submitted");
});
