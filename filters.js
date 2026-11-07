// View controls for the plane: which agents and resources are shown, and the nested-cube layout. Pure: plane.js
// passes the nodes, edges, clock and choices in, so `node --test` can check the rules (filters.test.js).
//
//   * Agents by type: a runtime chip hides every agent of that runtime; the Model menu hides one model of one runtime.
//     Hidden agents take their edges with them, and a resource that only hidden agents used goes too.
//   * Tools & files: "all" keeps every resource a shown agent used; "active" only those on an edge that is in flight,
//     pulsing, or was last used within the server's activity window (graph.py ACTIVE_S, sent as activeWindow);
//     "none" shows agents only. Ids in `keep` (the Jev node while a Jev cue is showing) are always shown.
//   * Layout: "sphere" (three concentric spheres) or "cube" (the same strata on three nested cubes).
//   * Runtimes: names, aliases and order come from the server's "runtimes" list (useRuntimes), with the built-in
//     tables below as the fallback; a runtime the page has no entry for gets a hashed colour and a free direction.
(function (root) {
  const RESOURCE_MODES = ["all", "active", "none"];
  const LAYOUTS = ["sphere", "cube"];
  const ACTIVE_WINDOW_S = 120;   // graph.py ACTIVE_S; used only when a payload does not say
  const MAX_LIST = 200, MAX_TEXT = 200;   // bounds on what a saved view may hold

  // The built-in runtimes: the fallback when the server sends no "runtimes" (an older server). graph.py's
  // registered Runtimes send the same values in /api/graph, and those win when present.
  const DOMAIN_ALIASES = { "claude-code": "claude", claude_code: "claude", "claude code": "claude", anthropic: "claude",
    "codex-cli": "codex", codex_cli: "codex", openai: "codex", gpt: "codex",   // one "GPT" runtime
    "antigravity-cli": "antigravity", agy: "antigravity", google: "antigravity", dsh: "deepseek" };
  const RUNTIME_NAMES = { claude: "Claude", codex: "GPT", pi: "Pi", antigravity: "Google", deepseek: "DeepSeek" };
  const RUNTIME_ORDER = ["claude", "codex", "pi", "antigravity", "deepseek"];
  const RUNTIME_ID = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;   // graph.py RUNTIME_ID
  const HEX_COLOR = /^#[0-9a-f]{6}$/i;
  const MAX_LABEL = 40;

  // Runtimes the server described ({id, label, color, aliases, order}), checked; null: use the built-in tables.
  let served = null;

  const isAgent = n => !!n && (n.kind === "agent" || n.kind === "subagent");
  // A table lookup that only sees the table's own entries: an id like "constructor" or "__proto__" must not find
  // Object.prototype's members.
  const own = (table, key) => (Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined);

  // Plain text only: no control characters, trimmed, capped. (The page still puts it in the DOM as text, never HTML.)
  function cleanText(value, max) {
    if (typeof value !== "string") return "";
    return value.replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g, " ").replace(/\s+/g, " ").trim().slice(0, max);
  }

  // Take the server's runtime list (payload.runtimes). Anything that is not an array, or is empty, goes back to the
  // built-in tables. Bad entries are skipped; an id must look like graph.py's ids, a colour must be #rrggbb.
  // Returns the list kept, in order.
  function useRuntimes(list) {
    if (!Array.isArray(list) || !list.length) { served = null; return []; }
    const byId = new Map();
    for (const r of list.slice(0, MAX_LIST)) {
      if (!r || typeof r !== "object") continue;
      const id = typeof r.id === "string" ? r.id.toLowerCase() : "";
      if (!RUNTIME_ID.test(id) || id.length > MAX_LABEL || byId.has(id)) continue;
      byId.set(id, {
        id,
        label: cleanText(r.label, MAX_LABEL) || id,
        color: typeof r.color === "string" && HEX_COLOR.test(r.color) ? r.color.toLowerCase() : null,
        order: Number.isFinite(r.order) ? r.order : Infinity,
        aliases: cleanList(r.aliases).map(a => a.toLowerCase().slice(0, MAX_LABEL)),
      });
    }
    if (!byId.size) { served = null; return []; }
    const ordered = [...byId.values()].sort((a, b) => a.order - b.order || a.id.localeCompare(b.id));
    const aliases = new Map();
    for (const r of ordered) for (const a of r.aliases) if (!byId.has(a) && !aliases.has(a)) aliases.set(a, r.id);
    served = { byId, aliases, order: ordered.map(r => r.id) };
    return ordered.map(r => ({ ...r, aliases: [...r.aliases] }));
  }

  // The runtime a node belongs to, as plane.js keys its domains and colours.
  function canonicalDomain(raw) {
    const key = String(raw || "other").toLowerCase();
    if (served) {
      if (served.byId.has(key)) return key;
      if (served.aliases.has(key)) return served.aliases.get(key);
    }
    return own(DOMAIN_ALIASES, key) || key;
  }
  const domainOfNode = n => canonicalDomain(n.domain || n.provider || "other");

  // The user's names for the runtimes: the server's label, else Claude / GPT / Pi / Google / DeepSeek; anything else
  // in title case.
  function runtimeName(domain) {
    const d = canonicalDomain(domain);
    const info = served && served.byId.get(d);
    if (info) return info.label;
    return own(RUNTIME_NAMES, d) || d.replace(/[-_]+/g, " ").replace(/\b\w/g, c => c.toUpperCase());
  }

  // The server's entry for a runtime ({id, label, color, order, aliases}), or null (no metadata, or not listed).
  function servedRuntime(domain) {
    const info = served && served.byId.get(canonicalDomain(domain));
    return info ? { ...info, aliases: [...info.aliases] } : null;
  }

  // One of the five runtimes this page has a logo and a fixed spot for.
  function builtinRuntime(domain) { return RUNTIME_ORDER.includes(canonicalDomain(domain)); }

  // A runtime the server lists that this page has no entry of its own for: it gets the plain glyph.
  function extraRuntime(domain) { return !!servedRuntime(domain) && !builtinRuntime(domain); }

  // A steady colour for a runtime with none given: a hue hashed from its id, as #rrggbb.
  function hashColor(id) {
    let h = 2166136261;
    for (const ch of String(id)) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
    const hue = (h >>> 0) % 360, s = 0.55, l = 0.62;
    const k = n => (n + hue / 30) % 12, a = s * Math.min(l, 1 - l);
    const f = n => Math.round(255 * (l - a * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1))));
    return "#" + [f(0), f(8), f(4)].map(v => v.toString(16).padStart(2, "0")).join("");
  }

  // A unit direction for a runtime with no fixed spot: of a fixed spread of candidates, the one farthest (by angle)
  // from every direction in `taken` (the five fixed ones and any already handed out). Deterministic.
  function freeDirection(taken) {
    const dirs = (taken || []).map(v => {
      const d = Math.hypot(v[0], v[1], v[2]) || 1;
      return [v[0] / d, v[1] / d, v[2] / d];
    });
    const N = 96, golden = Math.PI * (3 - Math.sqrt(5));
    let best = null, bestDot = Infinity;
    for (let i = 0; i < N; i++) {
      const y = 1 - 2 * (i + 0.5) / N, r = Math.sqrt(1 - y * y), t = i * golden;
      const c = [Math.cos(t) * r, y, Math.sin(t) * r];
      const nearest = dirs.reduce((m, d) => Math.max(m, c[0] * d[0] + c[1] * d[1] + c[2] * d[2]), -Infinity);
      if (nearest < bestDot - 1e-9) { bestDot = nearest; best = c; }
    }
    return best;
  }

  function modelKey(n) { return `${domainOfNode(n)}|${String(n.model || "")}`; }

  // Runtimes and their models among the agents given, for the chips and the Model menu (known runtimes first).
  function runtimesPresent(nodes) {
    const byDomain = new Map();
    for (const n of nodes) {
      if (!isAgent(n)) continue;
      const domain = domainOfNode(n);
      const entry = byDomain.get(domain) || { domain, name: runtimeName(domain), count: 0, models: new Map() };
      entry.count += 1;
      const key = modelKey(n), model = entry.models.get(key) || { key, model: String(n.model || ""), count: 0 };
      model.count += 1;
      entry.models.set(key, model);
      byDomain.set(domain, entry);
    }
    const order = served ? [...served.order, ...RUNTIME_ORDER.filter(d => !served.byId.has(d))] : RUNTIME_ORDER;
    const rank = d => { const i = order.indexOf(d); return i < 0 ? order.length : i; };
    return [...byDomain.values()]
      .sort((a, b) => rank(a.domain) - rank(b.domain) || a.domain.localeCompare(b.domain))
      .map(r => ({ ...r, models: [...r.models.values()].sort((a, b) => b.count - a.count || a.model.localeCompare(b.model)) }));
  }

  function defaultView() { return { hiddenRuntimes: [], hiddenModels: [], resources: "all", layout: "sphere" }; }

  function cleanList(value) {
    if (!Array.isArray(value)) return [];
    const out = [];
    for (const v of value) {
      if (typeof v !== "string" || !v || v.length > MAX_TEXT || out.includes(v)) continue;
      out.push(v);
      if (out.length >= MAX_LIST) break;
    }
    return out;
  }

  // Any saved value (a JSON string, an object, junk) becomes a whole, valid view; unknown fields are dropped.
  function normalizeView(value) {
    const v = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    return {
      hiddenRuntimes: cleanList(v.hiddenRuntimes).map(canonicalDomain).filter((d, i, all) => all.indexOf(d) === i),
      hiddenModels: cleanList(v.hiddenModels),
      resources: RESOURCE_MODES.includes(v.resources) ? v.resources : "all",
      layout: LAYOUTS.includes(v.layout) ? v.layout : "sphere",
    };
  }

  function parseView(text) {
    if (typeof text !== "string" || !text) return defaultView();
    try { return normalizeView(JSON.parse(text)); } catch (e) { return defaultView(); }
  }

  function serializeView(view) { return JSON.stringify(normalizeView(view)); }

  function isDefaultView(view) { return serializeView(view) === serializeView(defaultView()); }

  function agentShown(n, view) {
    if (view.hiddenRuntimes.includes(domainOfNode(n))) return false;
    return !view.hiddenModels.includes(modelKey(n));
  }

  // An edge counts as recent when it is in flight, pulsing, or last used within the activity window.
  function edgeRecent(e, now, windowS) {
    if (e.active || e.pulse) return true;
    const at = Number(e.lastAt);
    return Number.isFinite(now) && Number.isFinite(at) && at > 0 && now - at <= windowS;
  }

  // nodes: [{id, kind, domain|provider, model}]; edges: [{src, dst, active, pulse, lastAt}].
  // opts: {now (epoch s), activeWindow (s), keep: [ids always shown when present]}.
  // Returns {shown: Set of node ids, recent: Set of resource ids on a recent edge of a shown agent, total, count}.
  function filterGraph(nodes, edges, view, opts) {
    view = normalizeView(view);
    opts = opts || {};
    const windowS = Number.isFinite(opts.activeWindow) && opts.activeWindow > 0 ? opts.activeWindow : ACTIVE_WINDOW_S;
    const byId = new Map(nodes.map(n => [n.id, n]));
    const shown = new Set();
    for (const n of nodes) if (isAgent(n) && agentShown(n, view)) shown.add(n.id);
    // Each resource's links to agents: used by any agent at all, by a shown one, recently by a shown one.
    const links = new Map();
    for (const e of edges) {
      const a = byId.get(e.src), b = byId.get(e.dst);
      if (!a || !b || isAgent(a) === isAgent(b)) continue;
      const agent = isAgent(a) ? a : b, resource = isAgent(a) ? b : a;
      const link = links.get(resource.id) || { any: false, shown: false, recent: false };
      link.any = true;
      if (shown.has(agent.id)) {
        link.shown = true;
        if (edgeRecent(e, opts.now, windowS)) link.recent = true;
      }
      links.set(resource.id, link);
    }
    const recent = new Set();
    for (const n of nodes) {
      if (isAgent(n)) continue;
      const link = links.get(n.id) || { any: false, shown: false, recent: false };
      if (link.recent) recent.add(n.id);
      const keep = view.resources === "none" ? false
        : view.resources === "active" ? link.recent
        : !link.any || link.shown;   // "all": everything except what only hidden agents used
      if (keep) shown.add(n.id);
    }
    for (const id of opts.keep || []) if (byId.has(id)) shown.add(id);
    return { shown, recent, total: nodes.length, count: shown.size };
  }

  // The edges left once their nodes are filtered: both ends must be shown.
  function visibleEdges(edges, shown) { return edges.filter(e => shown.has(e.src) && shown.has(e.dst)); }

  // A point in direction d on the surface of the cube with this half-size: scale d so that its largest coordinate
  // reaches the half-size. The direction is kept; a zero vector goes to the centre of the +z face.
  function cubePoint(d, half) {
    const m = Math.max(Math.abs(d[0]), Math.abs(d[1]), Math.abs(d[2]));
    if (!(m > 1e-12)) return [0, 0, half];
    return [d[0] / m * half, d[1] / m * half, d[2] / m * half];
  }

  // The outward normal of the cube face a point sits on (the axis of its largest coordinate).
  function cubeNormal(p) {
    const ax = Math.abs(p[0]), ay = Math.abs(p[1]), az = Math.abs(p[2]);
    if (ax >= ay && ax >= az) return [Math.sign(p[0]) || 1, 0, 0];
    if (ay >= az) return [0, Math.sign(p[1]) || 1, 0];
    return [0, 0, Math.sign(p[2]) || 1];
  }

  // The 12 edges of a cube centred on the origin, as pairs of corners.
  function cubeEdges(half) {
    const out = [];
    for (let axis = 0; axis < 3; axis++) {
      for (const s1 of [-1, 1]) for (const s2 of [-1, 1]) {
        const a = [0, 0, 0], b = [0, 0, 0], [i, j] = [0, 1, 2].filter(k => k !== axis);
        a[axis] = -half; b[axis] = half;
        a[i] = b[i] = s1 * half; a[j] = b[j] = s2 * half;
        out.push([a, b]);
      }
    }
    return out;
  }

  // Which labels to draw under a cap: agents first (lower tier first), then resources on a recent edge, then the
  // rest, bigger first. items: [{key, agent, active, tier, weight}]. Returns a Set of keys.
  function pickLabels(items, cap) {
    const tier = t => (typeof t === "number" ? t : 99);
    const rank = it => (it.agent ? 0 : it.active ? 1 : 2);
    const sorted = [...items].sort((a, b) => rank(a) - rank(b) || tier(a.tier) - tier(b.tier) ||
      (b.weight || 0) - (a.weight || 0) || String(a.key).localeCompare(String(b.key)));
    return new Set(sorted.slice(0, Math.max(0, cap)).map(it => it.key));
  }

  const api = { RESOURCE_MODES, LAYOUTS, ACTIVE_WINDOW_S, own, canonicalDomain, runtimeName, modelKey, runtimesPresent,
    useRuntimes, servedRuntime, builtinRuntime, extraRuntime, hashColor, freeDirection,
    defaultView, normalizeView, parseView, serializeView, isDefaultView, filterGraph, visibleEdges, edgeRecent,
    cubePoint, cubeNormal, cubeEdges, pickLabels };
  if (root) root.PlaneFilters = api;
  if (typeof module === "object" && module && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : null);
