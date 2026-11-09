const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");
const story = require("../web/story.js");

const root = path.join(__dirname, "..", "samples");
const index = JSON.parse(fs.readFileSync(path.join(root, "index.json"), "utf8"));
const load = (item) => JSON.parse(zlib.gunzipSync(fs.readFileSync(path.join(root, item.id + ".json.gz"))));
const runs = index.map((item) => ({ slug: item.slug, run: load(item) }));
const of = (slug) => runs.find((entry) => entry.slug === slug).run;

test("every recorded event belongs to exactly one scene, in order", () => {
  for (const { slug, run } of runs) {
    const list = story.scenes(run.events, run);
    assert.equal(list[0].start, 0, slug);
    assert.equal(list.at(-1).end, run.events.length - 1, slug);
    list.forEach((scene, order) => {
      if (order) assert.equal(scene.start, list[order - 1].end + 1, `${slug} ${order}`);
      assert.ok(scene.focus >= scene.start && scene.focus <= scene.end, `${slug} ${scene.kind}`);
    });
  }
});

test("a run reads as a short story, not as its raw log", () => {
  for (const { slug, run } of runs) {
    const list = story.scenes(run.events, run),
      models = run.events.filter((e) => e.kind === "model.request").length,
      tools = run.events.filter((e) => e.kind === "tool.dispatch").length;
    // 모델 호출마다 완료 검사가 돌려보낼 수 있으므로 호출당 두 장면까지 생긴다.
    assert.ok(list.length <= models * 2 + tools + 6, `${slug}: ${list.length}`);
    assert.ok(list.length < run.events.length / 4, `${slug}: ${list.length} / ${run.events.length}`);
  }
});

test("each scene says what happened in plain text with values from the record", () => {
  const owners = new Set(["code", "model", "config", "runtime", "external", "user"]);
  for (const { slug, run } of runs)
    for (const scene of story.scenes(run.events, run)) {
      const label = `${slug} ${scene.number} ${scene.kind}`;
      assert.ok(scene.title, label);
      assert.ok(owners.has(scene.owner), label);
      const text = JSON.stringify([scene.title, scene.summary, scene.stats, scene.note]);
      assert.ok(!/undefined|NaN|\[object/.test(text), `${label}: ${text}`);
    }
});

test("the order of a tool run matches what the record shows", () => {
  const run = of("verified-write");
  assert.deepEqual(
    story.scenes(run.events, run).map((scene) => scene.kind),
    ["request", "prepare", "context", "model", "tool", "model", "tool", "model", "tool", "model", "finish"],
  );
});

test("model scenes state whether a tool was requested, using the recorded call", () => {
  const run = of("verified-write"),
    list = story.scenes(run.events, run),
    first = list.find((scene) => scene.kind === "model");
  assert.equal(first.title, "모델이 read_file 도구를 요청했습니다");
  assert.equal(first.owner, "model");
  assert.equal(run.events[first.focus].kind, "model.response");
  assert.match(first.summary, /learning-demo/);
  const last = list.filter((scene) => scene.kind === "model").at(-1);
  assert.equal(last.title, "모델이 도구를 쓰지 않고 답을 썼습니다");
  const tool = list.find((scene) => scene.kind === "tool");
  assert.equal(tool.title, "코드가 검사한 뒤 read_file 도구를 실행했습니다");
  assert.equal(run.events[tool.focus].kind, "tool.result");
});

test("the input scene lists what the model receives and which tools it may use", () => {
  const run = of("verified-write"),
    scene = story.scenes(run.events, run).find((item) => item.kind === "context"),
    request = run.events.find((e) => e.kind === "model.request");
  assert.match(scene.summary, new RegExp(`메시지 ${request.input.messages.length}개`));
  assert.match(scene.summary, new RegExp(`도구 ${request.input.tools.length}개`));
  assert.equal(scene.blocks.find((block) => block.type === "chat").items.length, request.input.messages.length);
  assert.equal(scene.inputEvent, request.id);
});

test("failed checks, summaries, delegation and errors get their own scene", () => {
  const kinds = (slug) => story.scenes(of(slug).events, of(slug)).map((scene) => scene.kind);
  assert.ok(kinds("feedback").includes("repair"));
  assert.ok(kinds("turn-compaction").includes("compaction"));
  assert.ok(kinds("team").includes("agents"));
  const guard = story.scenes(of("repeat-guard").events, of("repeat-guard")).at(-1);
  assert.equal(guard.title, "실행이 오류로 끝났습니다");
  assert.match(guard.summary, /3회/);
});

test("summary requests are not counted as agent model calls", () => {
  const run = of("turn-compaction"),
    list = story.scenes(run.events, run),
    summaries = run.events.filter((e) => e.kind === "model.request" && e.model_purpose === "compaction").length,
    all = run.events.filter((e) => e.kind === "model.request").length;
  assert.ok(summaries > 0);
  assert.equal(list.filter((scene) => scene.kind === "model").length, all - summaries);
});

test("a stopped run ends without pretending it finished", () => {
  const run = of("resume-stopped"),
    list = story.scenes(run.events, run);
  assert.ok(!list.some((scene) => scene.kind === "finish"));
  assert.equal(story.overview(run.events, run).statusLabel, "중지됨");
});

test("completion wording does not claim the answer was verified", () => {
  const run = of("verified-write"),
    finish = story.scenes(run.events, run).at(-1);
  assert.match(finish.title, /완료 조건 \d+개를 확인/);
  assert.match(finish.note, /검사한 범위까지만/);
  const noConditions = story.scenes(
    [
      { id: 1, kind: "validation.final", output: { passed: true } },
      { id: 2, kind: "output.answer" },
      { id: 3, kind: "run.complete" },
    ],
    { requirements: [] },
  )[0];
  assert.equal(noConditions.title, "검사할 조건 없이 완료로 끝났습니다");
});

test("the overview adds up the run and sceneAt finds the scene for any event", () => {
  const run = of("verified-write"),
    summary = story.overview(run.events, run),
    list = story.scenes(run.events, run);
  assert.equal(summary.statusLabel, "완료");
  assert.equal(summary.modelCalls, run.model_calls);
  assert.ok(summary.answer.length > 0);
  run.events.forEach((_, cursor) => {
    const scene = list[story.sceneAt(list, cursor)];
    assert.ok(cursor >= scene.start && cursor <= scene.end);
  });
});
