// Cue rules of the Jev circuit (jev.js). Run with: node --test
const test = require("node:test");
const assert = require("node:assert/strict");
const J = require("./jev.js");

const T0 = 1_000_000;                 // clock (ms)
const GEN = 1_800_000_000;            // payload generatedAt (epoch s)
const call = (id, phase, over = {}) => ({ id, agent: "a1", at: GEN - 5, end: phase === "pending" ? null : GEN - 1, phase, outcome: null, ...over });
const row = over => ({ applied: true, measure: "confidence", value_pm: 823, threshold_pm: 700, yes_pm: null, no_pm: null, ...over });
const outcome = over => ({ ok: true, fail_open: false, latency_ms: 412, policy: "generic", applied: true, reason: "all_applied",
  error: null, audit: [row()], ...over });
const name = id => ({ a1: "proj", a2: "other" })[id] ?? null;

function primed(jevs = []) {
  return J.observeJev(J.emptyJev(), jevs, GEN, T0);
}

test("the first payload only primes: history is never animated", () => {
  const s = primed([call("x1", "done", { outcome: outcome() }), call("x2", "missing")]);
  assert.equal(s.cues.length, 0);
  assert.equal(s.primed, true);
  assert.equal(J.jevLine(s, T0, name), "");
});

test("a call already pending at priming starts at the scan, without an outward trip", () => {
  const s = primed([call("p1", "pending")]);
  assert.equal(s.cues.length, 1);
  assert.equal(s.cues[0].primed, true);
  assert.deepEqual(J.frameAt(s.cues[0], T0, false), { stage: "scan", t: 0 });
  assert.equal(J.jevLine(s, T0, name), "proj: Jev deciding");
});

test("an empty first payload still primes, so the next call is animated", () => {
  let s = primed([]);
  s = J.observeJev(s, [call("p1", "pending")], GEN + 1, T0 + 100);
  assert.equal(s.cues[0].primed, false);
  assert.equal(J.frameAt(s.cues[0], T0 + 100, false).stage, "out");
});

test("every stage's timing: out 180 ms, scan 900 ms cycle, back 220 ms, marker 1.2 s", () => {
  let s = primed([]);
  s = J.observeJev(s, [call("p1", "pending")], GEN, T0);
  const c0 = s.cues[0];
  assert.deepEqual(J.frameAt(c0, T0, false), { stage: "out", t: 0 });
  assert.deepEqual(J.frameAt(c0, T0 + 90, false), { stage: "out", t: 0.5 });
  assert.deepEqual(J.frameAt(c0, T0 + J.OUT_MS, false), { stage: "scan", t: 0 });
  assert.deepEqual(J.frameAt(c0, T0 + J.OUT_MS + 450, false), { stage: "scan", t: 0.5 });
  assert.deepEqual(J.frameAt(c0, T0 + J.OUT_MS + J.SCAN_MS + 90, false), { stage: "scan", t: 0.1 });   // repeats
  const done = T0 + 2000;
  s = J.observeJev(s, [call("p1", "done", { outcome: outcome() })], GEN + 2, done);
  const c1 = s.cues[0];
  assert.equal(c1.outcome, "applied");
  assert.deepEqual(J.frameAt(c1, done, false), { stage: "back", t: 0 });
  assert.deepEqual(J.frameAt(c1, done + 110, false), { stage: "back", t: 0.5 });
  assert.deepEqual(J.frameAt(c1, done + J.BACK_MS, false), { stage: "mark", outcome: "applied", t: 0 });
  assert.deepEqual(J.frameAt(c1, done + J.BACK_MS + 600, false), { stage: "mark", outcome: "applied", t: 0.5 });
  assert.deepEqual(J.frameAt(c1, done + J.BACK_MS + J.MARK_MS, false), { stage: "gone" });
  assert.equal(J.pruneJev(s, done + J.BACK_MS + J.MARK_MS - 1), s);
  assert.equal(J.pruneJev(s, done + J.BACK_MS + J.MARK_MS).cues.length, 0);
});

test("a result seen during the outward trip waits for it to land before returning", () => {
  let s = J.observeJev(primed([]), [call("p1", "pending")], GEN, T0);
  s = J.observeJev(s, [call("p1", "done")], GEN, T0 + 50);
  assert.deepEqual(J.frameAt(s.cues[0], T0 + 100, false), { stage: "out", t: 100 / 180 });
  assert.deepEqual(J.frameAt(s.cues[0], T0 + J.OUT_MS, false), { stage: "back", t: 0 });
});

test("between polls: a call first seen finished gets one completion cue, marker only", () => {
  let s = primed([]);
  s = J.observeJev(s, [call("f1", "done", { outcome: outcome({ applied: false, reason: "all_fallback", audit: [] }) })], GEN, T0);
  assert.equal(s.cues.length, 1);
  const c = s.cues[0];
  assert.equal(c.kind, "complete");
  assert.deepEqual(J.frameAt(c, T0, false), { stage: "mark", outcome: "fallback", t: 0 });   // no out, scan or back
  assert.equal(J.jevLine(s, T0, name), "proj: Jev fell back (generic, all fallback, Jev 412 ms), finished between updates");
  // seen again: never twice
  s = J.observeJev(s, [call("f1", "done")], GEN + 2, T0 + 1500);
  assert.equal(s.cues.length, 0);
  s = J.observeJev(s, [call("f1", "done")], GEN + 4, T0 + 3000);
  assert.equal(s.cues.length, 0);
});

test("a call that ended more than 15 s before generatedAt is history", () => {
  let s = primed([]);
  s = J.observeJev(s, [call("old", "done", { end: GEN - 16 }), call("edge", "done", { end: GEN - 15 }),
    call("noend", "done", { end: null })], GEN, T0);
  assert.deepEqual(s.cues.map(c => c.key), ["edge"]);
});

test("missing: the marker stays at the agent, nothing travels back", () => {
  let s = J.observeJev(primed([]), [call("p1", "pending")], GEN, T0);
  s = J.observeJev(s, [call("p1", "missing")], GEN + 2, T0 + 1000);
  const c = s.cues[0];
  assert.deepEqual(J.frameAt(c, T0 + 1000, false), { stage: "mark", outcome: "missing", t: 0 });
  assert.equal(J.frameAt(c, T0 + 1000 + J.MARK_MS, false).stage, "gone");
  assert.equal(J.jevLine(s, T0 + 1000, name), "proj: Jev gave no result");
  // first seen missing (between polls) is a missing cue
  const m = J.observeJev(primed([]), [call("m1", "missing")], GEN, T0);
  assert.equal(m.cues[0].kind, "missing");
});

test("a pending cue whose call disappears ends unknown", () => {
  let s = J.observeJev(primed([]), [call("p1", "pending")], GEN, T0);
  s = J.observeJev(s, [], GEN + 2, T0 + 1000);
  assert.equal(s.cues[0].outcome, "unknown");
  assert.deepEqual(J.frameAt(s.cues[0], T0 + 1000, false), { stage: "mark", outcome: "unknown", t: 0 });
  assert.equal(J.jevLine(s, T0 + 1000, name), "proj: Jev call out of view");
});

test("a failed call still returns (it had a result)", () => {
  let s = J.observeJev(primed([]), [call("p1", "pending")], GEN, T0);
  s = J.observeJev(s, [call("p1", "done", { outcome: outcome({ ok: false, error: "timeout", fail_open: true }) })], GEN, T0 + 500);
  assert.equal(J.frameAt(s.cues[0], T0 + 500, false).stage, "back");
  assert.equal(J.jevLine(s, T0 + 500 + J.BACK_MS, name), "proj: Jev failed (generic, timeout, fell open, Jev 412 ms)");
});

test("simultaneous calls and the cue cap", () => {
  let s = primed([]);
  const many = Array.from({ length: 40 }, (_, i) => call("c" + i, "pending", { agent: i % 2 ? "a1" : "a2" }));
  s = J.observeJev(s, many, GEN, T0);
  assert.equal(s.cues.length, J.MAX_CUES);
  assert.equal(s.cues[0].key, "c8");   // the oldest go first
  assert.equal(J.jevLine(s, T0, name), "other: Jev deciding · proj: Jev deciding · other: Jev deciding · +29 more");
});

test("cues of an agent not on the plane are not described", () => {
  let s = J.observeJev(primed([]), [call("p1", "pending", { agent: "gone" })], GEN, T0);
  assert.equal(J.jevLine(s, T0, name), "");
});

test("reduced motion: a static hold while pending, a static marker, no travel", () => {
  let s = J.observeJev(primed([]), [call("p1", "pending")], GEN, T0);
  for (const t of [0, 90, 500, 1500]) assert.deepEqual(J.frameAt(s.cues[0], T0 + t, true), { stage: "pending" });
  s = J.observeJev(s, [call("p1", "done", { outcome: outcome() })], GEN, T0 + 2000);
  const c = s.cues[0];
  for (const t of [0, 100, J.BACK_MS, J.BACK_MS + 600, J.BACK_MS + J.MARK_MS - 1]) {
    assert.deepEqual(J.frameAt(c, T0 + 2000 + t, true), { stage: "mark", outcome: "applied", t: 0 });
  }
  assert.deepEqual(J.frameAt(c, T0 + 2000 + J.BACK_MS + J.MARK_MS, true), { stage: "gone" });
  assert.match(J.jevLine(s, T0 + 2000, name), /^proj: Jev applied \(/);
});

test("outcome classes per policy", () => {
  const o = J.outcomeOf;
  assert.equal(o(null), "done");
  assert.equal(o(outcome({ ok: false })), "failed");
  assert.equal(o(outcome({ error: "timeout", applied: true })), "failed");     // failure wins over its fallback
  assert.equal(o(outcome({ policy: "contextKeep", applied: true })), "applied");
  assert.equal(o(outcome({ policy: "contextKeep", applied: false })), "answered");
  assert.equal(o(outcome({ policy: "contextKeep", applied: null })), "answered");
  assert.equal(o(outcome({ policy: "safetyGate", applied: true })), "applied");
  assert.equal(o(outcome({ policy: "safetyGate", applied: false, reason: "missing_answer" })), "fallback");
  assert.equal(o(outcome({ policy: "safetyGate", applied: false, reason: "destructive" })), "answered");
  assert.equal(o(outcome({ policy: "generic", applied: true })), "applied");
  assert.equal(o(outcome({ policy: "routeModel", applied: false, reason: "partial_fallback" })), "partial");
  assert.equal(o(outcome({ policy: "shouldContinue", applied: false, reason: "low_confidence" })), "fallback");
  assert.equal(o(outcome({ policy: "none", applied: null, reason: null })), "answered");
});

test("wording: three decimals, no contradicted comparison, failed calls list no rows", () => {
  const d = J.describeOutcome;
  assert.equal(d(null), "");
  assert.equal(d(outcome()), "generic, all applied, confidence 0.823 ≥ 0.700 applied, Jev 412 ms");
  assert.equal(d(outcome({ audit: [row({ applied: false, value_pm: 823, threshold_pm: 700 })] })),
    "generic, all applied, confidence 0.823 (threshold 0.700) fell back, Jev 412 ms");
  assert.equal(d(outcome({ audit: [row({ applied: false, value_pm: 412, threshold_pm: 700 })] })),
    "generic, all applied, confidence 0.412 < 0.700 fell back, Jev 412 ms");
  assert.equal(d(outcome({ policy: "contextKeep", audit: [row({ applied: null, value_pm: 500, threshold_pm: 500 })] })),
    "contextKeep, all applied, confidence 0.500 ≥ 0.500, Jev 412 ms");
  assert.equal(d(outcome({ audit: [row({ measure: "yes_no", value_pm: null, threshold_pm: null, yes_pm: 800, no_pm: 200, applied: false })] })),
    "generic, all applied, yes/no at 0.800 / 0.200 fell back, Jev 412 ms");
  assert.equal(d(outcome({ audit: [row(), row(), row(), row(), row()] })).split(", +")[1], "2 more, Jev 412 ms");
  assert.equal(d(outcome({ ok: false, error: "http_error", audit: [row(), row()], latency_ms: null })), "generic, http_error");
  assert.equal(d(outcome({ audit: [row({ value_pm: null })], latency_ms: null })), "generic, all applied, confidence unknown applied");
  assert.equal(J.jevLine(J.observeJev(primed([]), [call("f", "done", { outcome: outcome() })], GEN, T0), T0, name),
    "proj: Jev applied (generic, all applied, confidence 0.823 ≥ 0.700 applied, Jev 412 ms), finished between updates");
});

test("a done call with no outcome is a neutral done", () => {
  let s = J.observeJev(primed([]), [call("p1", "pending")], GEN, T0);
  s = J.observeJev(s, [call("p1", "done")], GEN, T0 + 400);
  assert.equal(s.cues[0].outcome, "done");
  assert.equal(J.jevLine(s, T0 + 400 + J.BACK_MS, name), "proj: Jev done");
});
