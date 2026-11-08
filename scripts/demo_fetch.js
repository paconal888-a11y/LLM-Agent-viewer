/* 웹 데모: /api 요청을 미리 만든 JSON으로 답한다. 저장 기록 재생만 제공하며 모델을 호출하지 않는다. */
(() => {
  "use strict";
  const original = window.fetch.bind(window);
  const base = new URL("data/", document.baseURI);
  const reply = (value, status = 200) =>
    new Response(JSON.stringify(value), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  const read = async (name) => {
    const response = await original(new URL(name, base));
    if (!response.ok) throw new Error("데모 자료를 찾을 수 없습니다: " + name);
    return response.json();
  };
  const runs = new Map();
  const run = (id) => {
    if (!runs.has(id)) runs.set(id, read(`runs/${id}.json`));
    return runs.get(id);
  };
  let fileNumbers;
  const staticRoutes = {
    "/api/status": "status.json",
    "/api/samples": "samples.json",
    "/api/implementation": "implementation.json",
    "/api/project": "project.json",
    "/api/project/manifest": "manifest.json",
    "/api/experiments/cases": "cases.json",
  };
  async function answer(path, query) {
    if (path === "/api/activity") return reply({ run: null, experiment: null });
    if (path === "/api/history") return reply([]);
    if (staticRoutes[path]) return reply(await read(staticRoutes[path]));
    if (path === "/api/project/file") {
      fileNumbers ??= read("files/index.json");
      const number = (await fileNumbers)[query.get("path")];
      if (number === undefined) return reply({ error: "데모에 없는 파일입니다." }, 404);
      return reply(await read(`files/${number}.json`));
    }
    const feedback = path.match(/^\/api\/feedback\/([\w-]+)$/);
    if (feedback) return reply(await read(`feedback/${feedback[1]}.json`));
    const diagnosis = path.match(/^\/api\/runs\/([\w-]+)\/diagnosis$/);
    if (diagnosis) return reply(await read(`diagnosis/${diagnosis[1]}.json`));
    const record = path.match(/^\/api\/runs\/([\w-]+)$/);
    if (record) {
      const data = await run(record[1]);
      return reply({ ...data, events: data.events.slice(Number(query.get("after")) || 0) });
    }
    return reply({ error: "이 기능은 설치한 앱에서 사용할 수 있습니다." }, 404);
  }
  window.fetch = (input, init) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (!url.pathname.startsWith("/api/")) return original(input, init);
    if (init?.method && init.method !== "GET")
      return Promise.resolve(reply({ error: "웹 데모는 저장 기록 재생만 제공합니다." }, 400));
    return answer(url.pathname, url.searchParams).catch((error) =>
      reply({ error: error.message }, 500),
    );
  };
})();
