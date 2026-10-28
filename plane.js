// Plane view: every agent is a dot on one canvas, and everything an agent touches
// (web, documents, tools, messages) is a node it connects to. Non-agent nodes sit on one
// three-dimensional Voronoi field. Runtime endpoints own domains, their subagents stay
// inside those domains, and shared tools/files sit between the domains using them.
(function () {
  const POLL_MS = 1500;
  const WAITS_MS = 5000;
  const LANE_WINDOW_S = 30 * 60;   // the lane diagram covers the last 30 minutes
  const LANES_MAX = 8;
  const MIN_ZOOM = 0.35, MAX_ZOOM = 4;
  const REDUCED = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  const KIND_RADIUS = { agent: 8.5, subagent: 6, web: 12, doc: 5, area: 9, tool: 6, msg: 5 };
  const EDGE_COLOR = { call: "--working", spawn: "--blocked", message: "--done", reports: "--idle" };
  const MAX_LABELS = 40;
  const RECENT_PI_SUBAGENT_SECONDS = 120; // Give fast/completed child runs time to be noticed on the plane.
  const isAgent = n => n.kind === "agent" || n.kind === "subagent";
  const STATE_COLOR = { blocked: "--blocked", working: "--working", idle: "--idle", done: "--done", other: "--other" };
  const DOMAIN_COLORS = { claude: "#e9a960", claude_code: "#e9a960", "claude-code": "#e9a960",
    codex: "#9a88ff", pi: "#62d1c8", openai: "#6fa8ff", gpt: "#6fa8ff", cloud: "#74c7ec" };
  const OPENAI_MARK = "M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z";
  const GOOGLE_MARK = "M12.48 10.92v3.28h7.84c-.24 1.84-.853 3.187-1.787 4.133-1.147 1.147-2.933 2.4-6.053 2.4-4.827 0-8.6-3.893-8.6-8.72s3.773-8.72 8.6-8.72c2.6 0 4.507 1.027 5.907 2.347l2.307-2.307C18.747 1.44 16.133 0 12.48 0 5.867 0 .307 5.387.307 12s5.56 12 12.173 12c3.573 0 6.267-1.173 8.373-3.36 2.16-2.16 2.84-5.213 2.84-7.667 0-.76-.053-1.467-.173-2.053H12.48z";
  let openaiMarkPath = null, googleMarkPath = null;
  try {
    if (typeof Path2D === "function") {
      openaiMarkPath = new Path2D(OPENAI_MARK);
      googleMarkPath = new Path2D(GOOGLE_MARK);
    }
  } catch (e) { /* text marks below remain available if vector paths are unsupported */ }

  const canvas = document.getElementById("plane");
  const wrap = document.getElementById("plane-wrap");
  const ctx = canvas.getContext("2d");
  let active = false;
  let nodes = new Map();      // id -> node (sim state + display position)
  let edges = new Map();      // key -> edge
  let hover = null;           // node under the pointer
  let pointer = null;         // canvas pixel coordinates
  let drag = null;            // rotate or pan gesture
  let dragged = false;        // the last pointer gesture moved, so its click is ignored
  let view = { x: 0, y: 0, k: 1, yaw: -0.42, pitch: 0.42 };
  let cameraTargetId = null;  // node the camera orbits around after centering
  let colors = {};
  let width = 0, height = 0, dpr = 1;
  let sceneRadius = 260;
  let domainSites = new Map();
  let domainMeta = new Map();
  let simAlpha = 1;           // simulation heat: raised by new data, cooled every frame so the plane settles
  let selectedKey = null;     // edge clicked for the flow view
  let selectedAgent = null;   // agent clicked for its lanes; its lines stay visible while selected
  let selectedItems = new Map(); // ctrl-click selection for agent/resource details
  let flowTimer = null, waitsTimer = null;
  let panel = null, waitsPanel = null, closedPanel = null;
  let onStats = () => {};
  let closedOpen = false;
  try { closedOpen = localStorage.getItem("plane-closed-open") === "1"; } catch (e) { /* storage blocked */ }
  let lanesOpen = true, waitsOpen = true;
  let onOpen = () => {}, onWatch = () => {};

  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function readColors() {
    colors = { bg: css("--panel"), ink: css("--ink"), muted: css("--muted"), line: css("--line"), accent: css("--accent"), chip: css("--chip") };
    // --plane-style: crisp = solid edges and nodes, no additive glow (glow washes out on light pages)
    colors.crisp = css("--plane-style") === "crisp";
    // Optional per-theme agent look: a fixed fill behind the logo, and the domain ring width in px
    colors.agentFill = css("--agent-fill");
    colors.agentRing = parseFloat(css("--agent-ring")) || 1.5;
    for (const k of Object.keys(EDGE_COLOR)) colors["edge_" + k] = css(EDGE_COLOR[k]);
    for (const k of Object.keys(STATE_COLOR)) colors["state_" + k] = css(STATE_COLOR[k]);
  }

  function nodeColor(n) {
    if (n.kind === "agent" || n.kind === "subagent") {
      return colors["state_" + (n.state || (n.kind === "subagent" ? "idle" : "other"))] || colors.state_other;
    }
    return { web: colors.accent, doc: colors.ink, area: colors.accent, tool: colors.muted, msg: colors.edge_message }[n.kind] || colors.muted;
  }

  function localPoint(e) {
    const r = canvas.getBoundingClientRect();
    return { sx: e.clientX - r.left, sy: e.clientY - r.top };
  }

  function hash01(value) {
    let h = 2166136261;
    for (const ch of String(value)) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
    return (h >>> 0) / 4294967295;
  }

  function jitter3(value, radius) {
    const a = hash01(value + ":a") * Math.PI * 2;
    const z = hash01(value + ":z") * 2 - 1;
    const r = Math.sqrt(Math.max(0, 1 - z * z)) * radius;
    return [Math.cos(a) * r, Math.sin(a) * r, z * radius];
  }

  function domainOf(node) {
    const raw = String(node.domain || node.provider || "other").toLowerCase();
    return ({ "claude-code": "claude", claude_code: "claude", "claude code": "claude", anthropic: "claude",
      "codex-cli": "codex", codex_cli: "codex" })[raw] || raw;
  }

  function domainLabel(domain) {
    return ({ claude: "CLAUDE CODE", claude_code: "CLAUDE CODE", "claude-code": "CLAUDE CODE",
      codex: "CODEX / GPT", openai: "OPENAI / GPT", gpt: "GPT", pi: "PI", cloud: "CLOUD" })[domain] || String(domain).replace(/[-_]/g, " ").toUpperCase();
  }

  function domainColor(domain) { return DOMAIN_COLORS[domain] || colors.accent || "#8ba8ff"; }

  function agentBrand(n) {
    const model = String(n.model || "").toLowerCase();
    const domain = domainOf(n), provider = String(n.provider || "").toLowerCase();
    // A Codex runtime keeps its Codex mark even when its model is from the GPT family.
    if (domain === "codex" || provider.includes("codex")) return "codex";
    if (/gemini|argon|google/.test(model) || domain === "google" || provider === "google") return "google";
    if (/claude|anthropic/.test(model) || domain === "claude" || provider === "anthropic") return "claude";
    if (/gpt|openai/.test(model) || domain === "openai" || domain === "gpt") return "openai";
    if (domain === "pi" || provider === "pi" || model === "pi") return "pi";
    return domain !== "other" ? domain : provider || "agent";
  }

  function drawAgentLogo(target, x, y, size, brand, onNode = false) {
    if (!target || !size) return;
    target.save();
    target.translate(x, y);
    const white = onNode ? "#fff" : colors.ink || "#333";
    if ((brand === "codex" || brand === "openai") && openaiMarkPath) {
      target.translate(-size / 2, -size / 2);
      target.scale(size / 24, size / 24);
      target.fillStyle = brand === "codex" && !onNode ? "#10a37f" : white;
      target.fill(openaiMarkPath);
    } else if (brand === "google" && googleMarkPath) {
      target.translate(-size / 2, -size / 2);
      target.scale(size / 24, size / 24);
      target.save();
      target.clip(googleMarkPath);
      target.fillStyle = "#4285f4"; target.fillRect(12, 12, 12, 12);
      target.fillStyle = "#ea4335"; target.fillRect(12, 0, 12, 12);
      target.fillStyle = "#fbbc05"; target.fillRect(0, 12, 12, 12);
      target.fillStyle = "#34a853"; target.fillRect(0, 0, 12, 12);
      target.restore();
    } else if (brand === "claude") {
      // Claude's warm sunburst mark stays clear at the small sizes used by the map.
      const radius = size * 0.48;
      target.beginPath();
      for (let i = 0; i < 16; i++) {
        const a = -Math.PI / 2 + i * Math.PI / 8;
        const r = i % 2 ? radius * 0.48 : radius;
        const px = Math.cos(a) * r, py = Math.sin(a) * r;
        if (!i) target.moveTo(px, py); else target.lineTo(px, py);
      }
      target.closePath();
      target.fillStyle = onNode ? "#fff" : "#d97757";
      target.fill();
    } else if (brand === "pi") {
      target.fillStyle = white;
      target.font = `700 ${Math.max(8, size * 0.83)}px Georgia, serif`;
      target.textAlign = "center"; target.textBaseline = "middle";
      target.fillText("π", 0, 0.5);
    } else {
      const label = String(brand || "?").replace(/[^a-z0-9]/gi, "").slice(0, 1).toUpperCase() || "?";
      target.fillStyle = white;
      target.font = `700 ${Math.max(8, size * 0.72)}px system-ui, sans-serif`;
      target.textAlign = "center"; target.textBaseline = "middle";
      target.fillText(label, 0, 0.5);
    }
    target.restore();
  }

  function normalize3(v) {
    const d = Math.hypot(v[0], v[1], v[2]) || 1;
    return [v[0] / d, v[1] / d, v[2] / d];
  }

  function dot3(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
  function cross3(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
  function surfaceRadius() { return sceneRadius * 0.94; }

  function resourceClusterKey(node) {
    const group = String(node.group || "").toLowerCase();
    if (node.kind === "doc") return `files:${group || String(node.label || node.id).toLowerCase()}`;
    if (node.kind === "tool") {
      if (group.startsWith("tool:")) return group;
      const id = String(node.id || "").toLowerCase();
      if (id.startsWith("shared:tool:")) return `tool:${id.slice("shared:tool:".length)}`;
      return `tools:${group || String(node.groupLabel || node.label || node.id).toLowerCase()}`;
    }
    return `${node.kind}:${group || String(node.groupLabel || node.label || node.id).toLowerCase()}`;
  }

  function sphericalPatchPoint(direction, angle, distance) {
    if (distance <= 0) return direction;
    const axis = Math.abs(direction[0]) < 0.8 ? [1, 0, 0] : [0, 1, 0];
    const u = normalize3(cross3(direction, axis)), v = cross3(direction, u);
    const tangent = u.map((value, i) => value * Math.cos(angle) + v[i] * Math.sin(angle));
    return normalize3(direction.map((value, i) => value * Math.cos(distance) + tangent[i] * Math.sin(distance)));
  }

  function makeDomainSites(domains) {
    const fixed = {
      claude: [-0.94, -0.26, -0.22],
      pi: [0.92, 0.28, -0.18],
      codex: [-0.02, 0.08, 1],
      openai: [0.06, -0.94, 0.45],
      gpt: [0.06, -0.94, 0.45],
    };
    const sorted = [...new Set(domains)].sort();
    const sites = new Map();
    sorted.forEach((domain, i) => {
      let v = fixed[domain];
      if (!v) {
        const golden = Math.PI * (3 - Math.sqrt(5));
        const y = 1 - 2 * (i + 0.5) / Math.max(1, sorted.length);
        const r = Math.sqrt(1 - y * y);
        const theta = i * golden + hash01(domain) * 0.18;
        v = [Math.cos(theta) * r, y, Math.sin(theta) * r];
      }
      const p = normalize3(v).map(x => x * sceneRadius * 0.58);
      sites.set(domain, p);
    });
    if (sorted.length === 1) sites.set(sorted[0], [0, 0, 0]);
    return sites;
  }

  function cameraPoint(point) {
    const cy = Math.cos(view.yaw), sy = Math.sin(view.yaw);
    const cp = Math.cos(view.pitch), sp = Math.sin(view.pitch);
    const target = cameraTargetId && nodes.get(cameraTargetId);
    const origin = target && !target.gone ? [target.dx, target.dy, target.dz] : [0, 0, 0];
    const rx = point[0] - origin[0], ry = point[1] - origin[1], rz = point[2] - origin[2];
    const x = rx * cy + rz * sy;
    const z1 = -rx * sy + rz * cy;
    const y = ry * cp - z1 * sp;
    const z = ry * sp + z1 * cp;
    const focal = sceneRadius * 4.4;
    const scale = focal / Math.max(focal * 0.45, focal - z);
    return { x: width / 2 + view.x + x * scale * view.k, y: height / 2 + view.y + y * scale * view.k, depth: z, scale };
  }

  function weightedDomainPoint(node) {
    const weights = node.domainWeights || {};
    const entries = Object.entries(weights).filter(([domain, weight]) => domainSites.has(domain) && Number(weight) > 0);
    if (!entries.length) {
      const domains = node.domains || [];
      const picked = domains.filter(domain => domainSites.has(domain));
      if (!picked.length) return [0, 0, 0];
      return picked.reduce((sum, domain) => sum.map((v, i) => v + domainSites.get(domain)[i] / picked.length), [0,0,0]);
    }
    const total = entries.reduce((sum, [, weight]) => sum + Number(weight), 0);
    return entries.reduce((sum, [domain, weight]) => sum.map((v, i) => v + domainSites.get(domain)[i] * Number(weight) / total), [0,0,0]);
  }

  function layoutScene() {
    sceneRadius = Math.max(130, Math.min(width, height) * 0.46);
    const agents = [...nodes.values()].filter(n => !n.gone && isAgent(n));
    const domains = agents.map(domainOf);
    domainSites = makeDomainSites(domains);
    domainMeta = new Map();
    for (const n of agents) {
      const domain = domainOf(n), meta = domainMeta.get(domain) || { count: 0, cloud: false };
      meta.count += 1;
      meta.cloud = meta.cloud || n.environment === "cloud";
      domainMeta.set(domain, meta);
    }
    const depthOf = node => {
      let depth = 0, parent = node.parent, seen = new Set();
      while (parent && nodes.has(parent) && !seen.has(parent)) {
        seen.add(parent); depth++; parent = nodes.get(parent).parent;
      }
      return depth;
    };
    const orderedAgents = agents.sort((a, b) => depthOf(a) - depthOf(b));
    for (const n of orderedAgents) {
      const domain = domainOf(n), site = domainSites.get(domain) || [0,0,0];
      let target;
      if (n.kind === "subagent" && nodes.has(n.parent)) {
        const parent = nodes.get(n.parent);
        const offset = jitter3(n.id, sceneRadius * 0.15);
        target = [parent.ax + offset[0], parent.ay + offset[1], parent.az + offset[2]];
      } else {
        const offset = jitter3(n.id, sceneRadius * (n.kind === "subagent" ? 0.16 : 0.25));
        target = [site[0] + offset[0], site[1] + offset[1], site[2] + offset[2]];
      }
      [n.ax, n.ay, n.az] = target;
    }
    const resourceClusters = new Map();
    for (const n of nodes.values()) {
      if (isAgent(n) || n.gone) continue;
      const key = resourceClusterKey(n);
      let cluster = resourceClusters.get(key);
      if (!cluster) resourceClusters.set(key, cluster = { key, members: [], weights: {} });
      cluster.members.push(n);
      for (const [domain, weight] of Object.entries(n.domainWeights || {})) {
        if (domainSites.has(domain) && Number(weight) > 0) cluster.weights[domain] = (cluster.weights[domain] || 0) + Number(weight);
      }
      if (!Object.keys(n.domainWeights || {}).length) {
        for (const domain of n.domains || []) if (domainSites.has(domain)) cluster.weights[domain] = (cluster.weights[domain] || 0) + 1;
      }
    }
    const clustersByAffinity = new Map();
    for (const cluster of resourceClusters.values()) {
      cluster.members.sort((a, b) => String(a.id).localeCompare(String(b.id)));
      cluster.domains = Object.keys(cluster.weights).sort();
      cluster.affinity = cluster.domains.join("|") || "unassigned";
      const base = weightedDomainPoint({ domainWeights: cluster.weights, domains: cluster.domains });
      cluster.direction = Math.hypot(...base) > 1e-4 ? normalize3(base) : normalize3(jitter3(`runtime:${cluster.affinity}`, 1));
      const peers = clustersByAffinity.get(cluster.affinity) || [];
      peers.push(cluster);
      clustersByAffinity.set(cluster.affinity, peers);
    }
    const goldenAngle = Math.PI * (3 - Math.sqrt(5));
    for (const [affinity, clusters] of clustersByAffinity) {
      clusters.sort((a, b) => a.key.localeCompare(b.key));
      const cap = clusters.length < 2 ? 0 : Math.min(0.74, 0.12 + 0.11 * Math.sqrt(clusters.length));
      clusters.forEach((cluster, clusterIndex) => {
        const clusterAngle = clusterIndex * goldenAngle + hash01(affinity) * Math.PI * 2;
        const clusterDistance = cap * Math.sqrt((clusterIndex + 0.5) / clusters.length);
        cluster.direction = sphericalPatchPoint(cluster.direction, clusterAngle, clusterDistance);
        const memberCount = cluster.members.length;
        const memberSpread = memberCount < 2 ? 0 : Math.min(0.24, 0.045 * Math.sqrt(memberCount));
        cluster.members.forEach((n, memberIndex) => {
          const angle = memberIndex * goldenAngle + hash01(cluster.key) * Math.PI * 2;
          const distance = memberSpread * Math.sqrt((memberIndex + 0.5) / memberCount);
          const point = sphericalPatchPoint(cluster.direction, angle, distance);
          [n.ax, n.ay, n.az] = point.map(value => value * surfaceRadius());
          n.surfaceCluster = cluster.key;
          n.surfaceClusterSize = memberCount;
        });
      });
    }
  }

  async function poll() {
    if (!active) return;
    try {
      const res = await fetch("/api/graph", { cache: "no-store" });
      if (res.ok) applyGraph(await res.json());
    } catch (err) {
      /* server down: keep drawing the last plane */
    }
  }

  // Agents without an open session move to the folded "Closed" window. Keep recent Pi children
  // on the plane briefly so a quick subagent run remains visible after the parent tool returns.
  function splitClosed(data) {
    const now = Date.now() / 1000;
    const recentPiSubagents = new Set(data.nodes.filter(n => n.kind === "subagent" && n.provider === "pi" &&
      n.open === false && n.mtime && now - n.mtime <= RECENT_PI_SUBAGENT_SECONDS).map(n => n.id));
    const closed = data.nodes.filter(n => (n.kind === "agent" || n.kind === "subagent") && n.open === false &&
      !recentPiSubagents.has(n.id));
    if (!closed.length) return { nodes: data.nodes, edges: data.edges, closed };
    const gone = new Set(closed.map(n => n.id));
    const kept = data.edges.filter(e => !gone.has(e.src) && !gone.has(e.dst));
    const used = new Set();
    for (const e of kept) { used.add(e.src); used.add(e.dst); }
    const live = data.nodes.filter(n => n.kind === "agent" || n.kind === "subagent" ? !gone.has(n.id) : used.has(n.id));
    return { nodes: live, edges: kept, closed };
  }

  function placeResourcesByUsage(data) {
    const byId = new Map(data.nodes.map(n => [n.id, n])), usage = new Map();
    for (const node of data.nodes) if (!isAgent(node)) {
      node.domainWeights = {}; node.domains = []; node.cloudDomains = []; node.cloud = false;
    }
    for (const edge of data.edges) {
      const src = byId.get(edge.src), dst = byId.get(edge.dst);
      if (!src || !dst || !isAgent(src) || isAgent(dst)) continue;
      const domain = domainOf(src), record = usage.get(dst.id) || { weights: {}, cloud: new Set() };
      record.weights[domain] = (record.weights[domain] || 0) + (edge.count || 1);
      if (src.environment === "cloud") record.cloud.add(domain);
      usage.set(dst.id, record);
    }
    for (const node of data.nodes) {
      const record = usage.get(node.id);
      if (!record) continue;
      node.domainWeights = record.weights;
      node.domains = Object.keys(record.weights).sort();
      node.space = node.domains.length > 1 ? "shared" : node.domains[0];
      node.cloudDomains = [...record.cloud].sort();
      node.cloud = record.cloud.size > 0;
    }
  }

  function renderClosed(closed) {
    if (!closedPanel) return;
    closedPanel.win.hidden = !closed.length;
    closedPanel.title.textContent = `Closed · ${closed.length}`;
    const list = [...closed].sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
    const chips = list.map(n => {
      const chip = el("button", "closed-chip");
      chip.type = "button";
      const logo = document.createElement("canvas");
      logo.width = logo.height = 40;
      logo.className = "closed-brand-logo";
      logo.setAttribute("aria-hidden", "true");
      drawAgentLogo(logo.getContext("2d"), 20, 20, 27, agentBrand(n));
      const dot = el("span", "closed-dot");
      dot.style.background = nodeColor({ kind: n.kind, state: n.state });
      chip.append(logo, dot, el("span", "closed-name", n.name || n.label || n.id));
      chip.title = [n.name || n.label, n.role, n.model, n.state, n.kind].filter(Boolean).join(" · ");
      chip.setAttribute("aria-label", chip.title);
      if (n.jobId) chip.addEventListener("click", () => onOpen(n.jobId));
      else chip.disabled = true;
      return chip;
    });
    const grid = el("div", "closed-grid");
    grid.append(...chips);
    closedPanel.body.replaceChildren(grid);
  }

  function applyGraph(raw) {
    const data = splitClosed(raw);
    placeResourcesByUsage(data);
    renderClosed(data.closed);
    onStats(data.nodes.filter(n => isAgent(n) && n.open !== false));
    const seen = new Set();
    for (const n of data.nodes) {
      seen.add(n.id);
      let node = nodes.get(n.id);
      if (!node) {
        node = { id: n.id, x: 0, y: 0, z: 0, vx: 0, vy: 0, vz: 0,
                 dx: 0, dy: 0, dz: 0, born: performance.now(), gone: 0, ax: 0, ay: 0, az: 0 };
        nodes.set(n.id, node);
      }
      Object.assign(node, { kind: n.kind, label: n.label, name: n.name || n.label, state: n.state, jobId: n.jobId, gone: 0,
        group: n.group || n.kind, groupLabel: n.groupLabel || n.group || n.kind, role: n.role, model: n.model,
        provider: n.provider || "claude", domain: n.domain || n.provider || "claude", space: n.space || n.domain || n.provider || "claude",
        environment: n.environment || "local", cloud: !!n.cloud || n.environment === "cloud",
        cloudDomains: n.cloudDomains || [], domains: n.domains || [], domainWeights: n.domainWeights || {}, parent: n.parent,
        tier: typeof n.tier === "number" ? n.tier : null });
    }
    for (const [id, node] of nodes) {
      if (!seen.has(id) && !node.gone) node.gone = performance.now();
    }
    layoutScene();
    for (const n of nodes.values()) if (!n.placed && !n.gone) {
      n.x = n.dx = n.ax; n.y = n.dy = n.ay; n.z = n.dz = n.az; n.placed = true;
    }
    const keep = new Set();
    for (const e of data.edges) {
      const key = e.src + "|" + e.dst + "|" + e.kind;
      keep.add(key);
      const cur = edges.get(key) || { key, src: e.src, dst: e.dst, kind: e.kind, intensity: 0 };
      Object.assign(cur, { count: e.count, active: e.active, pulse: e.pulse });
      edges.set(key, cur);
    }
    for (const key of [...edges.keys()]) if (!keep.has(key)) edges.delete(key);
    simAlpha = Math.max(simAlpha, 0.6);  // new data reheats the layout a little
  }

  // Agents move inside their runtime domains. Resources form surface clusters by folder or
  // tool family, arranged near the weighted center of the runtime spaces that use them.
  function step(now) {
    simAlpha = Math.max(0.02, simAlpha * 0.985);
    const agents = [];
    for (const [id, n] of nodes) {
      if (n.gone && now - n.gone >= 900) { nodes.delete(id); continue; }
      if (n.gone) continue;
      if (isAgent(n)) { agents.push(n); n.fx = 0; n.fy = 0; n.fz = 0; n.sw = 0; n.sx = 0; n.sy = 0; n.sz = 0; }
      else {
        n.x += (n.ax - n.x) * 0.09; n.y += (n.ay - n.y) * 0.09; n.z += (n.az - n.z) * 0.09;
        n.dx = n.x; n.dy = n.y; n.dz = n.z;
      }
    }
    if (!REDUCED) {
      for (let i = 0; i < agents.length; i++) {
        const a = agents[i];
        for (let j = i + 1; j < agents.length; j++) {
          const b = agents[j];
          let dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z;
          if (dx === 0 && dy === 0 && dz === 0) { dx = hash01(a.id + b.id) - 0.5; dy = 0.3; dz = 0.2; }
          const d2 = Math.max(dx * dx + dy * dy + dz * dz, 100);
          const d = Math.sqrt(d2);
          const f = d < sceneRadius * 0.16 ? (sceneRadius * 0.16 - d) * 0.006 : 0;
          a.fx += (dx / d) * f; a.fy += (dy / d) * f; a.fz += (dz / d) * f;
          b.fx -= (dx / d) * f; b.fy -= (dy / d) * f; b.fz -= (dz / d) * f;
        }
      }
    }
    for (const e of edges.values()) {
      const a = nodes.get(e.src), b = nodes.get(e.dst);
      if (!a || !b || a.gone || b.gone) continue;
      const aa = isAgent(a), ba = isAgent(b);
      if (aa && ba) {
        if (e.kind === "spawn" || e.kind === "reports") {
          const dx = b.x - a.x, dy = b.y - a.y, dz = b.z - a.z;
          const d = Math.max(Math.hypot(dx, dy, dz), 1), rest = sceneRadius * 0.14;
          const f = (d - rest) * 0.002;
          a.fx += (dx / d) * f; a.fy += (dy / d) * f; a.fz += (dz / d) * f;
          b.fx -= (dx / d) * f; b.fy -= (dy / d) * f; b.fz -= (dz / d) * f;
        }
      } else if (aa !== ba) {
        const ag = aa ? a : b, rn = aa ? b : a;
        const w = e.active ? 1 : e.pulse ? 0.35 : 0.015;
        ag.sw += w; ag.sx += w * rn.x; ag.sy += w * rn.y; ag.sz += w * rn.z;
      }
    }
    for (const n of agents) {
      const targetX = n.ax, targetY = n.ay, targetZ = n.az;
      n.fx += (targetX - n.x) * (n.sw ? 0.008 : 0.016);
      n.fy += (targetY - n.y) * (n.sw ? 0.008 : 0.016);
      n.fz += (targetZ - n.z) * (n.sw ? 0.008 : 0.016);
      if (n.sw > 0) {
        const k = 0.006 * Math.min(1, n.sw);
        n.fx += (n.sx / n.sw - n.x) * k; n.fy += (n.sy / n.sw - n.y) * k; n.fz += (n.sz / n.sw - n.z) * k;
      }
      const damping = REDUCED ? 0.68 : 0.84;
      n.vx = (n.vx + n.fx * simAlpha) * damping;
      n.vy = (n.vy + n.fy * simAlpha) * damping;
      n.vz = (n.vz + n.fz * simAlpha) * damping;
      const maxV = REDUCED ? 0.5 : 2.8;
      const speed = Math.hypot(n.vx, n.vy, n.vz);
      if (speed > maxV) { n.vx *= maxV / speed; n.vy *= maxV / speed; n.vz *= maxV / speed; }
      n.x += n.vx; n.y += n.vy; n.z += n.vz;
      let center = domainSites.get(domainOf(n)) || [0,0,0], radius = sceneRadius * 0.52;
      if (n.kind === "subagent" && nodes.has(n.parent)) {
        const parent = nodes.get(n.parent);
        center = [parent.x, parent.y, parent.z]; radius = sceneRadius * 0.24;
      }
      const off = [n.x - center[0], n.y - center[1], n.z - center[2]], distance = Math.hypot(...off);
      if (distance > radius) {
        const scale = radius / distance;
        n.x = center[0] + off[0] * scale; n.y = center[1] + off[1] * scale; n.z = center[2] + off[2] * scale;
      }
      const sceneDistance = Math.hypot(n.x, n.y, n.z), sceneLimit = sceneRadius * 0.86;
      if (sceneDistance > sceneLimit) {
        const scale = sceneLimit / sceneDistance;
        n.x *= scale; n.y *= scale; n.z *= scale;
      }
      n.dx += (n.x - n.dx) * 0.12; n.dy += (n.y - n.dy) * 0.12; n.dz += (n.z - n.dz) * 0.12;
    }
  }

  // ---- Visibility: a line is shown only while in use. Hovering an agent reveals its
  // lines faintly, and the selected line stays visible while its flow is open.
  function targetIntensity(e) {
    if (e.active) return 1;
    if (e.key === selectedKey) return 0.9;
    if (selectedAgent && (e.src === selectedAgent || e.dst === selectedAgent)) return 0.5;
    if (e.pulse) return 0.55;
    if (hover && (e.src === hover.id || e.dst === hover.id)) return 0.4;
    const resource = nodes.get(e.dst);
    if (resource && !isAgent(resource) && (resource.domains || []).length > 1) return 0.2;
    if (e.kind === "reports") return 0.18;  // the org chart stays faintly readable
    return 0;
  }

  function nodeAt(sx, sy) {
    let best = null, bestD = 18;
    for (const n of nodes.values()) {
      if (n.gone || !Number.isFinite(n.sx)) continue;
      const d = Math.hypot(n.sx - sx, n.sy - sy);
      if (d < bestD) { best = n; bestD = d; }
    }
    return best;
  }

  // Distance from a canvas point to a projected 3D edge.
  function segmentDistance(px, py, a, b) {
    const dx = b.sx - a.sx, dy = b.sy - a.sy;
    const len2 = dx * dx + dy * dy || 1;
    const t = Math.max(0, Math.min(1, ((px - a.sx) * dx + (py - a.sy) * dy) / len2));
    return Math.hypot(px - (a.sx + dx * t), py - (a.sy + dy * t));
  }

  // Only lines that are currently drawn can be clicked.
  function edgeAt(sx, sy) {
    let best = null, bestD = 9;
    for (const e of edges.values()) {
      if (e.intensity < 0.05) continue;  // any line still drawn can be clicked
      const a = nodes.get(e.src), b = nodes.get(e.dst);
      if (!a || !b || a.gone || b.gone || !Number.isFinite(a.sx) || !Number.isFinite(b.sx)) continue;
      const d = segmentDistance(sx, sy, a, b);
      if (d < bestD) { best = e; bestD = d; }
    }
    return best;
  }

  function drawGlow(x, y, r, color, alpha) {
    const g = ctx.createRadialGradient(x, y, 0, x, y, r * 3);
    g.addColorStop(0, withAlpha(color, alpha));
    g.addColorStop(1, withAlpha(color, 0));
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.arc(x, y, r * 3, 0, Math.PI * 2); ctx.fill();
  }

  function withAlpha(color, a) {
    // Works for hex and for rgb()/rgba() from computed styles.
    const m = color.match(/rgba?\(([^)]+)\)/);
    if (m) {
      const parts = m[1].split(/[ ,\/]+/).filter(Boolean);
      return `rgba(${parts[0]}, ${parts[1]}, ${parts[2]}, ${a})`;
    }
    const hex = color.replace("#", "");
    const full = hex.length === 3 ? hex.split("").map(c => c + c).join("") : hex;
    const r = parseInt(full.slice(0, 2), 16), g = parseInt(full.slice(2, 4), 16), b = parseInt(full.slice(4, 6), 16);
    return `rgba(${r}, ${g}, ${b}, ${a})`;
  }

  function drawCloudIcon(x, y, size, color) {
    const s = size;
    ctx.save();
    ctx.lineWidth = Math.max(1, s * 0.13);
    ctx.strokeStyle = color;
    ctx.fillStyle = withAlpha(colors.bg, 0.92);
    ctx.beginPath();
    ctx.moveTo(x - s * 0.46, y + s * 0.22);
    ctx.lineTo(x - s * 0.46, y + s * 0.08);
    ctx.quadraticCurveTo(x - s * 0.46, y - s * 0.06, x - s * 0.31, y - s * 0.06);
    ctx.quadraticCurveTo(x - s * 0.25, y - s * 0.35, x + s * 0.03, y - s * 0.3);
    ctx.quadraticCurveTo(x + s * 0.24, y - s * 0.28, x + s * 0.28, y - s * 0.05);
    ctx.quadraticCurveTo(x + s * 0.49, y - s * 0.08, x + s * 0.48, y + s * 0.13);
    ctx.lineTo(x + s * 0.48, y + s * 0.22);
    ctx.closePath();
    ctx.fill(); ctx.stroke();
    ctx.restore();
  }

  function drawCellMesh() {
    const entries = [...domainSites.entries()];
    const envelope = surfaceRadius();
    const samples = 240;
    const distanceSquared = (a, b) => (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2;
    for (let i = 0; i < entries.length; i++) for (let j = i + 1; j < entries.length; j++) {
      const [domain, site] = entries[i], [otherDomain, otherSite] = entries[j];
      const delta = otherSite.map((value, axis) => value - site[axis]);
      const length = Math.hypot(...delta);
      if (!length) continue;
      const normal = delta.map(value => value / length);
      const offset = (dot3(otherSite, otherSite) - dot3(site, site)) / (2 * length);
      if (Math.abs(offset) >= envelope) continue;
      const center = normal.map(value => value * offset);
      const radius = Math.sqrt(envelope * envelope - offset * offset);
      const axis = Math.abs(normal[0]) < 0.8 ? [1, 0, 0] : [0, 1, 0];
      const u = normalize3(cross3(normal, axis)), v = cross3(normal, u);
      const points = [];
      for (let k = 0; k < samples; k++) {
        const angle = k / samples * Math.PI * 2;
        const point = center.map((value, dim) => value + radius * (Math.cos(angle) * u[dim] + Math.sin(angle) * v[dim]));
        const ownDistance = distanceSquared(point, site);
        const nearest = entries.every(([candidate, candidateSite]) =>
          candidate === domain || candidate === otherDomain || ownDistance <= distanceSquared(point, candidateSite) + envelope * envelope * 0.002);
        points.push(nearest ? cameraPoint(point) : null);
      }
      const validCount = points.filter(Boolean).length;
      if (!validCount) continue;
      const paths = [];
      if (validCount === samples) {
        paths.push({ points, closed: true });
      } else {
        const start = points.findIndex(point => !point);
        let path = [];
        for (let step = 1; step <= samples; step++) {
          const point = points[(start + step) % samples];
          if (point) path.push(point);
          else if (path.length > 1) { paths.push({ points: path, closed: false }); path = []; }
          else path = [];
        }
        if (path.length > 1) paths.push({ points: path, closed: false });
      }
      ctx.fillStyle = withAlpha(domainColor(domain), 0.018);
      ctx.strokeStyle = withAlpha(domainColor(domain), 0.065);
      ctx.lineWidth = 0.8;
      for (const path of paths) {
        ctx.beginPath();
        ctx.moveTo(path.points[0].x, path.points[0].y);
        for (let k = 1; k < path.points.length; k++) ctx.lineTo(path.points[k].x, path.points[k].y);
        if (path.closed) ctx.closePath();
        ctx.fill();
        ctx.stroke();
      }
    }
  }

  function drawDomains() {
    for (const [domain, site] of domainSites) {
      const color = domainColor(domain), meta = domainMeta.get(domain) || { count: 0, cloud: false };
      const labelPoint = cameraPoint([site[0], site[1] - sceneRadius * 0.28, site[2]]);
      ctx.font = "600 11px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillStyle = withAlpha(color, 0.94);
      const label = `${domainLabel(domain)} · ${meta.count}`;
      ctx.fillText(label, labelPoint.x, labelPoint.y);
      if (meta.cloud) drawCloudIcon(labelPoint.x + ctx.measureText(label).width / 2 + 13, labelPoint.y - 4, 15, color);
    }
    ctx.textAlign = "start";
  }

  function convexHull2D(points) {
    const sorted = [...points].sort((a, b) => a.x - b.x || a.y - b.y);
    if (sorted.length <= 2) return sorted;
    const cross = (o, a, b) => (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);
    const lower = [], upper = [];
    for (const point of sorted) {
      while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], point) <= 0) lower.pop();
      lower.push(point);
    }
    for (let i = sorted.length - 1; i >= 0; i--) {
      const point = sorted[i];
      while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], point) <= 0) upper.pop();
      upper.push(point);
    }
    lower.pop(); upper.pop();
    return lower.concat(upper);
  }

  function drawResourceClusterHints() {
    const clusters = new Map();
    for (const n of nodes.values()) {
      if (isAgent(n) || n.gone || !n.surfaceCluster || !Number.isFinite(n.sx)) continue;
      const members = clusters.get(n.surfaceCluster) || [];
      members.push(n);
      clusters.set(n.surfaceCluster, members);
    }
    ctx.save();
    ctx.globalAlpha = 1;
    ctx.lineCap = "round";
    for (const members of clusters.values()) {
      if (members.length < 2) continue;
      const points = members.map(n => ({ x: n.sx, y: n.sy }));
      const colorWeights = {};
      for (const n of members) for (const [domain, weight] of Object.entries(n.domainWeights || {})) {
        colorWeights[domain] = (colorWeights[domain] || 0) + Number(weight || 0);
      }
      const dominant = Object.keys(colorWeights).sort((a, b) => colorWeights[b] - colorWeights[a])[0];
      const color = dominant ? domainColor(dominant) : colors.line;
      if (points.length === 2) {
        ctx.beginPath(); ctx.moveTo(points[0].x, points[0].y); ctx.lineTo(points[1].x, points[1].y);
        ctx.strokeStyle = withAlpha(color, 0.075); ctx.lineWidth = 8; ctx.stroke();
        ctx.strokeStyle = withAlpha(color, 0.2); ctx.lineWidth = 0.8; ctx.stroke();
        continue;
      }
      const hull = convexHull2D(points);
      const center = points.reduce((sum, point) => ({ x: sum.x + point.x / points.length, y: sum.y + point.y / points.length }), { x: 0, y: 0 });
      const padded = hull.map(point => {
        const dx = point.x - center.x, dy = point.y - center.y, length = Math.hypot(dx, dy) || 1;
        return { x: point.x + dx / length * 7, y: point.y + dy / length * 7 };
      });
      ctx.beginPath(); ctx.moveTo(padded[0].x, padded[0].y);
      for (let i = 1; i < padded.length; i++) ctx.lineTo(padded[i].x, padded[i].y);
      ctx.closePath();
      ctx.fillStyle = withAlpha(color, 0.022); ctx.fill();
      ctx.strokeStyle = withAlpha(color, 0.2); ctx.lineWidth = 0.85; ctx.stroke();
    }
    ctx.restore();
  }

  function draw(now) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    for (const n of nodes.values()) {
      if (n.gone) continue;
      const p = cameraPoint([n.dx, n.dy, n.dz]);
      n.sx = p.x; n.sy = p.y; n.depth = p.depth; n.projectScale = p.scale;
    }
    drawCellMesh();
    drawDomains();
    drawResourceClusterHints();

    ctx.globalCompositeOperation = colors.crisp ? "source-over" : "lighter";
    for (const e of edges.values()) {
      const a = nodes.get(e.src), b = nodes.get(e.dst);
      if (!a || !b) continue;
      const target = targetIntensity(e);
      e.intensity += (target - e.intensity) * 0.08;
      const color = colors["edge_" + e.kind] || colors.accent;
      const fade = (a.gone ? 1 - (now - a.gone) / 900 : 1) * (b.gone ? 1 - (now - b.gone) / 900 : 1);
      const alpha = Math.max(0, e.intensity * fade);
      if (alpha < 0.02 || !Number.isFinite(a.sx) || !Number.isFinite(b.sx)) continue;
      const picked = e.key === selectedKey;
      ctx.lineWidth = picked ? 2.8 : e.active ? 2 : 1;
      ctx.strokeStyle = withAlpha(color, picked ? Math.max(alpha, 0.82) : alpha * 0.85);
      ctx.beginPath(); ctx.moveTo(a.sx, a.sy); ctx.lineTo(b.sx, b.sy); ctx.stroke();
      if (e.active) {
        const t = (now % 1400) / 1400;
        const p = cameraPoint([a.dx + (b.dx - a.dx) * t, a.dy + (b.dy - a.dy) * t, a.dz + (b.dz - a.dz) * t]);
        ctx.fillStyle = withAlpha(color, alpha);
        ctx.beginPath(); ctx.arc(p.x, p.y, 2.2 * p.scale, 0, Math.PI * 2); ctx.fill();
      }
    }
    ctx.globalCompositeOperation = "source-over";

    const agents = [...nodes.values()].filter(n => isAgent(n) && !n.gone);
    const crowded = agents.length > MAX_LABELS;
    const drawNodes = [...nodes.values()].filter(n => !n.gone).sort((a, b) => a.depth - b.depth);
    for (const n of drawNodes) {
      const life = Math.min(1, (now - n.born) / 600);
      if (life <= 0) continue;
      const agent = isAgent(n);
      const radius = radiusOf(n) * Math.max(0.72, Math.min(1.3, n.projectScale || 1));
      const color = nodeColor(n), outline = agent ? domainColor(domainOf(n)) : colors.line;
      const selected = selectedItems.has(n.id) || selectedAgent === n.id;
      ctx.globalAlpha = life * (agent ? 1 : colors.crisp ? 0.75 : 0.4);
      if (agent && !colors.crisp) {
        ctx.globalCompositeOperation = "lighter";
        drawGlow(n.sx, n.sy, radius, color, 0.32);
      }
      ctx.globalCompositeOperation = "source-over";
      ctx.fillStyle = agent && colors.agentFill ? colors.agentFill : color;
      ctx.beginPath(); ctx.arc(n.sx, n.sy, radius, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = n === hover ? colors.ink : selected ? colors.accent : outline;
      ctx.lineWidth = n === hover || selected ? 2.5 : agent ? colors.agentRing : 0.8;
      ctx.beginPath(); ctx.arc(n.sx, n.sy, radius + (n === hover || selected ? 3 : 1), 0, Math.PI * 2); ctx.stroke();
      if (agent) drawAgentLogo(ctx, n.sx, n.sy, Math.max(8, Math.min(16, radius * 1.42)), agentBrand(n), true);
      if (n.cloud || n.environment === "cloud") drawCloudIcon(n.sx + radius * 0.78, n.sy - radius * 0.82, 13, outline);
      if (agent && n !== hover && (!crowded || (n.tier !== null && n.tier <= 1))) {
        ctx.font = n.kind === "subagent" ? "10px system-ui, sans-serif" : "11px system-ui, sans-serif";
        ctx.textAlign = "center"; ctx.fillStyle = colors.muted;
        ctx.fillText(truncate(n.name || n.label || "", 24), n.sx, n.sy + radius + 13);
      }
      ctx.globalAlpha = 1;
    }
    ctx.textAlign = "start";
    if (hover) drawLabel(hover);
  }

  function radiusOf(n) {
    const base = KIND_RADIUS[n.kind] || 5;
    return isAgent(n) && n.tier !== null ? base + (n.tier === 0 ? 3 : n.tier === 1 ? 1.5 : 0) : base;
  }

  function truncate(s, max) { s = String(s); return s.length > max ? s.slice(0, max - 1) + "\u2026" : s; }

  function drawLabel(n) {
    const lines = isAgent(n)
      ? [n.name || n.label, [domainLabel(domainOf(n)), n.role, n.model, n.state, n.environment === "cloud" ? "cloud" : ""].filter(Boolean).join(" \u00b7 ") || n.kind]
      : [n.name || n.label, [n.kind, n.groupLabel && n.groupLabel !== n.kind ? n.groupLabel : "",
          n.surfaceClusterSize > 1 ? `${n.surfaceClusterSize} related resources` : "",
          n.domains && n.domains.length ? n.domains.map(domainLabel).join(" / ") : "",
          n.cloud ? "used in cloud" : ""].filter(Boolean).join(" \u00b7 ")];
    const sx = n.sx, sy = n.sy;
    ctx.font = "13px system-ui, sans-serif";
    const w = Math.max(...lines.map(l => ctx.measureText(l).width)) + 16;
    const h = 40;
    let x = sx + 14, y = sy - h - 6;
    if (x + w > width - 8) x = sx - w - 14;
    if (y < 8) y = sy + 14;
    ctx.fillStyle = colors.bg;
    ctx.strokeStyle = colors.line;
    ctx.lineWidth = 1;
    ctx.fillRect(x, y, w, h);
    ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
    ctx.fillStyle = colors.ink;
    ctx.fillText(lines[0], x + 8, y + 16);
    ctx.fillStyle = colors.muted;
    ctx.fillText(lines[1], x + 8, y + 32);
  }

  function resize() {
    const rect = wrap.getBoundingClientRect();
    width = Math.max(1, rect.width);
    height = Math.max(1, rect.height);
    dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    layoutScene();
  }

  function loop(now) {
    requestAnimationFrame(loop);
    if (!active) return;
    step(now);
    draw(now);
  }

  // ---- Pointer: drag through both poles to rotate, shift-drag to pan, wheel to zoom.
  canvas.addEventListener("pointerdown", e => {
    canvas.setPointerCapture(e.pointerId);
    const p = localPoint(e);
    drag = { sx: p.sx, sy: p.sy, vx: view.x, vy: view.y, yaw: view.yaw, pitch: view.pitch,
      mode: e.shiftKey || e.button === 1 ? "pan" : "rotate", moved: false };
    dragged = false;
  });

  canvas.addEventListener("pointermove", e => {
    const p = localPoint(e);
    pointer = p;
    if (drag) {
      const dx = p.sx - drag.sx, dy = p.sy - drag.sy;
      if (!drag.moved && Math.hypot(dx, dy) > 4) drag.moved = true;
      if (drag.moved) {
        if (drag.mode === "pan") {
          view.x = drag.vx + dx;
          view.y = drag.vy + dy;
        } else {
          view.yaw = drag.yaw + dx * 0.006;
          view.pitch = Math.max(-Math.PI / 2, Math.min(Math.PI / 2, drag.pitch + dy * 0.006));
        }
        hover = null;
        canvas.style.cursor = "grabbing";
        return;
      }
    }
    hover = nodeAt(pointer.sx, pointer.sy);
    const onEdge = !hover && edgeAt(pointer.sx, pointer.sy);
    canvas.style.cursor = hover || onEdge ? "pointer" : "default";
  });

  canvas.addEventListener("pointerup", () => {
    dragged = !!(drag && drag.moved);
    drag = null;
  });
  canvas.addEventListener("pointercancel", () => { drag = null; dragged = false; });
  canvas.addEventListener("pointerleave", () => { if (!drag) { hover = null; pointer = null; } });

  canvas.addEventListener("click", e => {
    if (dragged) { dragged = false; return; }  // a pan is not a click
    if (hover && (e.ctrlKey || e.metaKey)) { toggleNodeSelection(hover); return; }
    if (hover && isAgent(hover)) { selectAgent(hover); return; }
    if (hover) { selectNodeInfo(hover); return; }
    const edge = pointer && edgeAt(pointer.sx, pointer.sy);
    if (edge) selectEdge(edge);
  });

  const centerButton = wrap.querySelector('[data-camera-view="center"]');

  function centerNode(node) {
    if (!node || node.gone) return;
    cameraTargetId = node.id;
    view.x = 0;
    view.y = 0;
    hover = null;
    pointer = null;
  }

  function selectedCenterNode() {
    const selected = [...selectedItems.values()];
    if (selected.length) return selected[selected.length - 1];
    if (selectedAgent) return nodes.get(selectedAgent) || null;
    const edge = selectedKey && edges.get(selectedKey);
    return edge ? (nodes.get(edge.dst) || nodes.get(edge.src) || null) : null;
  }

  function syncCenterButton() {
    if (centerButton) centerButton.disabled = !selectedCenterNode();
  }

  function centerSelection() { centerNode(selectedCenterNode()); }

  function resetView() {
    view = { x: 0, y: 0, k: 1, yaw: -0.42, pitch: 0.42 };
    cameraTargetId = null;
  }

  canvas.addEventListener("dblclick", e => {
    const p = localPoint(e);
    if (!nodeAt(p.sx, p.sy) && !edgeAt(p.sx, p.sy)) closeFlow();
  });
  for (const button of wrap.querySelectorAll("[data-camera-view]")) {
    button.addEventListener("click", () => {
      if (button.dataset.cameraView === "zenith") view.pitch = Math.PI / 2;
      else if (button.dataset.cameraView === "nadir") view.pitch = -Math.PI / 2;
      else if (button.dataset.cameraView === "center") centerSelection();
      else resetView();
      hover = null;
      pointer = null;
    });
  }

  canvas.addEventListener("wheel", e => {
    e.preventDefault();
    const k2 = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, view.k * Math.exp(-e.deltaY * 0.0015)));
    const p = localPoint(e);
    const px = p.sx - width / 2, py = p.sy - height / 2;
    // Keep the point under the pointer where it is while zooming.
    view.x = px - (px - view.x) * (k2 / view.k);
    view.y = py - (py - view.y) * (k2 / view.k);
    view.k = k2;
  }, { passive: false });

  // ---- Flow window: the calls on one line, plus a lane diagram of the agent's last 30 minutes.
  function selectEdge(edge) {
    selectedItems.clear();
    selectedKey = edge.key;
    selectedAgent = null;
    syncCenterButton();
    startFlow();
  }

  function selectAgent(node) {
    selectedItems.clear();
    selectedAgent = node.id;
    selectedKey = null;
    syncCenterButton();
    startFlow();
  }

  function selectNodeInfo(node) {
    selectedItems.clear();
    selectedItems.set(node.id, node);
    selectedAgent = null;
    selectedKey = null;
    syncCenterButton();
    clearInterval(flowTimer);
    flowTimer = null;
    renderSelectionPanel();
  }

  function toggleNodeSelection(node) {
    if (!selectedItems.size && selectedAgent && selectedAgent !== node.id) {
      const current = nodes.get(selectedAgent);
      if (current) selectedItems.set(current.id, current);
    }
    if (selectedItems.has(node.id)) selectedItems.delete(node.id);
    else selectedItems.set(node.id, node);
    selectedAgent = null;
    selectedKey = null;
    syncCenterButton();
    clearInterval(flowTimer);
    flowTimer = null;
    if (selectedItems.size) renderSelectionPanel();
    else closeFlow();
  }

  function startFlow() {
    panel.hidden = false;
    clearInterval(flowTimer);
    flowTimer = setInterval(fetchFlow, POLL_MS);
    fetchFlow();
  }

  function closeFlow() {
    selectedKey = null;
    selectedAgent = null;
    selectedItems.clear();
    clearInterval(flowTimer);
    flowTimer = null;
    panel.hidden = true;
    syncCenterButton();
  }

  function nodeFilePath(node) {
    if (node.kind === "doc" && String(node.id).startsWith("doc:")) return String(node.id).slice(4);
    if (node.kind === "area" && node.id === "area:shared") return node.label || "";
    return "";
  }

  function containingFolder(path) {
    const normalized = String(path || "").replace(/\\/g, "/").replace(/\/$/, "");
    const slash = normalized.lastIndexOf("/");
    return slash > 0 ? normalized.slice(0, slash) : "";
  }

  function appendInfoRow(list, name, value) {
    if (value == null || value === "") return;
    const term = el("dt", "selection-label", name);
    const detail = el("dd", "selection-value", String(value));
    list.append(term, detail);
  }

  async function requestOpenFolder(node, status) {
    status.textContent = "Opening folder…";
    try {
      const res = await fetch("/api/open-folder", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: node.id }), cache: "no-store",
      });
      const raw = await res.text();
      let data = {};
      try { data = raw ? JSON.parse(raw) : {}; } catch {}
      if (!res.ok) {
        const reason = data.error || res.statusText || `HTTP ${res.status}`;
        throw new Error(reason);
      }
      if (!data.folder) throw new Error("server did not return a folder path");
      status.textContent = `Opened ${data.folder}`;
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      status.textContent = `Could not open that folder: ${reason}`;
    }
  }

  function renderSelectionPanel() {
    panel.hidden = false;
    const head = el("div", "flow-head");
    head.append(el("div", "flow-title", `Selected items · ${selectedItems.size}`),
                el("div", "flow-kind", "Ctrl-click or Command-click nodes to add or remove them."));
    const actions = el("div", "flow-actions");
    actions.append(button("Close", closeFlow));
    head.append(actions);

    const cards = [...selectedItems.values()].map(node => {
      const card = el("section", "selection-card");
      const cardHead = el("div", "selection-card-head");
      cardHead.append(el("div", "selection-item-title", node.name || node.label || node.id));
      const remove = button("×", () => {
        selectedItems.delete(node.id);
        if (selectedItems.size) renderSelectionPanel(); else closeFlow();
      });
      remove.className = "selection-remove";
      remove.title = "Remove from selection";
      remove.setAttribute("aria-label", `Remove ${node.name || node.label || node.id}`);
      cardHead.append(remove);
      card.append(cardHead);

      const details = el("dl", "selection-details");
      appendInfoRow(details, "Kind", node.kind);
      if (isAgent(node)) {
        appendInfoRow(details, "Runtime", domainLabel(domainOf(node)));
        appendInfoRow(details, "State", node.state);
        appendInfoRow(details, "Role", node.role);
        appendInfoRow(details, "Model", node.model);
        appendInfoRow(details, "Environment", node.environment);
      } else {
        appendInfoRow(details, "Space", node.space === "shared" ? "Shared" : domainLabel(node.space || domainOf(node)));
        appendInfoRow(details, "Group", node.groupLabel || node.group);
        appendInfoRow(details, "Used by", (node.domains || []).map(domainLabel).join(" / "));
        if (node.cloud) appendInfoRow(details, "Cloud", "Used by cloud agents");
      }
      const path = nodeFilePath(node);
      appendInfoRow(details, "Path", path);
      if (node.kind === "doc") appendInfoRow(details, "Containing folder", containingFolder(path));
      appendInfoRow(details, "ID", node.id);
      card.append(details);

      const status = el("div", "selection-status");
      const itemActions = el("div", "flow-actions selection-actions");
      if (node.jobId) {
        itemActions.append(button("Open agent", () => onOpen(node.jobId)),
                           button("Watch live", () => onWatch(node.jobId, node.name || node.label)));
      }
      itemActions.append(button("Center", () => centerNode(node)));
      if (node.kind === "doc" || node.id === "area:shared") {
        itemActions.append(button(node.kind === "doc" ? "Open containing folder" : "Open shared folder",
                                  () => requestOpenFolder(node, status)));
      }
      if (itemActions.childElementCount) card.append(itemActions, status);
      return card;
    });
    const list = el("div", "selection-list");
    list.replaceChildren(...cards);
    panel.replaceChildren(head, list);
  }

  // The agent's own view: its lanes and its newest calls across everything it touched.
  async function fetchAgent() {
    const node = nodes.get(selectedAgent);
    if (!node) { closeFlow(); return; }
    try {
      const res = await fetch("/api/flow?src=" + encodeURIComponent(node.id), { cache: "no-store" });
      if (res.ok && selectedAgent === node.id) renderAgentFlow(node, (await res.json()).agent || []);
    } catch (err) {
      /* server down: keep the last rows */
    }
  }

  function renderAgentFlow(node, agentEvents) {
    const head = el("div", "flow-head");
    head.append(el("div", "flow-title", node.name || node.label),
                el("div", "flow-kind", `${node.kind}${node.state ? " · " + node.state : ""} · ${agentEvents.length} recent calls`));
    const actions = el("div", "flow-actions");
    if (node.jobId) {
      actions.append(button("Open agent", () => onOpen(node.jobId)),
                     button("Watch live", () => onWatch(node.jobId, node.name || node.label)));
    }
    actions.append(button("Center", () => centerNode(node)));
    actions.append(button("Close", closeFlow));
    head.append(actions);
    const newest = [...agentEvents].reverse().slice(0, 40).map(c => {
      const row = el("li", "flow-row" + (c.ms == null ? " running" : ""));
      row.append(el("div", "flow-when", ago(c.at)),
                 el("div", "flow-tool", `${c.tool} → ${labelOf(c.dst)}`),
                 el("div", "flow-detail", c.detail || "(no detail)"),
                 el("div", "flow-ms", c.ms == null ? "running" : (c.ms / 1000).toFixed(1) + "s"));
      return row;
    });
    const list = el("ul", "flow-list");
    list.replaceChildren(...(newest.length ? newest : [el("li", "flow-empty", "no calls in the last 6 hours")]));
    panel.replaceChildren(head, renderLanes({ dst: null }, agentEvents), list);
  }

  async function fetchFlow() {
    if (selectedAgent) { await fetchAgent(); return; }
    const edge = selectedKey && edges.get(selectedKey);
    if (!edge) { if (selectedKey) closeFlow(); return; }  // the edge went away
    const q = new URLSearchParams({ src: edge.src, dst: edge.dst, kind: edge.kind });
    try {
      const res = await fetch("/api/flow?" + q.toString(), { cache: "no-store" });
      if (res.ok && selectedKey === edge.key) renderFlow(edge, await res.json());
    } catch (err) {
      /* server down: keep the last rows */
    }
  }

  function labelOf(id) {
    const n = nodes.get(id);
    return n ? (n.name || n.label) : id;
  }

  function ago(sec) {
    const s = Math.max(0, Date.now() / 1000 - sec);
    if (s < 60) return Math.floor(s) + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    return Math.floor(s / 3600) + "h ago";
  }

  function fmtDur(sec) {
    sec = Math.round(sec);
    if (sec < 60) return sec + "s";
    if (sec < 3600) return Math.floor(sec / 60) + "m" + String(sec % 60).padStart(2, "0") + "s";
    return Math.floor(sec / 3600) + "h" + String(Math.floor((sec % 3600) / 60)).padStart(2, "0") + "m";
  }

  // Text only: call details come from transcripts, so they are never parsed as markup.
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function button(label, onClick) {
    const b = el("button", "", label);
    b.type = "button";
    b.addEventListener("click", onClick);
    return b;
  }

  // A window with a title bar that folds the body away.
  function foldWindow(title, cls, isOpen, onToggle) {
    const win = el("section", "fold " + cls);
    const bar = el("button", "fold-bar");
    bar.type = "button";
    const caret = el("span", "fold-caret", isOpen ? "▾" : "▸");
    bar.append(caret, el("span", "fold-title", title));
    const body = el("div", "fold-body");
    body.hidden = !isOpen;
    bar.setAttribute("aria-expanded", String(isOpen));
    bar.addEventListener("click", () => {
      const open = body.hidden;
      body.hidden = !open;
      caret.textContent = open ? "▾" : "▸";
      bar.setAttribute("aria-expanded", String(open));
      onToggle(open);
    });
    win.append(bar, body);
    return { win, body, title: bar.querySelector(".fold-title") };
  }

  function renderFlow(edge, data) {
    const src = nodes.get(edge.src);
    const head = el("div", "flow-head");
    head.append(el("div", "flow-title", `${labelOf(edge.src)} → ${labelOf(edge.dst)}`),
                el("div", "flow-kind", `${edge.kind} · ${edge.count} total`));
    const actions = el("div", "flow-actions");
    if (src && src.jobId) {
      actions.append(button("Open agent", () => onOpen(src.jobId)),
                     button("Watch live", () => onWatch(src.jobId, src.name || src.label)));
    }
    actions.append(button("Center", () => centerNode(nodes.get(edge.dst) || src)));
    actions.append(button("Close", closeFlow));
    head.append(actions);

    const edgeEvents = data.edge || [];
    const maxMs = Math.max(1, ...edgeEvents.map(c => c.ms || 0));
    const rows = [...edgeEvents].reverse().map(c => {
      const row = el("li", "flow-row" + (c.ms == null ? " running" : ""));
      row.append(el("div", "flow-when", ago(c.at)),
                 el("div", "flow-tool", c.tool),
                 el("div", "flow-detail", c.detail || "(no detail)"));
      const bar = el("div", "flow-bar");
      const fill = el("div", "flow-fill");
      fill.style.width = c.ms == null ? "100%" : Math.max(4, Math.round((c.ms / maxMs) * 100)) + "%";
      bar.append(fill);
      row.append(bar, el("div", "flow-ms", c.ms == null ? "running" : (c.ms / 1000).toFixed(1) + "s"));
      return row;
    });
    const list = el("ul", "flow-list");
    list.replaceChildren(...(rows.length ? rows : [el("li", "flow-empty", "no calls on this line in the last 6 hours")]));

    panel.replaceChildren(head, renderLanes(edge, data.agent || []), list);
  }

  // Lanes: one row per thing this agent touched in the last 30 minutes, calls as blocks on a shared time axis.
  function renderLanes(edge, agentEvents) {
    const now = Date.now() / 1000;
    const t0 = now - LANE_WINDOW_S;
    const recent = agentEvents.filter(c => (c.ms == null ? now : c.at + c.ms / 1000) >= t0);
    const lanes = new Map();
    for (const c of recent) {
      if (!lanes.has(c.dst)) lanes.set(c.dst, { dst: c.dst, kind: c.kind, latest: 0, calls: [] });
      const lane = lanes.get(c.dst);
      lane.calls.push(c);
      lane.latest = Math.max(lane.latest, c.at);
    }
    const ordered = [...lanes.values()].sort((a, b) => b.latest - a.latest).slice(0, LANES_MAX);

    const fold = foldWindow(`Lanes · last ${LANE_WINDOW_S / 60} min · ${ordered.length} in use`, "lanes", lanesOpen,
      open => { lanesOpen = open; });
    if (!ordered.length) {
      fold.body.append(el("div", "flow-empty", "nothing called in the last 30 minutes"));
      return fold.win;
    }
    const span = LANE_WINDOW_S;
    for (const lane of ordered) {
      const row = el("div", "lane" + (lane.dst === edge.dst ? " lane-on" : ""));
      row.append(el("div", "lane-name", labelOf(lane.dst)));
      const track = el("div", "lane-track");
      for (const c of lane.calls) {
        const start = Math.max(0, Math.min(1, (c.at - t0) / span));
        const end = Math.max(start, Math.min(1, ((c.ms == null ? now : c.at + c.ms / 1000) - t0) / span));
        const block = el("div", "lane-block" + (c.ms == null ? " running" : ""));
        block.style.left = (start * 100).toFixed(2) + "%";
        block.style.width = Math.max(0.6, (end - start) * 100).toFixed(2) + "%";
        block.style.background = colors["edge_" + c.kind] || colors.accent;
        block.setAttribute("title", `${c.tool}${c.detail ? " · " + c.detail : ""}`);
        track.append(block);
      }
      row.append(track);
      fold.body.append(row);
    }
    const axis = el("div", "lane-axis");
    axis.append(el("span", "", "−30m"), el("span", "", "−15m"), el("span", "", "now"));
    fold.body.append(axis);
    return fold.win;
  }

  // ---- Waits window: how much of each agent's time went to waiting, and one suggestion.
  async function pollWaits() {
    if (!active || !waitsPanel) return;
    try {
      const res = await fetch("/api/waits", { cache: "no-store" });
      if (res.ok) renderWaits(await res.json());
    } catch (err) {
      /* server down: keep the last list */
    }
  }

  function renderWaits(data) {
    const jobs = (data.jobs || []).filter(j => j.waiting > 0 || j.working > 0).slice(0, 6);
    waitsPanel.title.textContent =`Waits · ${jobs.length} agent${jobs.length === 1 ? "" : "s"}`;
    const rows = jobs.map(j => {
      const row = el("div", "wait-row");
      row.append(el("div", "wait-name", j.name));
      const bar = el("div", "wait-bar");
      const fill = el("div", "wait-fill");
      fill.style.width = Math.round(j.share * 100) + "%";
      bar.append(fill);
      row.append(bar);
      row.append(el("div", "wait-stat", `${Math.round(j.share * 100)}% waiting · ${fmtDur(j.waiting)} waiting · ${fmtDur(j.working)} working`));
      if (j.longest) row.append(el("div", "wait-longest", `longest wait: ${j.longest.state === "blocked" ? "needs you" : "idle"} ${fmtDur(j.longest.seconds)}`));
      if (j.suggestion) row.append(el("div", "wait-tip", j.suggestion));
      row.addEventListener("click", () => onOpen(j.jobId));
      return row;
    });
    waitsPanel.body.replaceChildren(...(rows.length ? rows : [el("div", "flow-empty", "no agent history yet")]));
  }

  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && (selectedKey || selectedAgent || selectedItems.size)) closeFlow();
  });

  window.PlaneView = {
    // Run the simulation by hand (a hidden tab gets no animation frames), then draw once.
    _settle(frames) {
      let now = performance.now();
      for (let i = 0; i < frames; i++) step(now += 16);
      draw(now);
    },
    init(options) {
      onOpen = options.onOpen || onOpen;
      onWatch = options.onWatch || onWatch;
      onStats = options.onStats || onStats;
      panel = el("aside", "");
      panel.id = "flow-panel";
      panel.hidden = true;
      wrap.append(panel);
      waitsPanel = foldWindow("Waits", "waits", waitsOpen, open => { waitsOpen = open; });
      waitsPanel.win.id = "waits-panel";
      wrap.append(waitsPanel.win);
      closedPanel = foldWindow("Closed", "closed", closedOpen, open => {
        closedOpen = open;
        try { localStorage.setItem("plane-closed-open", open ? "1" : "0"); } catch (e) { /* storage blocked */ }
      });
      closedPanel.win.id = "closed-panel";
      closedPanel.win.hidden = true;
      wrap.append(closedPanel.win);
      readColors();
      resize();
      window.addEventListener("resize", resize);
      new MutationObserver(() => readColors()).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
      setInterval(poll, POLL_MS);
      setInterval(pollWaits, WAITS_MS);
      requestAnimationFrame(loop);
    },
    setActive(on) {
      active = on;
      if (on) { resize(); poll(); pollWaits(); }
    },
  };
})();
