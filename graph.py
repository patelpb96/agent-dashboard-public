"""Agent graph for the plane view: which agents are connected to what, right now.

Read from the same transcripts as metrics.py (~/.claude/projects/*/*.jsonl and
*/subagents/*.jsonl). Every tool_use is an edge from the agent that made it to
the thing it touched: a web tool, a document, an MCP tool or a plain tool.
Agent spawns become spawn edges, SendMessage becomes a message edge. A Read of
a file that some agent wrote in the window links to that document, so hand-offs
between agents show up; other Reads stay on the shared Read tool node.

Every non-agent node carries a `group` and `groupLabel` so the plane can clump
similar things together on its ring: file tools, shell, one MCP server, the
documents of one folder, and so on.

Rosters (ROSTER_DIR/*.json) let an orchestrator give its sessions human names,
roles, models and a reporting line:
  {"agents": [{"sessionId", "name", "role", "model", "tier", "reportsTo",
                "provider", "domain", "environment": "local" | "cloud"}],
   "events": [{"from", "to", "kind", "at"}]}
reportsTo becomes a "reports" edge (manager -> report). An event between two
agents sets the lastAt of the edge between them (reports, or a "message" edge
for peers), so hand-offs pulse. A subagent inherits its parent's domain. Cloud
agents mark the shared files and tools they use as cloud resources in the graph.

Only transcripts changed in the last GRAPH_WINDOW_S are read, and each file is
re-parsed only when its (mtime, size) changes, like metrics.py.
"""
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from paths import CODEX_SESSIONS_DIR, PI_SESSIONS_DIR, PROJECTS_DIR, ROSTER_DIR, SHARED_DIR
GRAPH_WINDOW_S = 6 * 3600   # only transcripts touched in the last 6 h
ACTIVE_S = 120              # an unanswered call this recent is in flight
CODEX_ACTIVE_S = 6 * 60      # Codex doesn't have one process id per session; use recent turn events
CODEX_TAIL_BYTES = 4 * 1024 * 1024
PI_ACTIVE_S = 6 * 60         # Pi session JSONL is updated as messages and tool results are persisted
PI_TAIL_BYTES = 4 * 1024 * 1024
PULSE_S = 6                 # a call this recent makes its edge pulse
REFRESH_S = 3
FLOW_EVENTS_TOTAL = 4000    # newest call events kept across all transcripts
EVENTS_PER_FILE = 300       # newest call events kept per transcript for the flow view

WEB_TOOLS = {"WebSearch", "WebFetch"}
DOC_TOOLS = {"Write", "Edit", "NotebookEdit"}
SPAWN_TOOLS = {"Agent", "Task"}
READ_TOOLS = {"Read"}
TOOL_GROUPS = {
    "files": ({"Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep", "LS",
                "read_file", "write_file", "apply_patch", "list_dir", "grep_files", "glob_files",
                "read", "write", "edit", "find", "grep", "ls"}, "files"),
    "shell": ({"Bash", "PowerShell", "BashOutput", "KillShell", "Monitor", "exec",
                "exec_command", "shell", "powershell", "bash", "run_command", "write_stdin"}, "shell"),
    "agents": ({"Agent", "Task", "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "TaskStop",
                "TaskOutput", "ListAgents", "spawn_agent", "send_message", "followup_task",
                "list_agents", "interrupt_agent"}, "agents"),
    "plan": ({"TodoWrite", "EnterPlanMode", "ExitPlanMode", "AskUserQuestion", "Skill",
              "ToolSearch", "ScheduleWakeup", "request_user_input", "create_goal", "update_goal",
              "get_goal", "wait"}, "planning"),
}
CODEX_WEB_TOOLS = {"web_search", "web_fetch", "web__run", "browser", "browser_search", "browser_open",
                   "open", "click", "image_query", "finance", "weather", "sports"}


def _ts(value):
    """ISO timestamp from a transcript line, as epoch seconds, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _environment(meta):
    """Normalize optional runtime-location metadata without guessing from a path."""
    if not isinstance(meta, dict):
        return "local"
    if meta.get("cloud") is True:
        return "cloud"
    value = meta.get("environment") or meta.get("execution") or meta.get("location")
    if isinstance(value, str) and value.casefold() in {"cloud", "remote", "hosted", "remote-cloud"}:
        return "cloud"
    return "local"


def _canonical_domain(value):
    value = str(value or "other").strip().casefold()
    compact = re.sub(r"[\s_-]+", "-", value)
    if compact in {"claude", "claude-code", "anthropic"}:
        return "claude"
    if compact in {"codex", "codex-cli", "openai-codex"}:
        return "codex"
    return compact


def agent_key(path):
    """(node id, parent id) for a transcript: the session, or a subagent of it."""
    if path.parent.name == "subagents":
        return f"sub:{path.stem}", path.parent.parent.name
    return path.stem, None


def doc_id(path):
    return "doc:" + _path_key(path).replace("\\", "/")


def _path_key(path, cwd=None):
    """Canonical path key so both providers land on one node for the same local file."""
    raw = os.path.expandvars(os.path.expanduser(str(path or "").strip()))
    if not raw:
        return "document"
    candidate = Path(raw)
    if not candidate.is_absolute() and cwd:
        candidate = Path(cwd) / candidate
    try:
        normalized = str(candidate.resolve(strict=False))
    except OSError:
        normalized = str(candidate)
    return os.path.normcase(normalized)


def _shared_target(path=None, cwd=None, hint=""):
    """One shared area node for every path into SHARED_DIR, even across providers. Off when unset."""
    if not SHARED_DIR:
        return None
    root = Path(SHARED_DIR).resolve(strict=False)
    name = root.name or "shared"
    root_key = os.path.normcase(str(root)).rstrip("\\/")
    candidate = None
    if path:
        raw = os.path.expandvars(os.path.expanduser(str(path).strip()))
        candidate = Path(raw)
        if not candidate.is_absolute() and cwd:
            candidate = Path(cwd) / candidate
        try:
            candidate_key = os.path.normcase(str(candidate.resolve(strict=False)))
        except OSError:
            candidate_key = os.path.normcase(str(candidate))
        if candidate_key == root_key or candidate_key.startswith(root_key + os.sep):
            return {
                "id": "area:shared", "kind": "area", "label": name,
                "group": "shared:workflow", "groupLabel": "shared workflow", "space": "shared",
                "detail": _shared_relative(candidate, root),
            }
    if hint:
        text = str(hint).replace("\\\\", "\\").replace("/", "\\").casefold()
        roots = [str(root).replace("/", "\\").casefold()]
        try:
            under_home = str(root.relative_to(Path.home().resolve(strict=False))).replace("/", "\\").casefold()
            roots += ["~\\" + under_home, "$home\\" + under_home]
        except ValueError:
            pass
        for known in roots:
            at = text.find(known)
            if at < 0:
                continue
            remainder = text[at + len(known):]
            if remainder and remainder[0] not in "\\ \t\r\n\"'`),;}":
                continue
            tail = remainder.lstrip("\\ \t\r\n\"'`)")
            tail = re.split(r"[\"'\s,;)}\]]", tail, maxsplit=1)[0]
            return {
                "id": "area:shared", "kind": "area", "label": name,
                "group": "shared:workflow", "groupLabel": "shared workflow", "space": "shared",
                "detail": name + ("/" + tail.replace("\\", "/") if tail else ""),
            }
    return None


def _shared_relative(path, root):
    name = root.name or "shared"
    try:
        relative = path.resolve(strict=False).relative_to(root)
        return name + ("/" + relative.as_posix() if str(relative) != "." else "")
    except (OSError, ValueError):
        return name


def document_target(path, cwd=None):
    """Build a normalized file node, or the shared area for a file in SHARED_DIR."""
    shared = _shared_target(path, cwd)
    if shared:
        return shared
    normalized = _path_key(path, cwd)
    p = Path(normalized)
    parent = p.parent
    group = "doc:" + os.path.normcase(str(parent)).replace("\\", "/")
    return {"id": "doc:" + normalized.replace("\\", "/"), "kind": "doc",
            "label": p.name or normalized, "group": group,
            "groupLabel": parent.name or parent.as_posix()}


def path_target_from_input(inp, cwd=None):
    inp = inp if isinstance(inp, dict) else {}
    for key in ("file_path", "notebook_path", "path", "filename", "file"):
        value = inp.get(key)
        if isinstance(value, str) and value:
            return document_target(value, cwd)
    return None


def shared_target_from_input(inp, cwd=None):
    if isinstance(inp, dict):
        text = " ".join(str(v) for v in inp.values() if isinstance(v, str))
    elif isinstance(inp, str):
        text = inp
    else:
        text = ""
    return _shared_target(cwd=cwd, hint=text)


def shared_type_target(name):
    """A common category node connects provider-specific calls of the same tool type."""
    name = str(name or "")
    if name.startswith("mcp__"):
        key, label = "mcp", "MCP tools"
    elif name in WEB_TOOLS or name.casefold() in CODEX_WEB_TOOLS:
        key, label = "web", "web"
    else:
        key, label = tool_group(name)
        if key == "tool:other":
            return None
        key = key[5:] if key.startswith("tool:") else key
        label = {"files": "file tools", "shell": "shell", "agents": "agent tools", "plan": "planning"}.get(key, label)
    return {"id": "shared:tool:" + key, "kind": "tool", "label": label,
            "group": "shared:tools", "groupLabel": "shared tools", "space": "shared"}


def _pi_subagent_tool(name, inp=None):
    folded = re.sub(r"[^a-z0-9]+", "_", str(name or "").casefold())
    if any(token in folded for token in ("subagent", "spawn_agent", "spawnagent", "delegate", "agent_task", "task_agent", "start_agent", "run_agent")):
        return True
    # Parallel/chain dispatches also carry an unambiguous list of agent/task pairs.
    has_agent_jobs = isinstance(inp, dict) and any(
        isinstance(entry, dict) and any(isinstance(entry.get(field), str) and entry[field].strip()
                                        for field in ("agent", "task", "prompt"))
        for key in ("tasks", "chain") if isinstance(inp.get(key), list) for entry in inp[key]
    )
    # Some Pi/MCP agent dispatch tools are simply named `agent`/`agents` and
    # identify themselves through their task-shaped arguments.
    has_task = isinstance(inp, dict) and any(isinstance(inp.get(key), str) and inp[key].strip()
                                               for key in ("task", "prompt", "description"))
    return has_agent_jobs or ("agent" in folded and has_task)


def _pi_subagent_jobs(name, inp):
    """Task descriptors for child agents represented by one Pi tool call."""
    if not _pi_subagent_tool(name, inp) or not isinstance(inp, dict):
        return "single", []
    for key, mode in (("tasks", "parallel"), ("chain", "chain")):
        entries = inp.get(key)
        if isinstance(entries, list):
            jobs = [entry for entry in entries if isinstance(entry, dict) and any(
                isinstance(entry.get(field), str) and entry[field].strip()
                for field in ("agent", "task", "prompt", "description")
            )]
            return mode, jobs
    if (isinstance(inp.get("agent"), str) and inp["agent"].strip() and
            isinstance(inp.get("task"), str) and inp["task"].strip()):
        return "single", [inp]
    if any(isinstance(inp.get(field), str) and inp[field].strip()
           for field in ("task", "prompt", "description")):
        return "single", [inp]
    return "single", []


def _pi_subagent_label(inp, call_id):
    if isinstance(inp, dict):
        agent = inp.get("agent")
        task = inp.get("task") or inp.get("prompt") or inp.get("description")
        if isinstance(agent, str) and agent.strip():
            label = agent.strip()
            if isinstance(task, str) and task.strip():
                label += " · " + task.strip().split("\n", 1)[0]
            return label[:100]
        for key in ("name", "title", "description", "task", "prompt"):
            value = inp.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().split("\n", 1)[0][:100]
    return "Pi subagent " + str(call_id or "run")[-8:]


def doc_group(path):
    """(group, label) for a document: the folder it lives in."""
    parent = Path(_path_key(path).replace("\\", "/")).parent
    return "doc:" + os.path.normcase(parent.as_posix()), parent.name or parent.as_posix()


def tool_group(name):
    for key, (names, label) in TOOL_GROUPS.items():
        if name in names:
            return "tool:" + key, label
    return "tool:other", "tools"


def target_of(name, inp):
    """What one tool call touches: {id, kind, label, group, groupLabel, edge}."""
    inp = inp if isinstance(inp, dict) else {}
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        server = parts[1] if len(parts) == 3 else "mcp"
        tool = parts[2] if len(parts) == 3 else name
        return {"id": f"mcp:{server}:{tool}", "kind": "tool", "label": tool,
                "group": f"mcp:{server}", "groupLabel": server.replace("claude_ai_", ""), "edge": "call"}
    if name in WEB_TOOLS or name.casefold() in CODEX_WEB_TOOLS:
        return {"id": f"web:{name}", "kind": "web", "label": name, "group": "web",
                "groupLabel": "web", "edge": "call"}
    if name in DOC_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path") or "document"
        return {**document_target(path), "edge": "call"}
    if name == "SendMessage":
        to = inp.get("to") or inp.get("recipient") or "agent"
        return {"id": f"msg:{to}", "kind": "msg", "label": str(to), "group": "msg",
                "groupLabel": "messages", "edge": "message"}
    group, glabel = tool_group(name)
    return {"id": f"tool:{name}", "kind": "tool", "label": name, "group": group,
            "groupLabel": glabel, "edge": "call"}


def detail_of(inp):
    """The one input field worth showing in a flow row: a command, path, query or URL."""
    inp = inp if isinstance(inp, dict) else {}
    for key in ("command", "cmd", "command_line", "file_path", "notebook_path", "path",
                "filename", "query", "url", "pattern", "to", "description"):
        value = inp.get(key)
        if isinstance(value, str) and value:
            return value.split("\n")[0][:160]
    return ""


def subagent_label(path):
    """A subagent's description from its .meta.json, else its file name."""
    try:
        data = json.loads(path.with_name(path.stem + ".meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return path.stem
    return data.get("description") or data.get("agentType") or path.stem


def parse_file(path):
    """Aggregate one Claude transcript into nodes, edges, and resource call events."""
    agent, parent = agent_key(path)
    provider = "claude"
    nodes = {}
    edges = {}          # (src, dst, kind) -> {count, lastAt}
    open_calls = {}     # tool_use id -> call event still waiting for its result
    events = []         # newest call events, for the flow view
    busy = False        # the newest turn still waits on a tool or on the model (no end_turn after it)
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"end_turn"' in line:
                    try:
                        if (json.loads(line).get("message") or {}).get("stop_reason") == "end_turn":
                            busy = False
                    except ValueError:
                        pass
                if '"tool_use"' not in line and '"tool_result"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                at = _ts(entry.get("timestamp")) or 0.0
                content = (entry.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") in ("tool_use", "tool_result"):
                        busy = True
                    if part.get("type") == "tool_result":
                        calls = open_calls.pop(part.get("tool_use_id"), None) or []
                        if calls and at:
                            for call in calls:
                                if call["at"]:
                                    call["ms"] = int((at - call["at"]) * 1000)
                    elif part.get("type") == "tool_use":
                        name, inp = part.get("name") or "", part.get("input")
                        t = target_of(name, inp)
                        path_target = path_target_from_input(inp)
                        targets = [t, shared_type_target(name), path_target,
                                   shared_target_from_input(inp)]
                        unique = {}
                        for target in targets:
                            if not target:
                                continue
                            target.setdefault("space", "shared" if target.get("space") == "shared" else provider)
                            unique[target["id"]] = target
                        calls = []
                        for target in unique.values():
                            nodes[target["id"]] = {k: target[k] for k in ("kind", "label", "group", "groupLabel", "space")}
                            key = (agent, target["id"], target.get("edge", "call"))
                            edge = edges.setdefault(key, {"count": 0, "lastAt": 0.0})
                            edge["count"] += 1
                            edge["lastAt"] = max(edge["lastAt"], at)
                            call = {"at": at, "src": agent, "dst": target["id"], "kind": target.get("edge", "call"),
                                    "tool": name, "detail": detail_of(inp) or target.get("detail", ""), "ms": None}
                            calls.append(call)
                        primary = next((call for call in calls if call["dst"] == t["id"]), None)
                        if primary:
                            primary["targets"] = [call["dst"] for call in calls]
                            events.append(primary)
                        if part.get("id"):
                            open_calls[part["id"]] = calls
                    # tool_result parts come in user entries; nothing else to read
        mtime = path.stat().st_mtime
        if parent:
            nodes[agent] = {"kind": "subagent", "label": subagent_label(path), "parent": parent, "mtime": mtime,
                            "busy": busy, "provider": provider, "domain": provider, "space": provider, "environment": "local"}
            edges[(parent, agent, "spawn")] = {"count": 1, "lastAt": path.stat().st_mtime}
        else:
            nodes[agent] = {"kind": "agent", "label": path.stem, "mtime": mtime,
                            "provider": provider, "domain": provider, "space": provider, "environment": "local"}
    except OSError:
        return None
    return {"agent": agent, "provider": provider, "nodes": nodes, "edges": edges, "open": open_calls,
            "events": events[-EVENTS_PER_FILE:]}


def _codex_meta(path):
    """Read Codex's first session_meta record without loading the full rollout."""
    try:
        with path.open("rb") as f:
            entry = json.loads(f.readline().decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    if entry.get("type") != "session_meta":
        return {}
    return entry.get("payload") or {}


def _codex_args(payload):
    raw = payload.get("input")
    if raw is None:
        raw = payload.get("arguments")
    if isinstance(raw, dict):
        return raw, ""
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                return decoded, raw
        except ValueError:
            pass
        return {}, raw
    return {}, ""


def parse_codex_file(path, now=None):
    """Parse a recent Codex rollout into the same agent/resource graph as Claude."""
    now = now or time.time()
    meta = _codex_meta(path)
    session_id = meta.get("id") or meta.get("session_id") or path.stem
    agent = "codex:" + str(session_id)
    cwd = meta.get("cwd") or ""
    project = Path(cwd).name if cwd else "Codex session"
    nodes, edges, open_calls, events = {}, {}, {}, []
    latest_started = 0.0
    latest_finished = 0.0
    latest_activity = 0.0
    model = None

    def add_call(name, inp, raw_text, at, call_id):
        if not name:
            return
        primary = target_of(name, inp)
        targets = [primary, shared_type_target(name), path_target_from_input(inp, cwd)]
        shared = shared_target_from_input(inp, cwd)
        if not shared:
            shared = _shared_target(cwd=cwd, hint=raw_text)
        targets.append(shared)
        unique = {}
        for target in targets:
            if not target:
                continue
            target.setdefault("space", "shared" if target.get("space") == "shared" else "codex")
            unique[target["id"]] = target
        calls = []
        detail = detail_of(inp) or (raw_text.split("\n")[0][:160] if raw_text else "")
        for target in unique.values():
            nodes[target["id"]] = {k: target[k] for k in ("kind", "label", "group", "groupLabel", "space")}
            key = (agent, target["id"], target.get("edge", "call"))
            edge = edges.setdefault(key, {"count": 0, "lastAt": 0.0})
            edge["count"] += 1
            edge["lastAt"] = max(edge["lastAt"], at)
            call = {"at": at, "src": agent, "dst": target["id"], "kind": target.get("edge", "call"),
                    "tool": name, "detail": detail or target.get("detail", ""), "ms": None}
            events.append(call)
            calls.append(call)
        if call_id and calls:
            open_calls[call_id] = calls

    def finish_calls(at):
        for calls in open_calls.values():
            for call in calls:
                if call["ms"] is None and at and call["at"]:
                    call["ms"] = max(0, int((at - call["at"]) * 1000))
        open_calls.clear()

    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size > CODEX_TAIL_BYTES:
                f.seek(size - CODEX_TAIL_BYTES)
                f.readline()  # discard the partial first record
            else:
                f.seek(0)
            for raw_line in f:
                try:
                    entry = json.loads(raw_line.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                at = _ts(entry.get("timestamp")) or 0.0
                if at:
                    latest_activity = max(latest_activity, at)
                kind = entry.get("type")
                payload = entry.get("payload") or {}

                if kind == "turn_context":
                    model = payload.get("model") or model
                elif kind == "event_msg":
                    event_type = payload.get("type")
                    event_at = _ts(payload.get("started_at") if event_type == "task_started" else payload.get("completed_at")) or at
                    if event_type == "task_started":
                        if latest_started > latest_finished:
                            finish_calls(event_at)
                        latest_started = max(latest_started, event_at)
                    elif event_type in ("task_complete", "turn_complete", "turn_aborted"):
                        latest_finished = max(latest_finished, event_at)
                        finish_calls(event_at)
                elif kind == "response_item":
                    item_type = payload.get("type")
                    if item_type in ("custom_tool_call", "function_call", "tool_use"):
                        args, raw_text = _codex_args(payload)
                        add_call(payload.get("name") or payload.get("tool_name") or "",
                                 args, raw_text, at, payload.get("call_id") or payload.get("id"))
                    elif item_type in ("custom_tool_call_output", "function_call_output", "tool_result"):
                        calls = open_calls.pop(payload.get("call_id") or payload.get("tool_call_id"), None) or []
                        for call in calls:
                            if call["at"] and at:
                                call["ms"] = max(0, int((at - call["at"]) * 1000))
                elif kind in ("custom_tool_call", "function_call", "tool_use"):
                    args, raw_text = _codex_args(entry)
                    add_call(entry.get("name") or entry.get("tool_name") or "", args, raw_text,
                             at, entry.get("call_id") or entry.get("id"))
                elif kind in ("custom_tool_call_output", "function_call_output", "tool_result"):
                    calls = open_calls.pop(entry.get("call_id") or entry.get("tool_call_id"), None) or []
                    for call in calls:
                        if call["at"] and at:
                            call["ms"] = max(0, int((at - call["at"]) * 1000))
        mtime = path.stat().st_mtime
        latest_activity = max(latest_activity, mtime)
    except OSError:
        return None

    active = latest_started > latest_finished and now - max(latest_activity, latest_started) <= CODEX_ACTIVE_S
    if open_calls and now - latest_activity <= ACTIVE_S:
        active = True
    label = f"Codex · {project} · {str(session_id)[-6:]}"
    nodes[agent] = {"kind": "agent", "label": label, "name": label, "provider": "codex", "domain": "codex",
                    "space": "codex", "environment": _environment(meta), "state": "working" if active else "done", "model": model,
                    "cwd": cwd, "mtime": mtime, "open": active}
    return {"agent": agent, "provider": "codex", "nodes": nodes, "edges": edges,
            "open": open_calls, "events": events[-EVENTS_PER_FILE:]}


def _pi_meta(path):
    """Read Pi's first session record without loading the full JSONL transcript."""
    try:
        with path.open("rb") as f:
            entry = json.loads(f.readline().decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    return entry if isinstance(entry, dict) and entry.get("type") == "session" else {}


def parse_pi_file(path, now=None):
    """Parse a Pi JSONL session into the shared agent/resource graph."""
    now = now or time.time()
    meta = _pi_meta(path)
    session_id = meta.get("id") or path.stem
    cwd = meta.get("cwd") or ""
    project = Path(cwd).name if cwd else "Pi session"
    agent = "pi:" + str(session_id)
    parent_session = meta.get("parentSession")
    parent_meta = _pi_meta(Path(parent_session)) if isinstance(parent_session, str) and parent_session else {}
    if parent_session and not parent_meta:
        parent_stem = Path(parent_session).stem
        parent_meta = {"id": parent_stem.rsplit("_", 1)[-1], "cwd": cwd}
    parent_id = "pi:" + str(parent_meta.get("id")) if parent_session and parent_meta.get("id") else None
    if parent_id == agent:
        parent_id = None
    environment = _environment(meta)
    nodes, edges, open_calls, events = {}, {}, {}, []
    spawn_calls = {}
    latest_activity = 0.0
    latest_role = None
    latest_stop = None
    model = None

    def add_call(name, inp, at, call_id):
        if not name:
            return
        primary = target_of(name, inp)
        targets = [primary, shared_type_target(name), path_target_from_input(inp, cwd),
                   shared_target_from_input(inp, cwd)]
        unique = {}
        for target in targets:
            if not target:
                continue
            target.setdefault("space", "shared" if target.get("space") == "shared" else "pi")
            unique[target["id"]] = target
        calls = []
        detail = detail_of(inp)
        for target in unique.values():
            nodes[target["id"]] = {k: target[k] for k in ("kind", "label", "group", "groupLabel", "space")}
            key = (agent, target["id"], target.get("edge", "call"))
            edge = edges.setdefault(key, {"count": 0, "lastAt": 0.0})
            edge["count"] += 1
            edge["lastAt"] = max(edge["lastAt"], at)
            call = {"at": at, "src": agent, "dst": target["id"], "kind": target.get("edge", "call"),
                    "tool": name, "detail": detail or target.get("detail", ""), "ms": None}
            events.append(call)
            calls.append(call)
        if call_id and calls:
            open_calls[call_id] = calls
        mode, jobs = _pi_subagent_jobs(name, inp)
        if jobs:
            child_ids = []
            call_key = str(call_id or f"{at}:{_pi_subagent_label(inp, '')}")
            for index, job in enumerate(jobs):
                child_id = f"pi:subagent:{session_id}:{call_key}:{index + 1}"
                label = _pi_subagent_label(job, f"{call_id or 'run'}:{index + 1}")
                nodes[child_id] = {"kind": "subagent", "label": label, "name": label,
                                   "parent": agent, "provider": "pi", "domain": "pi", "space": "pi",
                                   "environment": environment,
                                   "model": job.get("model") if isinstance(job, dict) else None,
                                   "role": "Subagent", "dispatchMode": mode, "dispatchIndex": index + 1,
                                   "state": "working", "mtime": at, "open": True, "synthetic": True}
                edges[(agent, child_id, "spawn")] = {"count": 1, "lastAt": at}
                child_ids.append(child_id)
            if call_id:
                spawn_calls[call_id] = child_ids

    def finish_call(call_id, at, duration=None):
        calls = open_calls.pop(call_id, None) or []
        for call in calls:
            if duration is not None:
                call["ms"] = max(0, int(duration))
            elif call["at"] and at:
                call["ms"] = max(0, int((at - call["at"]) * 1000))
        child_ids = spawn_calls.pop(call_id, ())
        if isinstance(child_ids, str):  # tolerate cached records created by older parser code
            child_ids = (child_ids,)
        for child_id in child_ids:
            if child_id in nodes:
                nodes[child_id].update({"state": "done", "mtime": at or nodes[child_id].get("mtime"), "open": False})

    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size > PI_TAIL_BYTES:
                f.seek(size - PI_TAIL_BYTES)
                f.readline()  # discard a partial first record
            else:
                f.seek(0)
            for raw_line in f:
                try:
                    entry = json.loads(raw_line.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                at = _ts(entry.get("timestamp")) or 0.0
                if at:
                    latest_activity = max(latest_activity, at)
                kind = entry.get("type")
                if kind == "model_change":
                    model = entry.get("modelId") or model
                    continue
                if kind != "message":
                    continue

                message = entry.get("message") or {}
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                latest_role = role or latest_role
                if role == "assistant":
                    model = message.get("model") or model
                    latest_stop = message.get("stopReason") or latest_stop
                    content = message.get("content") or []
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict) or block.get("type") != "toolCall":
                                continue
                            args = block.get("arguments") or block.get("args") or {}
                            if not isinstance(args, dict):
                                args = {}
                            add_call(block.get("name") or "", args, at, block.get("id") or block.get("toolCallId"))
                elif role == "toolResult":
                    finish_call(message.get("toolCallId"), at, message.get("durationMs"))

        mtime = path.stat().st_mtime
        latest_activity = max(latest_activity, mtime)
    except OSError:
        return None

    active = now - latest_activity <= PI_ACTIVE_S
    working = active and (bool(open_calls) or latest_role == "toolResult" or latest_stop in ("pending", "toolUse"))
    label = f"Pi · {project} · {str(session_id)[-6:]}"
    if parent_id:
        parent_path = Path(parent_session)
        try:
            parent_mtime = parent_path.stat().st_mtime
        except OSError:
            parent_mtime = 0.0
        parent_cwd = parent_meta.get("cwd") or cwd
        parent_project = Path(parent_cwd).name if parent_cwd else "Pi session"
        parent_label = f"Pi · {parent_project} · {str(parent_meta.get('id'))[-6:]}"
        nodes.setdefault(parent_id, {"kind": "agent", "label": parent_label, "name": parent_label, "provider": "pi",
                                     "domain": "pi", "space": "pi", "environment": _environment(parent_meta),
                                     "cwd": parent_cwd, "mtime": parent_mtime,
                                     "open": bool(parent_mtime and now - parent_mtime <= PI_ACTIVE_S), "_parentStub": True})
        edges[(parent_id, agent, "spawn")] = {"count": 1, "lastAt": mtime}
    nodes[agent] = {"kind": "subagent" if parent_id else "agent", "label": label, "name": label,
                    "parent": parent_id, "provider": "pi", "domain": "pi", "space": "pi",
                    "environment": environment, "state": "working" if working else "idle" if active else "done",
                    "model": model, "cwd": cwd, "mtime": mtime, "open": active}
    return {"agent": agent, "provider": "pi", "nodes": nodes, "edges": edges,
            "open": open_calls, "events": events[-EVENTS_PER_FILE:]}


def load_rosters(now, directory=None):
    """Roster files touched within the graph window, newest last."""
    directory = directory or ROSTER_DIR
    out = []
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            if now - path.stat().st_mtime > GRAPH_WINDOW_S:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data["_mtime"] = path.stat().st_mtime
            out.append(data)
    return out


ROSTER_LIVE_S = 15 * 60   # a roster run that hasn't been touched in this long has ended (or crashed)


def merge_alias(nodes, edges, alias, main) -> None:
    """Fold one session's node and lines into another (an agent's extra one-shot sessions)."""
    nodes.pop(alias, None)
    for (src, dst, kind) in [k for k in edges if alias in k[:2]]:
        e = edges.pop((src, dst, kind))
        key = (main if src == alias else src, main if dst == alias else dst, kind)
        cur = edges.setdefault(key, {"count": 0, "lastAt": 0.0, "active": False})
        cur["count"] += e.get("count", 0)
        cur["lastAt"] = max(cur["lastAt"], e.get("lastAt", 0.0))
        cur["active"] = cur["active"] or e.get("active", False)


def apply_rosters(nodes, edges, rosters, now=None):
    """Name agent nodes and add reporting edges from orchestrator rosters (in place)."""
    for roster in rosters:
        agents = [a for a in roster.get("agents") or [] if isinstance(a, dict) and a.get("sessionId")]
        for a in agents:
            for alias in a.get("sessions") or []:
                if alias != a["sessionId"]:
                    merge_alias(nodes, edges, alias, a["sessionId"])
        manager_of = {}
        for a in agents:
            sid = a["sessionId"]
            provider = a.get("provider") or "claude"
            domain = _canonical_domain(a.get("domain") or provider)
            node = nodes.setdefault(sid, {"kind": "agent", "label": sid, "provider": provider,
                                          "domain": domain, "space": domain, "environment": "local"})
            node.setdefault("provider", provider)
            node.setdefault("domain", domain)
            node.setdefault("space", domain)
            for field in ("name", "role", "model", "tier", "team", "state", "provider", "domain", "environment"):
                if a.get(field) is not None:
                    node[field] = a[field]
            if a.get("cloud") is True or a.get("environment") is not None:
                node["environment"] = _environment(a)
            if a.get("name"):
                node["label"] = a["name"]
            # Orchestrated agents (claude -p) have no session file; they're open while their run is.
            node["rosterLive"] = a.get("state") != "done" and \
                (now or 0) - roster.get("_mtime", now or 0) <= ROSTER_LIVE_S
            boss = a.get("reportsTo")
            if boss:
                manager_of[sid] = boss
                edges.setdefault((boss, sid, "reports"), {"count": 0, "lastAt": 0.0, "active": False})
        for ev in roster.get("events") or []:
            src, dst, at = ev.get("from"), ev.get("to"), ev.get("at") or 0.0
            if not src or not dst:
                continue
            if manager_of.get(dst) == src:
                key = (src, dst, "reports")
            elif manager_of.get(src) == dst:
                key = (dst, src, "reports")
            else:
                key = (src, dst, "message")
            e = edges.setdefault(key, {"count": 0, "lastAt": 0.0, "active": False})
            e["count"] += 1
            e["lastAt"] = max(e["lastAt"], at)
            if ev.get("active"):
                e["active"] = True


class GraphStore:
    def __init__(self):
        self._files = {}   # path -> ((mtime, size), parsed)
        self._lock = threading.Lock()
        self._snapshot = {"nodes": {}, "edges": [], "generatedAt": None}
        self._events = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                self.refresh()
            except Exception:
                pass  # keep the last good snapshot
            time.sleep(REFRESH_S)

    def refresh(self):
        now = time.time()
        seen = set()
        if PROJECTS_DIR.is_dir():
            for path in list(PROJECTS_DIR.glob("*/*.jsonl")) + list(PROJECTS_DIR.glob("*/*/subagents/*.jsonl")):
                seen.add(path)
                try:
                    st = path.stat()
                except OSError:
                    continue
                if now - st.st_mtime > GRAPH_WINDOW_S:
                    self._files.pop(path, None)
                    continue
                key = (st.st_mtime, st.st_size)
                cached = self._files.get(path)
                if cached and cached[0] == key:
                    continue
                parsed = parse_file(path)
                if parsed is not None:
                    self._files[path] = (key, parsed)
        if CODEX_SESSIONS_DIR.is_dir():
            for path in CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"):
                seen.add(path)
                try:
                    st = path.stat()
                except OSError:
                    continue
                if now - st.st_mtime > GRAPH_WINDOW_S:
                    self._files.pop(path, None)
                    continue
                key = (st.st_mtime, st.st_size)
                cached = self._files.get(path)
                if cached and cached[0] == key:
                    continue
                parsed = parse_codex_file(path, now)
                if parsed is not None:
                    self._files[path] = (key, parsed)
        if PI_SESSIONS_DIR.is_dir():
            for path in PI_SESSIONS_DIR.rglob("*.jsonl"):
                seen.add(path)
                try:
                    st = path.stat()
                except OSError:
                    continue
                if now - st.st_mtime > GRAPH_WINDOW_S:
                    self._files.pop(path, None)
                    continue
                key = (st.st_mtime, st.st_size)
                cached = self._files.get(path)
                if cached and cached[0] == key:
                    continue
                parsed = parse_pi_file(path, now)
                if parsed is not None:
                    self._files[path] = (key, parsed)
        for path in list(self._files):
            if (PROJECTS_DIR in path.parents or CODEX_SESSIONS_DIR in path.parents or
                    PI_SESSIONS_DIR in path.parents) and path not in seen:
                self._files.pop(path, None)
        self._publish(now, load_rosters(now))

    def _publish(self, now, rosters=()):
        nodes, edges, events = {}, {}, []
        for _, parsed in self._files.values():
            events.extend(parsed.get("events", []))
            for nid, info in parsed["nodes"].items():
                existing = nodes.get(nid)
                if existing is None:
                    nodes[nid] = dict(info)
                elif not info.get("_parentStub"):
                    existing.update(info)
                    existing.pop("_parentStub", None)
                elif existing.get("_parentStub"):
                    existing.update(info)
            for (src, dst, kind), e in parsed["edges"].items():
                cur = edges.setdefault((src, dst, kind), {"count": 0, "lastAt": 0.0, "active": False})
                cur["count"] += e["count"]
                cur["lastAt"] = max(cur["lastAt"], e["lastAt"])
            for call in parsed["open"].values():
                calls = call if isinstance(call, list) else [call]
                for item in calls:
                    key = (item["src"], item["dst"], item["kind"])
                    if now - item["at"] <= ACTIVE_S and key in edges:
                        edges[key]["active"] = True
        apply_rosters(nodes, edges, rosters, now)
        for _ in range(len(nodes)):
            changed = False
            for node in nodes.values():
                if node.get("kind") != "subagent":
                    continue
                parent = nodes.get(node.get("parent")) or {}
                parent_domain = _canonical_domain(parent.get("domain") or parent.get("provider")) if parent else None
                if parent_domain and node.get("domain") != parent_domain:
                    node["domain"] = parent_domain
                    changed = True
                if parent.get("environment") == "cloud" and node.get("environment") != "cloud":
                    node["environment"] = "cloud"
                    changed = True
            if not changed:
                break
        target_domains, target_cloud_domains = {}, {}
        for (src, dst, _), edge in edges.items():
            src_info = nodes.get(src) or {}
            dst_info = nodes.get(dst) or {}
            if src_info.get("kind") not in ("agent", "subagent") or dst_info.get("kind") in ("agent", "subagent"):
                continue
            domain = _canonical_domain(src_info.get("domain") or src_info.get("provider") or "claude")
            target_domains.setdefault(dst, {}).setdefault(domain, 0)
            target_domains[dst][domain] += edge.get("count", 0)
            if src_info.get("environment") == "cloud":
                target_cloud_domains.setdefault(dst, set()).add(domain)
        for nid, weights in target_domains.items():
            if nid not in nodes:
                continue
            domains = sorted(weights)
            nodes[nid]["domains"] = domains
            nodes[nid]["domainWeights"] = weights
            nodes[nid]["space"] = "shared" if len(domains) > 1 else domains[0]
            nodes[nid]["cloudDomains"] = sorted(target_cloud_domains.get(nid, ()))
            nodes[nid]["cloud"] = bool(nodes[nid]["cloudDomains"])
        for node in nodes.values():
            node.pop("_parentStub", None)
        out = [{
            "src": src, "dst": dst, "kind": kind,
            "count": e["count"], "lastAt": e["lastAt"],
            "active": e["active"], "pulse": now - e["lastAt"] <= PULSE_S,
        } for (src, dst, kind), e in edges.items() if src in nodes and dst in nodes]
        events.sort(key=lambda c: c["at"])
        snapshot = {
            "nodes": nodes,
            "edges": out,
            "generatedAt": now,
        }
        with self._lock:
            self._snapshot = snapshot
            self._events = events[-FLOW_EVENTS_TOTAL:]

    def snapshot(self):
        with self._lock:
            return self._snapshot

    def flow(self, src, dst, kind, limit=40):
        """The newest calls along one edge, oldest first, for the flow view."""
        with self._lock:
            matching = [c for c in self._events if c["src"] == src and
                        (c["dst"] == dst or dst in c.get("targets", ())) and c["kind"] == kind]
        return matching[-limit:]

    def agent_events(self, src, limit=300):
        """Every newest call one agent made, whatever it touched, oldest first, for the lane view."""
        with self._lock:
            matching = [c for c in self._events if c["src"] == src]
        return matching[-limit:]
