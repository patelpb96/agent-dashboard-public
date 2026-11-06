// Runtime metadata from /api/graph in filters.js: labels, aliases, order, colours and directions. Run with: node --test
const test = require("node:test");
const assert = require("node:assert/strict");
const F = require("./filters.js");

// What graph.py's runtime_meta() sends for the built-ins, plus one runtime the page has never heard of.
const SERVED = [
  { id: "claude", label: "Claude", color: "#e9a960", aliases: ["claude-code", "claude_code", "claude code", "anthropic"], order: 0 },
  { id: "codex", label: "GPT", color: "#9a88ff", aliases: ["codex-cli", "codex_cli", "openai", "gpt"], order: 1 },
  { id: "pi", label: "Pi", color: "#62d1c8", aliases: [], order: 2 },
  { id: "antigravity", label: "Google", color: "#ef7fae", aliases: ["antigravity-cli", "agy", "google"], order: 3 },
  { id: "deepseek", label: "DeepSeek", color: "#4d6bfe", aliases: ["dsh"], order: 4 },
  { id: "opencode", label: "OpenCode", color: "#7bd88f", aliases: ["open-code"], order: 5 },
];
const agent = (id, domain, model = "m") => ({ id, kind: "agent", domain, model });
const NODES = [agent("o1", "opencode"), agent("o2", "open-code"), agent("x1", "zed-agent"), agent("c1", "claude"),
  agent("d1", "dsh"), agent("g1", "openai")];

test.afterEach(() => F.useRuntimes(null));

test("without metadata the built-in tables still apply (an older server)", () => {
  assert.deepEqual(F.useRuntimes(undefined), []);
  assert.deepEqual(["codex", "openai", "antigravity", "dsh", "pi"].map(d => F.runtimeName(d)), ["GPT", "GPT", "Google", "DeepSeek", "Pi"]);
  assert.equal(F.servedRuntime("claude"), null);
  assert.equal(F.extraRuntime("opencode"), false);
  assert.equal(F.runtimeName("opencode"), "Opencode");
  assert.deepEqual(F.runtimesPresent(NODES).map(r => r.domain), ["claude", "codex", "deepseek", "open-code", "opencode", "zed-agent"]);
});

test("the built-ins read the same with metadata as without", () => {
  const before = ["claude", "claude-code", "codex", "gpt", "pi", "antigravity", "google", "dsh", "deepseek"]
    .map(d => [F.canonicalDomain(d), F.runtimeName(d)]);
  F.useRuntimes(SERVED);
  const after = ["claude", "claude-code", "codex", "gpt", "pi", "antigravity", "google", "dsh", "deepseek"]
    .map(d => [F.canonicalDomain(d), F.runtimeName(d)]);
  assert.deepEqual(after, before);
});

test("an unknown runtime id from the server gets its label, aliases and place in the order", () => {
  F.useRuntimes(SERVED);
  assert.equal(F.canonicalDomain("open-code"), "opencode");
  assert.equal(F.runtimeName("open-code"), "OpenCode");
  assert.equal(F.extraRuntime("opencode"), true);
  assert.equal(F.extraRuntime("claude"), false);
  assert.equal(F.builtinRuntime("dsh"), true);
  const present = F.runtimesPresent(NODES);
  assert.deepEqual(present.map(r => [r.domain, r.name, r.count]),
    [["claude", "Claude", 1], ["codex", "GPT", 1], ["deepseek", "DeepSeek", 1], ["opencode", "OpenCode", 2],
     ["zed-agent", "Zed Agent", 1]]);   // an id nobody listed still shows, last, in title case
  const view = F.normalizeView({ hiddenRuntimes: ["open-code"] });
  assert.deepEqual(view.hiddenRuntimes, ["opencode"]);
  const out = F.filterGraph(NODES, [], view, {});
  assert.deepEqual([...out.shown].sort(), ["c1", "d1", "g1", "x1"]);
});

test("metadata is checked: bad ids, colours and text are dropped or cleaned", () => {
  const kept = F.useRuntimes([
    { id: "Bad Id", label: "x" }, { id: "<script>", label: "x" }, null, "claude",
    { id: "ok-rt", label: "  Ok\u0000<b>RT</b>\n" + "x".repeat(80), color: "red;background:url(x)", aliases: ["OK", 7, "claude"], order: "first" },
    { id: "ok-rt", label: "duplicate" },
    { id: "claude", label: "Claude", color: "#E9A960", order: 0 },
  ]);
  assert.deepEqual(kept.map(r => r.id), ["claude", "ok-rt"]);
  const ok = F.servedRuntime("ok");
  assert.equal(ok.id, "ok-rt");
  assert.equal(ok.color, null);
  assert.equal(ok.label.length, 40);
  assert.ok(ok.label.startsWith("Ok <b>RT</b> x"));   // plain text; the page sets it with textContent
  assert.equal(F.canonicalDomain("claude"), "claude");   // an alias never takes over another runtime's id
  assert.equal(F.servedRuntime("claude").color, "#e9a960");
  assert.deepEqual(F.useRuntimes([]), []);
  assert.equal(F.servedRuntime("claude"), null);
});

test("hashed colours are steady #rrggbb values that differ by id", () => {
  const a = F.hashColor("opencode");
  assert.match(a, /^#[0-9a-f]{6}$/);
  assert.equal(F.hashColor("opencode"), a);
  assert.notEqual(F.hashColor("zed-agent"), a);
});

test("free directions stay clear of the fixed ones and of each other", () => {
  const unit = v => { const d = Math.hypot(...v); return v.map(x => x / d); };
  const fixed = [[-0.94, -0.26, -0.22], [-0.02, 0.08, 1], [0.92, 0.28, -0.18], [0.1, 0.92, -0.42], [0.55, -0.6, -0.6]].map(unit);
  const taken = [...fixed];
  const handed = [];
  for (let i = 0; i < 4; i++) { const v = F.freeDirection(taken); handed.push(v); taken.push(v); }
  assert.deepEqual(F.freeDirection(fixed), handed[0]);   // deterministic
  const angle = (a, b) => Math.acos(Math.max(-1, Math.min(1, a[0] * b[0] + a[1] * b[1] + a[2] * b[2])));
  for (const [i, v] of handed.entries()) {
    assert.ok(Math.abs(Math.hypot(...v) - 1) < 1e-9);
    for (const f of fixed) assert.ok(angle(v, f) > 0.6, `direction ${i} is ${angle(v, f)} rad from a fixed one`);
    for (const w of handed.slice(0, i)) assert.ok(angle(v, w) > 0.6);
  }
});
