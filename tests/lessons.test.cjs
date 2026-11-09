const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");
const course = require("../web/lessons.js");

const root = path.join(__dirname, "..", "samples");
const index = JSON.parse(fs.readFileSync(path.join(root, "index.json"), "utf8"));
const cache = new Map();
function events(slug) {
  if (!cache.has(slug)) {
    const item = index.find((entry) => entry.slug === slug);
    assert.ok(item, "샘플 없음: " + slug);
    cache.set(slug, JSON.parse(zlib.gunzipSync(fs.readFileSync(path.join(root, item.id + ".json.gz")))).events);
  }
  return cache.get(slug);
}
const TABS = new Set(["change", "code", "input", "values", "learn"]);

test("the feedback address carries only the version and a fixed screen name", () => {
  const lesson = new URL(
    course.feedbackAddress({ version: "9.9.9", demo: true, lesson: "tools", step: 0, run: "sample-x", view: "journey" }),
  );
  assert.equal(lesson.origin + lesson.pathname, "https://github.com/paconal888-a11y/LLM-Agent-viewer/issues/new");
  assert.equal(lesson.searchParams.get("template"), "stuck.yml");
  assert.equal(lesson.searchParams.get("version"), "9.9.9");
  assert.equal(lesson.searchParams.get("screen"), "웹 데모 · 강의 tools 1단계");

  const sample = new URL(course.feedbackAddress({ mode: "sample", run: "sample-tool-basic", view: "journey" }));
  assert.equal(sample.searchParams.get("screen"), "PC 샘플 보기 · 기록 tool-basic");

  const real = new URL(course.feedbackAddress({ mode: "live", run: "20261109-private", view: "journey" }));
  assert.equal(real.searchParams.get("screen"), "PC 실행 · 구조 · 실행");
  assert.ok(!real.href.includes("private"), real.href);

  const odd = new URL(course.feedbackAddress({ mode: "live", view: "C:\\Users\\someone", lesson: "../secret" }));
  assert.equal(odd.searchParams.get("screen"), "PC 실행");
});

test("the issue form has the fields the feedback address fills in", () => {
  const form = fs.readFileSync(path.join(__dirname, "..", ".github", "ISSUE_TEMPLATE", "stuck.yml"), "utf8");
  for (const id of ["screen", "version"]) assert.match(form, new RegExp(`^\\s+id: ${id}$`, "m"));
});

test("issue form names are long enough for GitHub to list them", () => {
  const folder = path.join(__dirname, "..", ".github", "ISSUE_TEMPLATE");
  for (const file of fs.readdirSync(folder)) {
    const name = fs.readFileSync(path.join(folder, file), "utf8").match(/^name: (.+?)\s*$/m);
    assert.ok(name && [...name[1]].length >= 3, file);
  }
});

test("every lesson step points at a real event in a real sample", () => {
  for (const lesson of course.lessons)
    lesson.steps.forEach((step, number) => {
      const label = `${lesson.id} ${number + 1}단계`;
      const list = events((step.sample || lesson.sample));
      const at = course.locate(list, step);
      assert.ok(at >= 0, label + ": 이벤트를 찾지 못함 " + step.kind);
      assert.ok(TABS.has(step.tab || "change"), label);
      assert.ok(step.title && step.body, label);
      if (step.tab === "input") assert.equal(list[at].kind, "model.request", label);
      if (step.messageIncludes)
        assert.ok(course.messageIndex(list[at], step) >= 0, label + ": 메시지를 찾지 못함 " + step.messageIncludes);
      if (step.tab === "code") assert.ok(list[at].source, label + ": 코드 위치 없음");
    });
});

test("every lesson teaches something and offers a next action", () => {
  const ids = new Set();
  const chapters = new Set(course.chapters.map((chapter) => chapter.id));
  for (const lesson of course.lessons) {
    assert.ok(!ids.has(lesson.id), lesson.id);
    ids.add(lesson.id);
    assert.ok(chapters.has(lesson.chapter), lesson.id);
    assert.ok(lesson.question && lesson.takeaway && lesson.steps.length >= 4, lesson.id);
    assert.ok(lesson.tryIt?.text && (lesson.tryIt.file || lesson.tryIt.view || lesson.tryIt.compose), lesson.id);
  }
  for (const chapter of course.chapters)
    assert.ok(course.lessons.some((lesson) => lesson.chapter === chapter.id), chapter.id);
});

test("files a lesson asks you to open exist in the project", () => {
  for (const lesson of course.lessons)
    if (lesson.tryIt.file)
      assert.ok(fs.existsSync(path.join(__dirname, "..", lesson.tryIt.file)), lesson.tryIt.file);
});

test("numbers quoted in lesson text match the recorded events", () => {
  const first = events("verified-write").findIndex((e) => e.kind === "model.request");
  assert.equal(first, 45);
  assert.ok(events("verified-write").slice(0, first).every((e) => !e.kind.startsWith("model.")));
  assert.equal(events("verified-write").find((e) => e.kind === "model.tokenize").output.token_count, 1105);
  const limits = events("rules-layers").find((e) => e.kind === "config.resolve").output.settings.find((s) => s.key === "loop.max_model_calls");
  assert.deepEqual(limits.layers.map((l) => l.value), [8, 5, 6]);
  const save = events("resume-stopped").find((e) => e.kind === "checkpoint.save").output;
  assert.deepEqual([save.bytes, save.messages], [13465, 4]);
  assert.equal(events("resume-continued").find((e) => e.kind === "run.resume").output.restored_messages, 6);
  assert.equal(events("turn-compaction").find((e) => e.kind === "context.prune").output.pruned[0].original_characters, 723);
  const firstRequest = events("verified-write").find((e) => e.kind === "model.request").input;
  assert.deepEqual([firstRequest.messages.length, firstRequest.tools.length], [4, 6]);
  const summary = events("compaction").find((e) => e.kind === "memory.compaction.result").output;
  assert.deepEqual([summary.before_characters, summary.after_characters], [2488, 73]);
  const budget = events("turn-compaction").find((e) => e.kind === "context.check").input;
  assert.equal(budget.threshold_tokens, 1250);
  assert.deepEqual(events("tensors").find((e) => e.kind === "reference.embedding").output.tensor_shape, [1, 995, 1024]);
  const cacheWrites = events("tensors").filter((e) => e.kind === "reference.cache.write");
  assert.equal(cacheWrites[0].output.forward_input_tokens, 995);
  assert.equal(cacheWrites[1].output.forward_input_tokens, 1);
});

test("steps inside one recording move forward, and the team lesson ends on the main agent", () => {
  for (const lesson of course.lessons) {
    const method = lesson.id === "method";
    let previous = { slug: null, at: -1 };
    for (const step of lesson.steps) {
      const slug = step.sample || lesson.sample,
        at = course.locate(events(slug), step);
      // 14강은 증상에서 원인으로 거슬러 올라가므로 순서를 검사하지 않는다.
      if (!method && slug === previous.slug) assert.ok(at > previous.at, lesson.id + " " + step.title);
      previous = { slug, at };
    }
  }
  const team = course.byId("team"),
    last = team.steps.at(-1);
  assert.equal(events("team")[course.locate(events("team"), last)].agent_name, "주 에이전트");
});

test("locate counts matches, from the end when negative, and reports misses", () => {
  const list = [
    { kind: "tool.dispatch", input: { function: { name: "a" } } },
    { kind: "x" },
    { kind: "tool.dispatch", input: { function: { name: "b" } } },
  ];
  assert.equal(course.locate(list, { kind: "tool.dispatch" }), 0);
  assert.equal(course.locate(list, { kind: "tool.dispatch", nth: 2 }), 2);
  assert.equal(course.locate(list, { kind: "tool.dispatch", nth: -1 }), 2);
  assert.equal(course.locate(list, { kind: "tool.dispatch", tool: "b" }), 2);
  assert.equal(course.locate(list, { kind: "tool.dispatch", nth: 3 }), -1);
  assert.equal(course.locate(list, { kind: "none" }), -1);
});

test("routes parse lessons and runs and reject malformed step numbers", () => {
  assert.deepEqual(course.parseRoute("#/lessons"), { name: "lessons" });
  assert.deepEqual(course.parseRoute("#/lesson/skills/3"), { name: "lesson", id: "skills", step: 2 });
  assert.deepEqual(course.parseRoute("#/lesson/skills/99"), { name: "lesson", id: "skills", step: 0 });
  assert.equal(course.parseRoute("#/lesson/none/1"), null);
  assert.deepEqual(course.parseRoute("#/run/sample-team/12/input"), { name: "run", id: "sample-team", event: 12, tab: "input" });
  assert.deepEqual(course.parseRoute("#/view/memory"), { name: "view", view: "memory" });
  assert.deepEqual(course.parseRoute("#/view/memory/sample-compaction"), { name: "view", view: "memory", run: "sample-compaction" });
  assert.equal(course.parseRoute("#/view/secret"), null);
  assert.equal(course.parseRoute(""), null);
  assert.equal(course.parseRoute("#/unknown"), null);
});
