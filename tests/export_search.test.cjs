// 내보내기 미리보기(EXPORT_PREVIEW)와 본문 검색(BODY_SEARCH) 화면 조각을 DOM 없이 검사한다.
// 서버 API 응답은 고정 입력이며(모의), 실제 기록의 값이 아니다. 화면이 응답을 어떻게 표시하고 어디로 이동시키는지만 본다.
const test = require("node:test");
const { Script, createContext } = require("node:vm");
const { readFileSync } = require("node:fs");

// 층별 원인 정리 화면. 서버 응답은 모의 데이터다.
function diagnosisScope() {
  const source = readFileSync(require.resolve("../web/workbench.js"), "utf8");
  const start = source.indexOf("  function diagnosisHtml(report) {");
  const end = source.indexOf("  function renderLearningFlow(body) {");
  const context = createContext({
    esc: (value) => String(value).replace(/</g, "&lt;"),
  });
  new Script(source.slice(start, end)).runInContext(context);
  return context;
}

test("diagnosis shows the focused layer, its evidence links and the unverified scope", () => {
  const html = diagnosisScope().diagnosisHtml({
    headline: "기록은 ‘모델의 선택’ 층을 먼저 살펴보라고 가리킵니다.",
    focus: "model",
    next_step: "반복 측정",
    note: "증명하지 않습니다",
    verification: { by_code: [], not_checked: ["최종 답변 문장의 사실 여부"] },
    layers: [
      { id: "runtime", label: "실행기·설정", status: "clear", checks: [] },
      {
        id: "model",
        label: "모델의 선택",
        status: "issue",
        checks: [
          { id: "x", question: "호출했는가", result: "fail", detail: "호출 0회: <add>", event_id: 12 },
        ],
      },
    ],
  });
  require("node:assert/strict").match(html, /먼저 볼 곳/);
  require("node:assert/strict").match(html, /data-event="12"/);
  require("node:assert/strict").match(html, /호출 0회: &lt;add>/);
  require("node:assert/strict").match(html, /코드가 확인하지 않은 것/);
  require("node:assert/strict").doesNotMatch(html, /코드가 확인한 것/);
  require("node:assert/strict").match(html, /판단할 기록 없음|기록상 문제 없음/);
});

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
function element(extra = {}) {
  return {
    innerHTML: "",
    textContent: "",
    value: "",
    checked: false,
    disabled: false,
    open: true,
    classes: new Set(),
    classList: {
      add(c) { this.owner.classes.add(c); },
      remove(c) { this.owner.classes.delete(c); },
      toggle(c, force) { if (force === undefined) force = !this.owner.classes.has(c); if (force) this.owner.classes.add(c); else this.owner.classes.delete(c); },
      contains(c) { return this.owner.classes.has(c); },
    },
    addEventListener() {},
    ...extra,
  };
}
function setup(apiImpl) {
  const nodes = new Map();
  const make = (id, extra) => { const el = element(extra); el.classList.owner = el; nodes.set(id, el); return el; };
  for (const id of ["bodySearch", "bodySearchKind", "bodySearchResults", "export", "closeExport", "exportOriginal", "exportShare", "exportPreview", "exportNote", "exportRun", "exportDialog"]) make(id);
  const options = {};
  make("exportOptions", {
    querySelector(selector) {
      const key = selector.match(/data-option="([^"]+)"/)[1];
      return (options[key] ??= { checked: ["paths", "secrets", "emails", "ips"].includes(key) });
    },
  });
  const tray = element(); tray.classList.owner = tray;
  const calls = [];
  const sandbox = vm.createContext({
    $: (id) => { assert.ok(nodes.has(id), "unknown element " + id); return nodes.get(id); },
    document: { querySelector: (s) => (s === ".recordtray" ? tray : null) },
    window: { location: { assign(url) { calls.push(url); } } },
    state: { run: { id: "run-1" }, live: false },
    eventNames: { "model.request": "모델 요청", "file.decode": "UTF-8 해석", "context.assemble": "messages 조립" },
    esc: (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]),
    num: (v) => (typeof v === "number" ? v.toLocaleString() : "—"),
    toast(message) { calls.push("toast:" + message); },
    api: async (url) => { calls.push(url); return apiImpl(url); },
    encodeURIComponent,
    setTimeout, clearTimeout,
  });
  vm.runInContext(block("EXPORT_PREVIEW") + "\n" + block("BODY_SEARCH"), sandbox);
  return { nodes, sandbox, calls, options, tray };
}

test("본문 검색 결과는 모델 요청의 messages 적중만 '모델 입력' 탭으로, 나머지는 '데이터 변화' 탭으로 보낸다", async () => {
  const { nodes, sandbox, tray } = setup(async () => ({
    query: "규칙", count: 3, truncated: false,
    by_kind: { "model.request": 1, "file.decode": 1, "context.assemble": 1 },
    scope: "이벤트의 input·output 본문",
    hits: [
      { event_id: 19, kind: "file.decode", field: "output.text", snippet: "…사용자에게\n한국어로 답한다…" },
      { event_id: 42, kind: "context.assemble", field: "output.messages[0].content", message_index: 0, role: "system", snippet: "x" },
      { event_id: 46, kind: "model.request", field: "input.messages[0].content", message_index: 0, role: "system", origin: "harness", request_number: 1, snippet: "y" },
    ],
  }));
  nodes.get("bodySearch").value = "규칙";
  await vm.runInContext("runBodySearch()", sandbox);
  const html = nodes.get("bodySearchResults").innerHTML;
  assert.ok(!nodes.get("bodySearchResults").classes.has("hidden"));
  assert.ok(tray.classes.has("searching"), "검색 중에는 트레이가 결과 모드가 된다");
  assert.match(html, /data-inspection-event="46" data-inspection-tab="input" data-inspection-message="0"/);
  assert.match(html, /data-inspection-event="42" data-inspection-tab="change"/, "context.assemble의 messages 적중은 모델 입력 탭으로 보내지 않는다");
  assert.match(html, /data-inspection-event="19" data-inspection-tab="change" data-inspection-message="-1"/);
  assert.match(html, /요청 #1 · 모델 요청/);
  assert.match(html, /messages\[0\] · system · 작성: 하네스/);
  assert.match(html, /messages\[0\] · system · 작성 주체 미기록/);
  assert.match(html, /3건/);
  assert.ok(!html.includes("\n한국어"), "발췌의 줄바꿈은 한 칸으로 접는다");
  nodes.get("bodySearch").value = "";
  await vm.runInContext("runBodySearch()", sandbox);
  assert.ok(nodes.get("bodySearchResults").classes.has("hidden"));
  assert.ok(!tray.classes.has("searching"));
});

test("본문 검색은 kind 필터를 쿼리에 넣고, 늦게 도착한 이전 응답은 버린다", async () => {
  let resolveSlow;
  const { nodes, sandbox, calls } = setup((url) =>
    url.includes("q=slow") ? new Promise((r) => (resolveSlow = r)) : Promise.resolve({ hits: [], count: 0, by_kind: {}, truncated: false }),
  );
  nodes.get("bodySearch").value = "slow";
  nodes.get("bodySearchKind").value = "tool.result";
  const slow = vm.runInContext("runBodySearch()", sandbox);
  nodes.get("bodySearch").value = "fast";
  await vm.runInContext("runBodySearch()", sandbox);
  assert.ok(calls.some((u) => u.includes("/api/runs/run-1/search?q=slow&kind=tool.result")));
  resolveSlow({ hits: [{ event_id: 1, kind: "tool.result", field: "output.content", snippet: "SLOW" }], count: 1, by_kind: {}, truncated: false });
  await slow;
  assert.ok(!nodes.get("bodySearchResults").innerHTML.includes("SLOW"), "먼저 보낸 검색의 늦은 응답은 표시하지 않는다");
});

test("내보내기 미리보기는 옵션을 쿼리로 보내고 치환 횟수·예시·note를 표시하며, 사본 저장 주소에 같은 옵션을 쓴다", async () => {
  const { nodes, sandbox, calls, options } = setup(async (url) => {
    assert.match(url, /\/api\/runs\/run-1\/export\/preview\?paths=1&secrets=1&emails=1&ips=1&omit_file_text=0&omit_sources=1$/);
    return {
      counts: { path: 4, secret: 1 },
      examples: { path: ["C:\\Users\\someone\\x"], secret: ["sk-abc…"] },
      includes: { prompt: true, file_texts: 2, tool_results: 1, model_requests: 3, source_snapshots: 5, browser_input: 1 },
      event_count: 77,
      note: "정규식 기반이라 모든 비밀값을 찾는다고 보증하지 않습니다.",
    };
  });
  options.omit_sources = { checked: true };
  assert.equal(vm.runInContext("exportQuery()", sandbox), "paths=1&secrets=1&emails=1&ips=1&omit_file_text=0&omit_sources=1");
  await vm.runInContext("loadExportPreview()", sandbox);
  const html = nodes.get("exportPreview").innerHTML;
  assert.match(html, /경로[^<]*<\/th><td>4회<\/td><td><code>C:\\Users\\someone\\x<\/code>/);
  assert.match(html, /비밀값[^<]*<\/th><td>1회<\/td>/);
  assert.match(html, /기록 77개 · 프롬프트 포함 · 파일 원문 2개/);
  assert.equal(nodes.get("exportNote").textContent, "정규식 기반이라 모든 비밀값을 찾는다고 보증하지 않습니다.");
  assert.equal(nodes.get("exportShare").disabled, false);
  nodes.get("exportShare").onclick();
  nodes.get("exportOriginal").onclick();
  assert.ok(calls.includes("/api/runs/run-1/export/share?paths=1&secrets=1&emails=1&ips=1&omit_file_text=0&omit_sources=1"));
  assert.ok(calls.includes("/api/runs/run-1/export"), "원본 저장은 치환 없이 기존 주소를 쓴다");
});
