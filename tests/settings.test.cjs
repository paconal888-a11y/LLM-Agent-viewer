const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require.resolve("../web/workbench.js"), "utf8");

function block(name) {
  const start = source.indexOf("// " + name + "_START"),
    end = source.indexOf("// " + name + "_END");
  assert.ok(start > 0 && end > start, name + " 마커가 workbench.js에 있어야 한다");
  return source.slice(start, end);
}
function declaration(name) {
  const start = source.search(new RegExp(`  (?:async )?function ${name}\\(`));
  assert.ok(start > 0, name);
  const rest = source.slice(start + 1);
  return rest.slice(0, rest.search(/\n  (?:async )?function |\n  \/\/ /));
}
function setup({ manifest } = {}) {
  const nodes = new Map();
  const control = (id, extra) =>
    nodes.set(id, {
      id,
      value: "",
      checked: false,
      disabled: false,
      min: "",
      max: "",
      textContent: "",
      innerHTML: "",
      open: false,
      classList: { add() {}, remove() {}, toggle() {} },
      ...extra,
    });
  control("useKv", { checked: true });
  control("prefixCache", { checked: true });
  control("stream", { checked: true });
  control("maxTokens", { value: "256", min: "1", max: "256" });
  control("temperature", { value: "0.2", min: "0", max: "2" });
  control("captureLayer", { value: "0", min: "0", max: "27" });
  control("seed", { value: "42", min: "0", max: "4294967295" });
  control("topK", { value: "40", min: "0", max: "1000" });
  control("topP", { value: "0.95", min: "0", max: "1" });
  for (const id of ["agentMode", "prompt", "requirements", "stepMode", "backend", "restoreNote", "configuration"])
    control(id);
  nodes.get("backend").value = "reference";
  const $ = (id) => {
    assert.ok(nodes.has(id), "unknown control " + id);
    return nodes.get(id);
  };
  const features = { rules: true, memory: true, compaction: false, turn_compaction: true, skills: true, tools: true, commands: true, writes: false, mcp: false, lsp: false, feedback_recall: false, planning: false };
  const state = { session: "session-x", ready: true };
  const sandbox = vm.createContext({
    $,
    state,
    features,
    esc: (v) => String(v ?? ""),
    modeUI() {},
    featureUI() {},
    api: async (url) => {
      if (url === "/api/project/manifest") return manifest || { version: "v-current", files: [], workspace: { sha256: "ws-current", files: [] } };
      throw new Error("unexpected " + url);
    },
  });
  const engineFieldsStart = source.indexOf("  const engineFields = {"),
    engineFieldsEnd = source.indexOf("};", engineFieldsStart) + 2;
  vm.runInContext(source.slice(engineFieldsStart, engineFieldsEnd), sandbox);
  vm.runInContext(block("ENGINE_RESTORE"), sandbox);
  vm.runInContext(declaration("restoreNote"), sandbox);
  vm.runInContext(declaration("engine"), sandbox);
  vm.runInContext(block("RUN_IMPORT"), sandbox);
  return { $, state, features, sandbox };
}

const recordedRun = {
  id: "abc123",
  prompt: "data/learning-demo.json을 읽고 practice 값을 true로 바꿔 저장한 뒤 check_file로 검사해줘.",
  mode: "plan",
  session_id: "lesson-1",
  step_mode: true,
  features: { rules: true, memory: false, skills: false, tools: true, commands: false, writes: true },
  requirements: [{ tool: "check_file", arguments: { path: "data/learning-demo.json" } }],
  engine: { cache_prompt: false, stream: false, use_kv: true, capture_layer: 12, max_tokens: 768, temperature: 0.7, top_k: 1, top_p: 0.1, seed: 7 },
  conditions: { backend: "llama", model: "qwen3-4b", workspace: { sha256: "ws-current", files: [] } },
  implementation: { version: "v-current", files: [] },
};

test("importing a past run restores every generation setting, not only prompt and features", async () => {
  const { $, state, features, sandbox } = setup();
  const report = await sandbox.importRunSettings(recordedRun);
  assert.equal($("prompt").value, recordedRun.prompt);
  assert.equal($("agentMode").value, "plan");
  assert.equal($("stepMode").checked, true);
  assert.equal(state.session, "lesson-1");
  assert.deepEqual(JSON.parse($("requirements").value), recordedRun.requirements);
  assert.equal(features.writes, true);
  assert.equal(features.memory, false);
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox.engine())), recordedRun.engine);
  assert.ok(report.restored.some((r) => r.startsWith("생성 설정 9/9")));
  assert.ok(report.restored.includes("하네스 코드 동일"));
  assert.ok(report.restored.includes("작업 공간 파일 동일"));
});

test("conditions that cannot be reproduced are reported instead of silently adjusted", async () => {
  const { $, sandbox } = setup({
    manifest: {
      version: "v-other",
      files: [{ path: "local_agent_lab/policy.py", sha256: "changed" }],
      workspace: { sha256: "ws-other", files: [{ path: "AGENTS.md", sha256: "new" }] },
    },
  });
  const run = {
    ...recordedRun,
    sample: { title: "x" },
    implementation: { version: "v-recorded", files: [{ path: "local_agent_lab/policy.py", sha256: "old" }] },
    conditions: { backend: "llama", model: "qwen3-4b", workspace: { sha256: "ws-recorded", files: [{ path: "AGENTS.md", sha256: "old" }] } },
  };
  const report = await sandbox.importRunSettings(run);
  const text = report.issues.join("\n");
  assert.match(text, /실행기: 기록은 llama/);
  assert.match(text, /최대 출력 토큰: 기록 768는 현재 실행기 범위\(1–256\) 밖/);
  assert.match(text, /하네스 코드가 당시와 다릅니다: policy.py/);
  assert.match(text, /작업 공간 파일이 당시와 다릅니다: AGENTS.md/);
  assert.match(text, /재생 기록입니다/);
  assert.equal($("maxTokens").value, "768", "값은 그대로 넣고 범위 밖임을 알린다");
  assert.match($("restoreNote").innerHTML, /달라진 조건/);
});

test("runs recorded without engine settings keep the current values and say so", async () => {
  const { $, sandbox } = setup();
  const report = await sandbox.importRunSettings({ ...recordedRun, engine: undefined, session_id: undefined });
  assert.equal($("temperature").value, "0.2");
  assert.ok(report.issues.some((i) => i.startsWith("생성 설정: 기록에 없음")));
  assert.ok(report.issues.some((i) => i.startsWith("세션: 기록에 없어")));
});
