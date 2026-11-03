// View controls of the plane (filters.js). Run with: node --test
const test = require("node:test");
const assert = require("node:assert/strict");
const F = require("./filters.js");

const NOW = 1_800_000_000;   // epoch s
const agent = (id, domain, model, over = {}) => ({ id, kind: "agent", domain, model, ...over });
const res = (id, kind = "tool") => ({ id, kind });
const edge = (src, dst, over = {}) => ({ src, dst, kind: "call", active: false, pulse: false, lastAt: NOW - 3600, ...over });

const NODES = [
  agent("c1", "claude", "opus"), agent("c2", "claude-code", "sonnet"),
  agent("g1", "codex", "gpt-5"), agent("g2", "openai", "gpt-5"),
  agent("p1", "pi", "qwen"), agent("a1", "antigravity", "gemini"),
  { id: "s1", kind: "subagent", domain: "claude", model: "haiku", parent: "c1" },
  res("t:shared"), res("t:claude-only"), res("t:gpt-only"), res("d:recent", "doc"), res("d:loose", "doc"),
  res("mcp:jev:jev_decide"),
];
const EDGES = [
  edge("c1", "t:shared"), edge("g1", "t:shared"),
  edge("c2", "t:claude-only"),
  edge("g2", "t:gpt-only", { active: true }),
  edge("p1", "d:recent", { lastAt: NOW - 30 }),
  edge("c1", "s1", { kind: "spawn" }),
  edge("c1", "mcp:jev:jev_decide"),
];
const run = (view, opts = {}) => F.filterGraph(NODES, EDGES, view, { now: NOW, activeWindow: 120, ...opts });
const ids = set => [...set].sort();

test("runtime names: codex, openai and gpt are GPT; antigravity is Google", () => {
  assert.deepEqual(["codex", "openai", "gpt", "codex-cli"].map(F.runtimeName), ["GPT", "GPT", "GPT", "GPT"]);
  assert.deepEqual(["antigravity", "antigravity-cli", "claude-code", "pi"].map(F.runtimeName), ["Google", "Google", "Claude", "Pi"]);
  assert.equal(F.runtimeName("my_runtime"), "My Runtime");
});

test("runtimes present: one entry per runtime, known ones first, models grouped with counts", () => {
  const r = F.runtimesPresent(NODES);
  assert.deepEqual(r.map(x => [x.name, x.count]), [["Claude", 3], ["GPT", 2], ["Pi", 1], ["Google", 1]]);
  assert.deepEqual(r[1].models.map(m => [m.key, m.count]), [["codex|gpt-5", 2]]);
});

test("the default view shows everything", () => {
  const out = run(F.defaultView());
  assert.equal(out.count, NODES.length);
  assert.equal(out.total, NODES.length);
});

test("hiding a runtime hides its agents, their edges, and resources only they used", () => {
  const out = run({ hiddenRuntimes: ["codex"] });
  for (const id of ["g1", "g2", "t:gpt-only"]) assert.ok(!out.shown.has(id), id);
  assert.ok(out.shown.has("t:shared"), "a resource a shown agent also used stays");
  assert.ok(out.shown.has("d:loose"), "a resource no agent used is not an orphan of a hidden one");
  const kept = F.visibleEdges(EDGES, out.shown);
  assert.ok(kept.every(e => !["g1", "g2"].includes(e.src)));
  assert.equal(kept.length, EDGES.length - 2);
});

test("an alias in the saved view hides the same runtime", () => {
  assert.ok(!run({ hiddenRuntimes: ["openai"] }).shown.has("g1"));
});

test("hiding one model hides only that runtime's agents on it", () => {
  const out = run({ hiddenModels: ["claude|opus"] });
  assert.ok(!out.shown.has("c1"));
  assert.ok(out.shown.has("c2") && out.shown.has("s1"));
  assert.ok(!out.shown.has("mcp:jev:jev_decide"), "only c1 used the Jev node");
});

test("active: only resources on an active, pulsing or recent edge of a shown agent", () => {
  const out = run({ resources: "active" });
  assert.deepEqual(ids(out.shown).filter(id => !NODES.find(n => n.id === id && /agent/.test(n.kind))),
    ["d:recent", "t:gpt-only"]);
  assert.deepEqual(ids(out.recent), ["d:recent", "t:gpt-only"]);
  // a pulse counts; the recent window is the server's
  const pulsed = F.filterGraph(NODES, [edge("c1", "t:shared", { pulse: true })], { resources: "active" }, { now: NOW });
  assert.ok(pulsed.shown.has("t:shared"));
  const narrow = run({ resources: "active" }, { activeWindow: 10 });
  assert.ok(!narrow.shown.has("d:recent"));
});

test("active and a hidden runtime combine: a hidden agent's activity does not keep a resource", () => {
  const out = run({ resources: "active", hiddenRuntimes: ["codex"] });
  assert.ok(!out.shown.has("t:gpt-only"));
  assert.ok(out.shown.has("d:recent"));
});

test("none shows agents only, but keeps the Jev node while a cue is showing", () => {
  const out = run({ resources: "none" });
  assert.deepEqual(ids(out.shown), ["a1", "c1", "c2", "g1", "g2", "p1", "s1"]);
  const cue = run({ resources: "none" }, { keep: ["mcp:jev:jev_decide", "not-on-plane"] });
  assert.ok(cue.shown.has("mcp:jev:jev_decide"));
  assert.ok(!cue.shown.has("not-on-plane"));
  assert.equal(F.visibleEdges(EDGES, cue.shown).filter(e => e.dst === "mcp:jev:jev_decide").length, 1);
});

test("cube projection lands on the surface and keeps the direction", () => {
  const dirs = [[1, 0, 0], [0.3, -0.9, 0.2], [-2, 2, 1], [0.01, 0.02, -0.03], [5, 5, 5]];
  for (const d of dirs) {
    const p = F.cubePoint(d, 50);
    assert.ok(Math.abs(Math.max(...p.map(Math.abs)) - 50) < 1e-9, `on the surface: ${p}`);
    const nd = Math.hypot(...d), np = Math.hypot(...p);
    for (let i = 0; i < 3; i++) assert.ok(Math.abs(d[i] / nd - p[i] / np) < 1e-9, "same direction");
    const n = F.cubeNormal(p);
    assert.equal(n.reduce((s, v, i) => s + v * p[i], 0), 50, "the face normal points out through the point");
  }
  assert.deepEqual(F.cubePoint([0, 0, 0], 7), [0, 0, 7]);
});

test("a cube has 12 edges of the full side length, all on its surface", () => {
  const edges = F.cubeEdges(2);
  assert.equal(edges.length, 12);
  for (const [a, b] of edges) {
    assert.equal(Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]), 4);
    assert.ok([...a, ...b].every(v => Math.abs(v) === 2));
  }
  assert.equal(new Set(edges.map(e => JSON.stringify(e))).size, 12);
});

test("saved views round-trip, and junk falls back to the defaults", () => {
  const view = { hiddenRuntimes: ["codex"], hiddenModels: ["claude|opus"], resources: "active", layout: "cube" };
  assert.deepEqual(F.parseView(F.serializeView(view)), view);
  for (const junk of [null, undefined, "", "{", "null", "[]", "42", '"cube"', "{\"layout\":5}"]) {
    assert.deepEqual(F.parseView(junk), F.defaultView(), String(junk));
  }
  const mixed = F.parseView(JSON.stringify({ hiddenRuntimes: ["openai", 3, "", "codex", "x".repeat(500)],
    hiddenModels: "claude|opus", resources: "some", layout: "cube", extra: { a: 1 } }));
  assert.deepEqual(mixed, { hiddenRuntimes: ["codex"], hiddenModels: [], resources: "all", layout: "cube" });
  assert.ok(F.isDefaultView(F.parseView("junk")));
  assert.ok(!F.isDefaultView(view));
});

test("labels under a cap: agents first by tier, then active resources, then the biggest", () => {
  const items = [
    { key: "big", weight: 9 }, { key: "hot", active: true, weight: 1 },
    { key: "lead", agent: true, tier: 0 }, { key: "worker", agent: true, tier: 2 }, { key: "anon", agent: true },
    { key: "small", weight: 2 },
  ];
  assert.deepEqual([...F.pickLabels(items, 4)], ["lead", "worker", "anon", "hot"]);
  assert.deepEqual([...F.pickLabels(items, 6)].slice(4), ["big", "small"]);
  assert.equal(F.pickLabels(items, 0).size, 0);
});
