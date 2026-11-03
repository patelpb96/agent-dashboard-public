// Jev on the plane: turns the Jev calls in successive /api/graph payloads into short visual cues (a packet goes out
// from the agent to the Jev node, scans there while the decision is pending, comes back, and leaves an outcome
// marker at the agent). Pure: the caller passes the clock, so the same payloads and times always give the same cues.
// plane.js draws the cues; this file holds the rules so `node --test` can check them.
//
// Honesty rules (the same as the Agent Workbench's Jev circuit):
//   * The first payload only primes: finished calls are history, never animated; a call already pending is shown
//     scanning, without an outward trip it was not seen making.
//   * A call first seen already finished gets one completion cue, never an invented pending phase, and only when it
//     finished at most FRESH_S before the payload was made.
//   * The outcome is the server's bounded reading of Jev's answer (allow-listed codes and per-mille numbers; never
//     the request, answers, labels or messages). Without one, a finished call is a neutral "done".
(function (root) {
  const OUT_MS = 180;
  const SCAN_MS = 900;
  const BACK_MS = 220;
  const MARK_MS = 1200;
  const FRESH_S = 15;      // a call that ended longer ago than this before generatedAt is history when first seen
  const MAX_CUES = 32;     // cues kept at once; the oldest go first

  // Marker class of a Jev answer. `applied` means different things per policy, so it is read per policy:
  // applied (its policy picked a value; not proof the agent acted on it), partial (some decisions applied, some fell
  // back), fallback (none applied), failed (Jev did not answer; with fail-open the agent fell back), answered (neither:
  // questions only, nothing to change, or a safety gate that asks the human). done: a result that could not be read.
  function outcomeOf(o) {
    if (!o) return "done";
    if (o.ok === false || o.error != null) return "failed";
    // contextKeep: `applied` = something was dropped or truncated; keeping everything is an answer, not a fallback.
    if (o.policy === "contextKeep") return o.applied === true ? "applied" : "answered";
    // safetyGate: `applied` = allow or deny decided by the gate; asking hands the call to the human (a fallback only
    // when an answer was missing).
    if (o.policy === "safetyGate") return o.applied === true ? "applied" : o.reason === "missing_answer" ? "fallback" : "answered";
    if (o.applied === true) return "applied";
    if (o.applied === false) return o.reason === "partial_fallback" ? "partial" : "fallback";
    return "answered";
  }

  const pm = v => (v / 1000).toFixed(3);
  // A short, plain description of a Jev answer: policy, reason, up to three audited decisions against their
  // threshold(s), and Jev's own latency.
  function describeOutcome(o) {
    if (!o) return "";
    const parts = [];
    if (o.policy) parts.push(o.policy);
    if (o.error) parts.push(`${o.error}${o.fail_open ? ", fell open" : ""}`);
    else if (o.reason) parts.push(o.reason.replace(/_/g, " "));
    const audit = Array.isArray(o.audit) ? o.audit : [];
    // A failed call's rows are its fallback defaults, not decisions: not listed.
    const rows = (o.error ? [] : audit.slice(0, 3)).map(r => {
      const mark = r.applied === true ? " applied" : r.applied === false ? " fell back" : "";
      if (r.measure === "yes_no") {
        return r.yes_pm != null && r.no_pm != null ? `yes/no at ${pm(r.yes_pm)} / ${pm(r.no_pm)}${mark}` : `yes/no${mark}`;
      }
      if (r.value_pm == null) return `confidence unknown${mark}`;
      if (r.threshold_pm == null) return `confidence ${pm(r.value_pm)}${mark}`;
      const over = r.value_pm >= r.threshold_pm;
      // Never print a comparison the row's own outcome contradicts (an unknown option can clear the bar and fall back).
      if (r.applied != null && over !== r.applied) return `confidence ${pm(r.value_pm)} (threshold ${pm(r.threshold_pm)})${mark}`;
      return `confidence ${pm(r.value_pm)} ${over ? "≥" : "<"} ${pm(r.threshold_pm)}${mark}`;
    });
    parts.push(...rows);
    const more = o.error ? 0 : audit.length - rows.length;
    if (more > 0) parts.push(`+${more} more`);
    if (o.latency_ms != null) parts.push(`Jev ${o.latency_ms} ms`);
    return parts.join(", ");
  }

  // A result travels back to the agent; no result (missing, out of view) just leaves its marker.
  const returned = o => o !== null && o !== "missing" && o !== "unknown";

  function emptyJev() {
    return { primed: false, phases: new Map(), cues: [] };
  }

  // Fold one payload's `jevs` (with its generatedAt, epoch seconds) into the state at clock time `now` (ms).
  // A cue: {key (the call id), source (the agent id), kind: trip | complete | missing, start, primed (a trip already
  // pending when first seen: no outward travel), ended (clock time the result or its absence was seen; null while
  // pending), outcome, detail (the answer in words)}.
  function observeJev(state, jevs, generatedAt, now) {
    const cues = new Map(state.cues.map(c => [c.key, c]));
    const phases = new Map();
    const first = !state.primed;
    const finish = (key, outcome, detail = "") => {
      const c = cues.get(key);
      if (!c || c.kind !== "trip" || c.ended !== null) return false;
      cues.set(key, { ...c, ended: now, outcome, detail });
      return true;
    };
    const add = cue => { if (!cues.has(cue.key)) cues.set(cue.key, cue); };
    for (const c of Array.isArray(jevs) ? jevs : []) {
      if (!c || typeof c.id !== "string" || phases.has(c.id)) continue;
      const key = c.id, old = first ? undefined : state.phases.get(key);
      phases.set(key, c.phase);
      if (c.phase === "pending") {
        // Only a call seen to appear pending gets the outward trip; one already pending starts scanning.
        add({ key, source: c.agent, kind: "trip", start: now, primed: first || old !== undefined, ended: null, outcome: null, detail: "" });
        continue;
      }
      if (c.phase !== "done" && c.phase !== "missing") continue;
      const outcome = c.phase === "done" ? outcomeOf(c.outcome) : "missing";
      const detail = c.phase === "done" ? describeOutcome(c.outcome) : "";
      if (finish(key, outcome, detail)) continue;
      if (first || old === c.phase) continue;   // history, or already shown
      const fresh = typeof c.end === "number" && typeof generatedAt === "number" && generatedAt - c.end <= FRESH_S;
      // Seen pending before but its cue is gone (cap), or never seen and recent: one completion cue.
      if (old === "pending" || (old === undefined && fresh)) {
        add({ key, source: c.agent, kind: c.phase === "done" ? "complete" : "missing", start: now, primed: false,
              ended: now, outcome, detail });
      }
    }
    // A pending trip whose call is no longer in the payload (pushed out, or its transcript left) ends unknown.
    for (const c of [...cues.values()]) {
      if (c.kind === "trip" && c.ended === null && !phases.has(c.key)) finish(c.key, "unknown");
    }
    let list = [...cues.values()].filter(c => !expired(c, now));
    if (list.length > MAX_CUES) list = list.slice(list.length - MAX_CUES);
    return { primed: true, phases, cues: list };
  }

  // Drop cues whose marker has run out. Returns the same state when nothing expired.
  function pruneJev(state, now) {
    const cues = state.cues.filter(c => !expired(c, now));
    return cues.length === state.cues.length ? state : { ...state, cues };
  }

  function backStart(c) {
    if (c.ended === null) return null;
    const outEnd = c.start + (c.primed || c.kind !== "trip" ? 0 : OUT_MS);
    return Math.max(c.ended, outEnd);   // a result seen during the outward trip waits for it to land
  }
  function expired(c, now) {
    const b = backStart(c);
    if (b === null) return false;
    const back = c.kind === "trip" && returned(c.outcome) ? BACK_MS : 0;
    return now >= b + back + MARK_MS;
  }

  const clamp = x => (x < 0 ? 0 : x > 1 ? 1 : x);
  // What to draw for a cue at `now`: {stage: out | scan | back, t} (t runs 0..1 within the stage; the scan repeats),
  // {stage: pending} (reduced motion: a static hold on the Jev node), {stage: mark, outcome, t}, or {stage: gone}.
  function frameAt(c, now, reduced) {
    const b = backStart(c);
    const outcome = c.outcome || "unknown";
    if (b === null || now < b) {
      if (reduced) return { stage: "pending" };
      const outEnd = c.start + (c.primed ? 0 : OUT_MS);
      if (now < outEnd) return { stage: "out", t: clamp((now - c.start) / OUT_MS) };
      return { stage: "scan", t: ((now - outEnd) % SCAN_MS) / SCAN_MS };
    }
    // Only a call that came back with a result travels back; a missing or unknown one just leaves its marker.
    const back = c.kind === "trip" && returned(outcome) ? BACK_MS : 0;
    if (now < b + back) return reduced ? { stage: "mark", outcome, t: 0 } : { stage: "back", t: clamp((now - b) / BACK_MS) };
    if (now < b + back + MARK_MS) return { stage: "mark", outcome, t: reduced ? 0 : clamp((now - b - back) / MARK_MS) };
    return { stage: "gone" };
  }

  const OUTCOME_WORDS = {
    applied: "applied", partial: "partly applied", fallback: "fell back", failed: "failed", answered: "answered",
    done: "done", missing: "gave no result", unknown: "call out of view",
  };

  // One line of text for the cues showing now (also what reduced motion relies on). `name(agent)` gives an agent's
  // display name, or null for an agent that is not on the plane (its cues are not described).
  function jevLine(state, now, name) {
    const parts = [];
    for (const c of state.cues) {
      const f = frameAt(c, now, true);
      if (f.stage === "gone") continue;
      const who = name(c.source);
      if (who == null) continue;
      if (f.stage === "pending") parts.push(`${who}: Jev deciding`);
      else if (f.stage === "mark") {
        const what = `Jev ${OUTCOME_WORDS[f.outcome]}${c.detail ? ` (${c.detail})` : ""}`;
        parts.push(c.kind === "complete" ? `${who}: ${what}, finished between updates` : `${who}: ${what}`);
      }
    }
    return parts.length === 0 ? "" : parts.slice(0, 3).join(" · ") + (parts.length > 3 ? ` · +${parts.length - 3} more` : "");
  }

  const api = { OUT_MS, SCAN_MS, BACK_MS, MARK_MS, FRESH_S, MAX_CUES,
    outcomeOf, describeOutcome, emptyJev, observeJev, pruneJev, frameAt, jevLine };
  if (root) root.JevCircuit = api;
  if (typeof module === "object" && module && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : null);
