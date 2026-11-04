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
import hashlib
import json
import math
import os
import re
import shlex
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path, PurePath
from urllib.parse import unquote, urlsplit

from paths import (ANTIGRAVITY_DIR, CODEX_SESSIONS_DIR, DSH_DIR, PI_SESSIONS_DIR, PROJECTS_DIR, ROSTER_DIR,
                   SHARED_DIR)
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
                "read_file", "write_file", "edit_file", "apply_patch", "list_dir", "grep_files", "glob_files",
                "read", "write", "edit", "find", "grep", "ls", "glob", "read_image"}, "files"),
    "shell": ({"Bash", "PowerShell", "BashOutput", "KillShell", "Monitor", "exec",
                "exec_command", "shell", "powershell", "bash", "run_command", "write_stdin",
                "pwsh", "job_output", "job_kill", "job_list"}, "shell"),
    "agents": ({"Agent", "Task", "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "TaskStop",
                "TaskOutput", "ListAgents", "spawn_agent", "send_message", "followup_task",
                "list_agents", "interrupt_agent", "invoke_subagent", "define_subagent",
                "manage_subagents", "manage_task", "spawn_teammate", "wait_agent", "subagent",
                "subagent_fork", "team_task_create", "team_task_update", "team_task_get", "team_task_list"}, "agents"),
    "plan": ({"TodoWrite", "EnterPlanMode", "ExitPlanMode", "AskUserQuestion", "Skill",
              "ToolSearch", "ScheduleWakeup", "request_user_input", "create_goal", "update_goal",
              "get_goal", "wait", "schedule", "todo_write", "skill"}, "planning"),
}
CODEX_WEB_TOOLS = {"web_search", "web_fetch", "web__run", "browser", "browser_search", "browser_open",
                   "open", "click", "image_query", "finance", "weather", "sports",
                   "search_web", "read_url_content"}   # the last two are Antigravity's


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
    if compact in {"codex", "codex-cli", "openai-codex", "openai", "gpt"}:   # all shown as "GPT"
        return "codex"
    if compact in {"antigravity", "antigravity-cli", "agy", "google-antigravity"}:
        return "antigravity"
    return compact


def agent_key(path):
    """(node id, parent id) for a transcript: the session, or a subagent of it."""
    if path.parent.name == "subagents":
        return f"sub:{path.stem}", path.parent.parent.name
    return path.stem, None


def doc_id(path):
    return "doc:" + _path_key(path).replace("\\", "/")


def _resolved(candidate):
    """resolve() a path, but not a UNC one (//host/share): resolving it would connect to that host."""
    text = str(candidate)
    if text.startswith(("\\\\", "//")):
        return os.path.normpath(text)
    try:
        return str(candidate.resolve(strict=False))
    except OSError:
        return text


def _path_key(path, cwd=None):
    """Canonical path key so both providers land on one node for the same local file."""
    # No expandvars: paths come from transcripts, and $VAR / %VAR% would put env values in labels.
    raw = os.path.expanduser(str(path or "").strip())
    if not raw:
        return "document"
    candidate = Path(raw)
    if not candidate.is_absolute() and cwd:
        candidate = Path(cwd) / candidate
    return os.path.normcase(_resolved(candidate))


def _shared_target(path=None, cwd=None, hint=""):
    """One shared area node for every path into SHARED_DIR, even across providers. Off when unset."""
    if not SHARED_DIR:
        return None
    root = Path(SHARED_DIR).resolve(strict=False)
    name = root.name or "shared"
    root_key = os.path.normcase(str(root)).rstrip("\\/")
    candidate = None
    if path:
        raw = os.path.expanduser(str(path).strip())
        candidate = Path(raw)
        if not candidate.is_absolute() and cwd:
            candidate = Path(cwd) / candidate
        candidate_key = os.path.normcase(_resolved(candidate))
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


SKILL_FILES = {"skill.md", "miniskill.md"}


def skill_target(name):
    """One node per skill, whether it was invoked with the Skill tool or its files were read."""
    name = str(name).rsplit(":", 1)[-1].strip() or "skill"
    return {"id": "skill:" + name.casefold(), "kind": "skill", "label": name,
            "group": "skills", "groupLabel": "skills"}


def _skill_name(path):
    """The skill a path belongs to: a SKILL.md/MINISKILL.md's folder, or <name> in .../skills/<name>/..."""
    parts = PurePath(str(path).replace("\\", "/")).parts
    if len(parts) >= 2 and parts[-1].casefold() in SKILL_FILES:
        return parts[-2]
    folded = [p.casefold() for p in parts]
    for i in range(len(parts) - 2, 0, -1):      # innermost skills/ folder wins
        if folded[i - 1] == "skills":
            return parts[i]
    return None


def document_target(path, cwd=None):
    """Build a normalized file node, the shared area for a file in SHARED_DIR, or the skill it belongs to."""
    shared = _shared_target(path, cwd)
    if shared:
        return shared
    skill = _skill_name(path)
    if skill:
        return skill_target(skill)
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
    if name == "Skill" and (inp.get("skill") or inp.get("command")):
        return {**skill_target(inp.get("skill") or inp.get("command")), "edge": "call"}
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


# ---- Ship events: commits, pushes and pull requests found in shell calls.
SHIP_EVENTS_TOTAL = 200     # newest ship events kept across all transcripts
# Tools that start a shell command. BashOutput, KillShell, Monitor and write_stdin only watch or feed
# a command that is already running, so they never start a commit or a push themselves.
SHELL_RUN_TOOLS = {"Bash", "PowerShell", "exec", "exec_command", "shell", "powershell", "bash", "run_command",
                   "pwsh"}
SHELLS = {"bash", "sh", "zsh", "dash", "fish", "pwsh", "powershell", "cmd"}
SHIP_TEXT_MAX = 200         # branch and remote names are shown as plain text; keep them short
# The only text from a transcript the page may turn into a link. ASCII only, so look-alike
# letters cannot sneak into the owner or repo name.
PR_URL = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", re.ASCII | re.MULTILINE)
# `[main 1a2b3c4] message`, also `[main (root-commit) 1a2b3c4]` and `[detached HEAD 1a2b3c4]`
COMMIT_LINE = re.compile(r"^\[([^\]\n]+?)(?: \(root-commit\))? [0-9a-f]{7,40}\] ", re.MULTILINE)
# A pushed ref: ` * [new branch]  feature-x -> feature-x`, `   1a2b..3c4d  main -> main`, ` + a...b  x -> x`.
# Rejected refs (`!`) are left out here and caught by the `! [rejected]` check.
PUSH_REF = re.compile(r"^\s*(?:[+*=-]\s+)?(?:(?:\[[^\]\n]+\]|[0-9a-f]{4,40}\.\.\.?[0-9a-f]{4,40})\s+)?"
                      r"(\S+)\s+->\s+(\S+)", re.MULTILINE)
TRACKING = re.compile(r"branch '([^'\n]+)' set up to track", re.IGNORECASE)   # older git writes "Branch"
EXIT_CODE = re.compile(r"(?:exit code|exited with code|exit_code\"?)[:\s]+(-?\d+)", re.IGNORECASE)
REDIRECT = re.compile(r"^(?:\d*|&)(?:>>?|<)(.*)$")
CD_COMMANDS = {"cd", "pushd", "chdir", "set-location", "sl"}
PUSH_VALUE_OPTIONS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
PR_VALUE_OPTIONS = {"-R", "--repo", "-B", "--base", "-t", "--title", "-b", "--body", "-F", "--body-file",
                    "-a", "--assignee", "-l", "--label", "-m", "--milestone", "-p", "--project",
                    "-r", "--reviewer", "-T", "--template"}


def _shell_segments(command):
    """Split a command line on &&, ||, ;, | and newlines, keeping quoted text (commit messages) whole."""
    out, cur, quote, i = [], [], None, 0
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                cur.append(command[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = None
            cur.append(ch)
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch in ";\n|" or (ch == "&" and command[i - 1:i] not in (">", "<") and command[i + 1:i + 2] != ">"):
            out.append("".join(cur))
            cur = []
            if command[i:i + 2] in ("&&", "||"):
                i += 1
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [segment.strip() for segment in out if segment.strip()]


def _shell_words(segment):
    """Words of one simple command, quotes removed, backslashes kept (Windows paths), redirects dropped."""
    lexer = shlex.shlex(segment, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    lexer.commenters = ""
    try:
        words = list(lexer)
    except ValueError:      # an unclosed quote: fall back to plain spaces
        words = segment.split()
    out, i = [], 0
    while i < len(words):
        redirect = REDIRECT.match(words[i])
        if redirect:
            i += 1 if redirect.group(1) else 2   # `2>&1` is one word; `> file` is two
            continue
        out.append(words[i])
        i += 1
    # leading `FOO=bar`, `env`, `command` and `time` only change how the real program runs
    while out and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", out[0]) or out[0] in ("env", "command", "time", "&")):
        out.pop(0)
    return out


def _program(word):
    name = PurePath(str(word).replace("\\", "/")).name.casefold()
    return name[:-4] if name.endswith(".exe") else name


def _join_dir(cwd, path):
    raw = os.path.expanduser(str(path or ""))
    if not raw:
        return cwd
    if os.path.isabs(raw) or not cwd:
        return raw
    return os.path.join(cwd, raw)


def _clip(value):
    return str(value)[:SHIP_TEXT_MAX] if value else None


def ship_steps(command, cwd=None):
    """The commit, push and PR steps one shell command would run, in order, with what the command says."""
    if not isinstance(command, str) or not re.search(r"\bgit\b|\bgh\b", command):
        return []
    steps = []
    for segment in _shell_segments(command):
        words = _shell_words(segment)
        if not words:
            continue
        program = _program(words[0])
        if program in CD_COMMANDS:
            targets = [w for w in words[1:] if not w.startswith("-")]
            if targets and targets[-1] != "-":
                cwd = _join_dir(cwd, targets[-1])   # later steps run in that folder
            continue
        if program == "gh" and len(words) >= 3 and words[1] == "pr" and words[2] in ("create", "new"):
            branch, skip = None, False
            for i, word in enumerate(words[3:], 3):
                if skip:
                    skip = False
                elif word in ("-H", "--head") and i + 1 < len(words):
                    branch, skip = words[i + 1], True
                elif word.startswith("--head="):
                    branch = word.split("=", 1)[1]
                elif word in PR_VALUE_OPTIONS:
                    skip = True
            steps.append({"step": "pr", "branch": branch, "remote": None, "cwd": cwd})
            continue
        if program != "git":
            continue
        i = 1
        while i < len(words) and words[i].startswith("-"):   # git's own options come before the subcommand
            if words[i] == "-C" and i + 1 < len(words):
                cwd = _join_dir(cwd, words[i + 1])
                i += 2
            elif words[i] in ("-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(words):
                i += 2
            else:
                i += 1
        if i >= len(words):
            continue
        sub, args = words[i], words[i + 1:]
        if sub == "commit":
            if "--dry-run" not in args:
                steps.append({"step": "commit", "branch": None, "remote": None, "cwd": cwd})
        elif sub == "push":
            if any(a in ("-n", "--dry-run", "-d", "--delete") for a in args):
                continue    # a rehearsal or a branch deletion ships nothing
            positional, skip = [], False
            for arg in args:
                if skip:
                    skip = False
                elif arg in PUSH_VALUE_OPTIONS:
                    skip = True
                elif not arg.startswith("-"):
                    positional.append(arg)
            branch = None
            for ref in positional[1:2]:
                ref = ref.lstrip("+").split(":", 1)[-1]   # `src:dst` lands on dst
                branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
            steps.append({"step": "push", "branch": branch or None,
                          "remote": positional[0] if positional else None, "cwd": cwd})
    return steps


def _result_text(value):
    """Plain text of a tool result, whatever shape the runtime stored it in."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_result_text(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(_result_text(value[key]) for key in ("text", "output", "content", "stdout", "stderr")
                         if key in value)
    return ""


def _exit_failed(text):
    """True when the result reports a non-zero exit code near its top."""
    match = EXIT_CODE.search(text[:400])
    return bool(match) and match.group(1) != "0"


def command_of(name, inp, raw_text=""):
    """The command line a shell tool call runs, or None for any other call."""
    if name not in SHELL_RUN_TOOLS:
        return None
    inp = inp if isinstance(inp, dict) else {}
    for key in ("command", "cmd", "command_line", "script"):
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            # Codex's ["bash", "-lc", "<script>"] carries the real line last
            if len(value) >= 2 and _program(value[0]) in SHELLS:
                return value[-1]
            return shlex.join(value)
    return raw_text if raw_text and not raw_text.lstrip().startswith("{") else None


def ship_events(agent, call_id, steps, text, at, failed=False):
    """Ship events for one finished shell call: each step, judged from what the command printed."""
    text = text or ""
    lower = text.casefold()
    bad = "error:" in lower or "fatal:" in lower
    pushed = list(PUSH_REF.finditer(text))
    tracking = TRACKING.search(text)
    commit_line = COMMIT_LINE.search(text)
    pr_url = PR_URL.search(text)
    events, used, pushed_branch = [], {}, None
    for step in steps:
        kind, branch, url = step["step"], step["branch"], None
        if kind == "commit":
            # The `[branch hash]` line proves the commit landed even when a later chained step failed.
            ok = bool(commit_line) or not ("nothing to commit" in lower or bad or failed)
            if commit_line and not branch:
                branch = commit_line.group(1)
        elif kind == "push":
            ok = bool(pushed or "everything up-to-date" in lower or tracking) and \
                not ("! [rejected]" in lower or bad or "denied" in lower)
            if not branch or branch == "HEAD":
                branch = (pushed[0].group(2) if pushed else tracking.group(1) if tracking else None) or branch
            if branch and branch.startswith("refs/heads/"):
                branch = branch[len("refs/heads/"):]
            pushed_branch = branch or pushed_branch
        else:
            branch = branch or pushed_branch    # `git push -u origin HEAD && gh pr create` opens the PR for that branch
            ok = bool(pr_url)
            url = pr_url.group(0) if pr_url else None
        used[kind] = used.get(kind, 0) + 1
        name = kind if used[kind] == 1 else f"{kind}{used[kind]}"   # a second push in one line keeps its own id
        cwd = step["cwd"]
        events.append({"id": f"{agent}:{call_id}:{name}", "agent": agent, "at": at, "step": kind, "ok": ok,
                       "branch": _clip(branch), "remote": _clip(step["remote"]), "url": url,
                       "cwd": cwd or None,
                       "folder": "doc:" + _path_key(cwd).replace("\\", "/") if cwd else None})
    return events


def ship_target(folder, doc_groups):
    """The doc group the page can beam from: the cwd's own folder, else its busiest folder below it."""
    if not folder:
        return None
    if folder in doc_groups:
        return folder
    prefix = folder.rstrip("/") + "/"
    under = [group for group in doc_groups if group.startswith(prefix)]
    return min(under, key=lambda g: (-doc_groups[g], len(g), g)) if under else None


# ---- Jev calls: `jev_decide` on the `jev` MCP server, so the plane can show an agent tapping into Jev and back.
# Exact name only, never "anything containing jev". Claude Code and Pi write MCP tools as `mcp__<server>__<tool>`;
# Codex writes a namespaced function_call that codex_tool_name joins into the same name.
#
# A Jev result is the one tool result read beyond shell output (approved by Preet 2026-11-02, exactly this list):
# only for a result matched to a Jev call id still waiting, only when its text is at most JEV_RESULT_MAX bytes of
# JSON, and only `ok`, `failOpen`, `latencyMs`, `policy`, `decision.applied`, `decision.reason`, `error.reason` and
# at most JEV_AUDIT_ROWS rows of `applied`, `confidence` and `threshold` (or a yes/no decision's two thresholds) from
# `decision.audit`. Strings go through allow-lists, numbers become whole per-mille or milliseconds. The request,
# answers, probabilities, choice, value, ids, model, risks, redactions and every message are never kept.
JEV_TOOL = "mcp__jev__jev_decide"
JEV_PER_TRANSCRIPT = 32     # Jev calls remembered per transcript, newest last
JEV_WAITING_MAX = 64        # Jev call ids waiting for their result, per transcript
JEV_PER_AGENT = 8           # newest Jev calls sent per agent
JEV_TOTAL = 40              # newest Jev calls sent in all
JEV_RESULT_MAX = 64 * 1024  # a larger result text is not read (the call is done, its outcome unknown)
JEV_AUDIT_ROWS = 8          # further audit rows are dropped uncounted
JEV_LATENCY_MAX = 3_600_000
JEV_POLICIES = ("none", "generic", "contextKeep", "routeModel", "safetyGate", "shouldContinue")
JEV_REASONS = (
    "all_applied", "all_fallback", "partial_fallback", "no_decisions", "kept_all", "dropped_unneeded_items",
    "truncated_items", "high_confidence", "low_confidence", "middle_band", "unknown_option", "missing_answer",
    "no_profiles", "above_threshold", "below_threshold", "pinned", "tool_pair", "destructive", "exfiltration",
    "scope_drift", "reversible_low_risk", "not_reversible",
)
JEV_ERRORS = ("missing_key", "timeout", "transport_error", "http_error", "malformed_response", "state_too_large",
              "invalid_request")


def codex_tool_name(item):
    """A Codex call's tool name: an MCP namespace (`mcp__<server>`) and its name joined as Claude and Pi spell it."""
    name = item.get("name") or item.get("tool_name") or ""
    namespace = item.get("namespace")
    if (isinstance(name, str) and name and not name.startswith("mcp__") and isinstance(namespace, str)
            and namespace.startswith("mcp__") and len(namespace) > len("mcp__")):
        return f"{namespace}__{name}"
    return name if isinstance(name, str) else ""


def _jev_text(content):
    """A result's text: a string, or the first text block of a content array."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                return block["text"]
    return None


def _jev_json(text):
    if text is None or len(text) > JEV_RESULT_MAX or len(text.encode("utf-8", errors="replace")) > JEV_RESULT_MAX:
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _flag(value):
    return value if isinstance(value, bool) else None


def _number(value, top):
    """A finite number in 0..top, else None (out of range is dropped, not clamped). bool is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if 0 <= value <= top else None


def _per_mille(value):
    value = _number(value, 1)
    return None if value is None else int(math.floor(value * 1000 + 0.5))


def _allow(allowed, value):
    return (value if value in allowed else "other") if isinstance(value, str) else None


def jev_outcome(content):
    """The bounded outcome of one Jev result (see the section note), or None when it can't be read."""
    value = _jev_json(_jev_text(content))
    if not (isinstance(value, dict) and "ok" in value):
        # One level of an MCP wrapper: {"content":[{"type":"text","text":"..."}]}, or the bare content array.
        inner = value if isinstance(value, list) else value.get("content") if isinstance(value, dict) else None
        value = _jev_json(_jev_text(inner)) if isinstance(inner, list) else None
        if not (isinstance(value, dict) and "ok" in value):
            return None
    decision = value.get("decision") if isinstance(value.get("decision"), dict) else {}
    error = value.get("error") if isinstance(value.get("error"), dict) else {}
    latency = _number(value.get("latencyMs"), JEV_LATENCY_MAX)
    out = {"ok": _flag(value.get("ok")), "fail_open": _flag(value.get("failOpen")),
           "latency_ms": None if latency is None else int(math.floor(latency + 0.5)),
           "policy": _allow(JEV_POLICIES, value.get("policy")), "applied": _flag(decision.get("applied")),
           "reason": _allow(JEV_REASONS, decision.get("reason")), "error": _allow(JEV_ERRORS, error.get("reason")),
           "audit": []}
    audit = decision.get("audit")

    def row(applied, measure, value_pm=None, threshold_pm=None, yes_pm=None, no_pm=None):
        return {"applied": applied, "measure": measure, "value_pm": value_pm, "threshold_pm": threshold_pm,
                "yes_pm": yes_pm, "no_pm": no_pm}

    if out["policy"] in ("generic", "contextKeep") and isinstance(audit, list):
        # generic: one row per decision (a choice, or a yes/no "noul"); contextKeep: one per item, no applied flag
        generic = out["policy"] == "generic"
        for item in audit[:JEV_AUDIT_ROWS]:
            item = item if isinstance(item, dict) else {}
            if generic and item.get("kind") == "noul":
                # Only the two thresholds: with them and `applied`, the probability would reveal the decision's value.
                thresholds = item.get("thresholds") if isinstance(item.get("thresholds"), dict) else {}
                out["audit"].append(row(_flag(item.get("applied")), "yes_no",
                                        yes_pm=_per_mille(thresholds.get("yes")), no_pm=_per_mille(thresholds.get("no"))))
            else:
                out["audit"].append(row(_flag(item.get("applied")) if generic else None, "confidence",
                                        _per_mille(item.get("confidence")), _per_mille(item.get("threshold"))))
    elif out["policy"] in ("routeModel", "shouldContinue") and isinstance(audit, dict):
        out["audit"].append(row(out["applied"], "confidence", _per_mille(audit.get("confidence")),
                                _per_mille(audit.get("threshold"))))
    # safetyGate's thresholds apply to its risks, which are not kept: no rows. `none` has no decision.
    return out


class JevLog:
    """The Jev calls of one transcript, bounded, and the call ids still waiting for their result."""

    def __init__(self):
        self.calls = []         # {"call", "n", "phase", "at", "end", "outcome"}, oldest first
        self.waiting = []       # call ids whose result may be read; cleared at every new user turn
        self._seen = {}         # call id -> calls seen with it, so a reused id still gets its own stable id

    def _push(self, call_id, phase, at, end):
        n = self._seen.get(call_id, 0)
        self._seen[call_id] = n + 1
        self.calls.append({"call": call_id, "n": n, "phase": phase, "at": at, "end": end, "outcome": None})
        del self.calls[:-JEV_PER_TRANSCRIPT]

    def started(self, call_id, at):
        call_id = str(call_id or "")
        # A repeated start (a replayed line) is the same call: one still pending, or one with the same start time.
        if any(c["call"] == call_id and (c["phase"] == "pending" or (call_id and c["at"] == at)) for c in self.calls):
            return
        self._push(call_id, "pending", at or None, None)
        if call_id and call_id not in self.waiting:
            self.waiting.append(call_id)
            del self.waiting[:-JEV_WAITING_MAX]

    def result(self, call_id, at, content, named=None):
        """A tool result. `named` is the tool the result names itself (Pi does): that name decides whether it is
        Jev's. Otherwise only a result for a waiting Jev call id counts. Returns True for a Jev result."""
        call_id = str(call_id or "")
        if named is not None and named != JEV_TOOL:
            return False
        waiting = bool(call_id) and call_id in self.waiting
        if not waiting and named is None:
            return False
        if waiting:
            self.waiting.remove(call_id)
        call = next((c for c in reversed(self.calls) if c["call"] == call_id and c["phase"] == "pending"), None)
        if call:
            # The result's text is read only for a waiting id.
            call.update(phase="done", end=at or None, outcome=jev_outcome(content) if waiting else None)
            return True
        if named is None:
            return True
        # A late or repeated result for a call already logged (done, or missing after a new turn) is not a new call.
        if call_id and any(c["call"] == call_id for c in self.calls):
            return True
        # A result whose start was never seen (its line was dropped): finished, with no start time invented.
        self._push(call_id, "done", None, at or None)
        return True

    def busy(self):
        """A call is still pending (so a new user turn would change it)."""
        return bool(self.waiting) or any(c["phase"] == "pending" for c in self.calls)

    def turn_started(self, at):
        """A new user turn: a call still pending won't get its result, and no waiting id may be read any more."""
        for call in self.calls:
            if call["phase"] == "pending":
                call.update(phase="missing", end=at or None)
        self.waiting.clear()

    def view(self, agent):
        """Payload rows for this transcript's calls (copies; the per-file cache stays as parsed)."""
        out = []
        for c in self.calls:
            key = f"{agent}\0{c['call']}" + (f"\0{c['n']}" if c["n"] else "")
            out.append({"id": hashlib.sha1(key.encode("utf-8")).hexdigest()[:12], "agent": agent,
                        "at": c["at"], "end": c["end"], "phase": c["phase"],
                        "outcome": {**c["outcome"], "audit": [dict(r) for r in c["outcome"]["audit"]]}
                        if c["outcome"] else None})
        return out


def jev_rows(parsed_files, now, aliases=None):
    """Newest Jev calls across transcripts, newest first: at most JEV_PER_AGENT per agent and JEV_TOTAL in all.
    A call still pending in a transcript that went quiet past its active window is reported missing."""
    aliases = aliases or {}
    rows = []
    for parsed in parsed_files:
        quiet = now - (parsed.get("activity") or 0) > parsed.get("activeWindow", ACTIVE_S)
        for row in parsed.get("jevs", []):
            row = {**row, "agent": aliases.get(row["agent"], row["agent"])}
            if quiet and row["phase"] == "pending":
                row["phase"] = "missing"
            rows.append(row)
    rows.sort(key=lambda r: r["at"] if r["at"] is not None else r["end"] or 0, reverse=True)
    per_agent, out = {}, []
    for row in rows:
        if per_agent.get(row["agent"], 0) >= JEV_PER_AGENT:
            continue
        per_agent[row["agent"]] = per_agent.get(row["agent"], 0) + 1
        out.append(row)
        if len(out) == JEV_TOTAL:
            break
    return out


def subagent_label(path):
    """A subagent's description from its .meta.json, else its file name."""
    try:
        data = json.loads(path.with_name(path.stem + ".meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return path.stem
    return data.get("description") or data.get("agentType") or path.stem


def _claude_user_turn(entry):
    """True for a line that starts a new user turn: text from the person (or a notification), not a tool result."""
    message = entry.get("message")
    if not isinstance(message, dict) or entry.get("isMeta"):
        return False
    if entry.get("type") != "user" and message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    return (isinstance(content, list) and
            any(isinstance(p, dict) and p.get("type") == "text" for p in content) and
            not any(isinstance(p, dict) and p.get("type") == "tool_result" for p in content))


def parse_file(path):
    """Aggregate one Claude transcript into nodes, edges, and resource call events."""
    agent, parent = agent_key(path)
    provider = "claude"
    nodes = {}
    edges = {}          # (src, dst, kind) -> {count, lastAt}
    open_calls = {}     # tool_use id -> call event still waiting for its result
    events = []         # newest call events, for the flow view
    busy = False        # the newest turn still waits on a tool or on the model (no end_turn after it)
    ships = []          # commits, pushes and PRs this agent ran, judged once their result is in
    pending_ships = {}  # tool_use id -> (steps, at) for a shell call that may ship, until its result
    cwd = None          # every Claude transcript line records the folder the agent was working in
    jev = JevLog()
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"end_turn"' in line:
                    try:
                        if (json.loads(line).get("message") or {}).get("stop_reason") == "end_turn":
                            busy = False
                    except ValueError:
                        pass
                # user lines matter only while a Jev call waits: a new turn means its result won't come
                jev_turn = '"user"' in line and jev.busy()
                if '"tool_use"' not in line and '"tool_result"' not in line and not jev_turn:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                at = _ts(entry.get("timestamp")) or 0.0
                if jev_turn and _claude_user_turn(entry):
                    jev.turn_started(at)
                cwd = entry.get("cwd") or cwd
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
                        pending = pending_ships.pop(part.get("tool_use_id"), None)
                        if pending:
                            text = _result_text(part.get("content"))
                            ships.extend(ship_events(agent, part.get("tool_use_id"), pending[0], text, at or pending[1],
                                                     bool(part.get("is_error")) or _exit_failed(text)))
                        jev.result(part.get("tool_use_id"), at, part.get("content"))
                    elif part.get("type") == "tool_use":
                        name, inp = part.get("name") or "", part.get("input")
                        if name == JEV_TOOL:
                            jev.started(part.get("id"), at)
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
                            steps = ship_steps(command_of(name, inp), cwd)
                            if steps:
                                pending_ships[part["id"]] = (steps, at)
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
            "events": events[-EVENTS_PER_FILE:], "ships": ships[-SHIP_EVENTS_TOTAL:],
            "jevs": jev.view(agent), "activity": mtime, "activeWindow": ACTIVE_S}


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
    ships, pending_ships = [], {}
    ship_cwd = cwd          # turn_context moves this when the session changes folder; the node keeps its first cwd
    jev = JevLog()
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
        workdir = inp.get("workdir") if isinstance(inp, dict) else None
        steps = ship_steps(command_of(name, inp, raw_text),
                           _join_dir(ship_cwd, workdir) if isinstance(workdir, str) else ship_cwd)
        if call_id and steps:
            pending_ships[call_id] = (steps, at)

    def finish_ship(call_id, output, at):
        pending = pending_ships.pop(call_id, None)
        if not pending:
            return
        failed = False
        if isinstance(output, str):
            try:
                decoded = json.loads(output)
            except ValueError:
                decoded = None
            if isinstance(decoded, dict):   # older Codex wraps the output and its exit code in JSON
                metadata = decoded.get("metadata")
                code = metadata.get("exit_code") if isinstance(metadata, dict) else None
                failed = isinstance(code, int) and code != 0
                output = decoded.get("output", output)
        text = _result_text(output)
        ships.extend(ship_events(agent, call_id, pending[0], text, at or pending[1], failed or _exit_failed(text)))

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
                    ship_cwd = payload.get("cwd") or ship_cwd
                elif kind == "event_msg":
                    event_type = payload.get("type")
                    event_at = _ts(payload.get("started_at") if event_type == "task_started" else payload.get("completed_at")) or at
                    if event_type == "task_started":
                        jev.turn_started(event_at)
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
                        name, call_id = codex_tool_name(payload), payload.get("call_id") or payload.get("id")
                        if name == JEV_TOOL:
                            jev.started(call_id, at)
                        add_call(name, args, raw_text, at, call_id)
                    elif item_type in ("custom_tool_call_output", "function_call_output", "tool_result"):
                        calls = open_calls.pop(payload.get("call_id") or payload.get("tool_call_id"), None) or []
                        for call in calls:
                            if call["at"] and at:
                                call["ms"] = max(0, int((at - call["at"]) * 1000))
                        finish_ship(payload.get("call_id") or payload.get("tool_call_id"), payload.get("output"), at)
                        jev.result(payload.get("call_id") or payload.get("tool_call_id"), at, payload.get("output"))
                elif kind in ("custom_tool_call", "function_call", "tool_use"):
                    args, raw_text = _codex_args(entry)
                    name, call_id = codex_tool_name(entry), entry.get("call_id") or entry.get("id")
                    if name == JEV_TOOL:
                        jev.started(call_id, at)
                    add_call(name, args, raw_text, at, call_id)
                elif kind in ("custom_tool_call_output", "function_call_output", "tool_result"):
                    calls = open_calls.pop(entry.get("call_id") or entry.get("tool_call_id"), None) or []
                    for call in calls:
                        if call["at"] and at:
                            call["ms"] = max(0, int((at - call["at"]) * 1000))
                    finish_ship(entry.get("call_id") or entry.get("tool_call_id"), entry.get("output"), at)
                    jev.result(entry.get("call_id") or entry.get("tool_call_id"), at, entry.get("output"))
        mtime = path.stat().st_mtime
        latest_activity = max(latest_activity, mtime)
    except OSError:
        return None

    active = latest_started > latest_finished and now - max(latest_activity, latest_started) <= CODEX_ACTIVE_S
    if open_calls and now - latest_activity <= ACTIVE_S:
        active = True
    label = f"GPT · {project} · {str(session_id)[-6:]}"
    nodes[agent] = {"kind": "agent", "label": label, "name": label, "provider": "codex", "domain": "codex",
                    "space": "codex", "environment": _environment(meta), "state": "working" if active else "done", "model": model,
                    "cwd": cwd, "mtime": mtime, "open": active}
    return {"agent": agent, "provider": "codex", "nodes": nodes, "edges": edges,
            "open": open_calls, "events": events[-EVENTS_PER_FILE:], "ships": ships[-SHIP_EVENTS_TOTAL:],
            "jevs": jev.view(agent), "activity": latest_activity, "activeWindow": CODEX_ACTIVE_S}


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
    ships, pending_ships = [], {}
    spawn_calls = {}
    jev = JevLog()
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
        steps = ship_steps(command_of(name, inp), cwd)
        if call_id and steps:
            pending_ships[call_id] = (steps, at)
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
                if role == "user":
                    jev.turn_started(at)
                elif role == "assistant":
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
                            if block.get("name") == JEV_TOOL:
                                jev.started(block.get("id") or block.get("toolCallId"), at)
                            add_call(block.get("name") or "", args, at, block.get("id") or block.get("toolCallId"))
                elif role == "toolResult":
                    finish_call(message.get("toolCallId"), at, message.get("durationMs"))
                    pending = pending_ships.pop(message.get("toolCallId"), None)
                    if pending:
                        text = _result_text(message.get("content"))
                        ships.extend(ship_events(agent, message.get("toolCallId"), pending[0], text, at or pending[1],
                                                 bool(message.get("isError")) or _exit_failed(text)))
                    named = message.get("toolName")   # Pi names the tool on its result: that name decides
                    jev.result(message.get("toolCallId"), at, message.get("content"),
                               named if isinstance(named, str) and named else None)

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
            "open": open_calls, "events": events[-EVENTS_PER_FILE:], "ships": ships[-SHIP_EVENTS_TOTAL:],
            "jevs": jev.view(agent), "activity": latest_activity, "activeWindow": PI_ACTIVE_S}


# ---- Antigravity: Google's agent CLI (Gemini models). One SQLite file per conversation in
# <ANTIGRAVITY_DIR>/conversations/<id>.db; each row of `steps` holds one protobuf step. There is no
# published schema, so the field numbers below were read off real files (Antigravity CLI, Nov 2026)
# and every lookup tolerates a missing or malformed field.
ANTIGRAVITY_ACTIVE_S = 6 * 60
ANTIGRAVITY_STEP_ROWS = 3000     # newest steps read per conversation
AG_STEP_USER, AG_STEP_MODEL, AG_STEP_TOOL = 14, 15, 132
AG_STATUS_RUNNING, AG_STATUS_DONE = 1, 3   # 7 and 9 were a denied and a failed tool call


def _pb_varint(data, i):
    value, shift = 0, 0
    while True:
        byte = data[i]
        i += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return value, i


def _pb_fields(data):
    """Top-level (field, wire type, value) triples of one protobuf message; [] if it isn't one."""
    data = data or b""
    out, i, n = [], 0, len(data)
    try:
        while i < n:
            key, i = _pb_varint(data, i)
            field, wire = key >> 3, key & 7
            if wire == 0:
                value, i = _pb_varint(data, i)
            elif wire == 2:
                size, i = _pb_varint(data, i)
                if i + size > n:
                    return []
                value = bytes(data[i:i + size])
                i += size
            elif wire == 1:
                value, i = bytes(data[i:i + 8]), i + 8
            elif wire == 5:
                value, i = bytes(data[i:i + 4]), i + 4
            else:
                return []
            out.append((field, wire, value))
    except IndexError:
        return []
    return out if i == n else []


def _pb_get(data, *path):
    """The first value at a field path (each number steps into a nested message), or None."""
    value = data
    for number in path:
        if not isinstance(value, (bytes, bytearray)):
            return None
        value = next((v for f, _, v in _pb_fields(value) if f == number), None)
        if value is None:
            return None
    return value


def _pb_text(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return ""


def _pb_time(value):
    """A google.protobuf.Timestamp {1: seconds, 2: nanos} as epoch seconds, or 0."""
    seconds = _pb_get(value, 1) if isinstance(value, (bytes, bytearray)) else None
    if not isinstance(seconds, int):
        return 0.0
    nanos = _pb_get(value, 2)
    return seconds + (nanos / 1e9 if isinstance(nanos, int) else 0.0)


def _snake_keys(args):
    """Antigravity spells arguments Path / CommandLine; the shared helpers expect path / command_line."""
    out = dict(args)
    for key, value in args.items():
        if isinstance(key, str):
            out.setdefault(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).lower(), value)
    return out


def _sqlite_ro(path):
    """Read-only connection that never creates or changes the file (works while Antigravity writes)."""
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)


def antigravity_summaries(root=None):
    """conversation id -> row of conversation_summaries.db (title, workspace, status, parent, ...)."""
    path = Path(root or ANTIGRAVITY_DIR) / "conversation_summaries.db"
    if not path.is_file():
        return {}
    cols = ("conversation_id", "title", "workspace_uris", "status", "parent_conversation_id",
            "agent_name", "not_fully_idle", "killed")
    try:
        con = _sqlite_ro(path)
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(f"select {', '.join(cols)} from conversation_summaries").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return {row["conversation_id"]: dict(row) for row in rows if row["conversation_id"]}


def antigravity_present(conversation_id, root=None):
    """True while an Antigravity CLI holds the conversation's presence lock, False when nobody holds it,
    None when that can't be told (no lock file, or not Windows, where the lock isn't probed).
    Only True means anything: the lock is held while a turn runs, and a resumed or idle CLI can keep
    writing to the conversation without holding it, so False is not proof the CLI has exited."""
    lock = Path(root or ANTIGRAVITY_DIR) / "presence" / f"{conversation_id}.lock"
    if os.name != "nt" or not lock.is_file():
        return None
    try:
        with lock.open("rb"):
            return False     # nobody holds it right now
    except PermissionError:
        return True
    except OSError:
        return None


def _workspace_path(uris):
    """The first workspace of a conversation as a local path ("file:///C:/Users/x" -> "C:/Users/x")."""
    try:
        first = (json.loads(uris) if isinstance(uris, str) else uris or [])[0]
    except (ValueError, IndexError, TypeError, KeyError):
        return ""
    if not isinstance(first, str) or not first.startswith("file://"):
        return ""
    path = unquote(urlsplit(first).path)
    return path[1:] if re.match(r"^/[A-Za-z]:", path) else path


def parse_antigravity_db(path, now=None, summary=None, present=None):
    """Parse one Antigravity conversation database into the shared agent/resource graph."""
    now = now or time.time()
    path = Path(path)
    conversation_id = path.stem
    summary = summary or {}
    agent = "antigravity:" + conversation_id
    cwd = _workspace_path(summary.get("workspace_uris"))
    project = Path(cwd).name if cwd else "Antigravity"
    parent_cid = summary.get("parent_conversation_id") or ""
    parent_id = "antigravity:" + parent_cid if parent_cid and parent_cid != conversation_id else None
    nodes, edges, open_calls, events, ships = {}, {}, {}, [], []

    def add_call(name, args, at, end, call_id, status, result):
        inp = _snake_keys(args)
        targets = [target_of(name, inp), shared_type_target(name), path_target_from_input(inp, cwd),
                   shared_target_from_input(inp, cwd)]
        unique = {}
        for target in targets:
            if target:
                target.setdefault("space", "shared" if target.get("space") == "shared" else "antigravity")
                unique[target["id"]] = target
        calls = []
        detail = detail_of(inp)
        finished = status != AG_STATUS_RUNNING
        for target in unique.values():
            nodes[target["id"]] = {k: target[k] for k in ("kind", "label", "group", "groupLabel", "space")}
            key = (agent, target["id"], target.get("edge", "call"))
            edge = edges.setdefault(key, {"count": 0, "lastAt": 0.0})
            edge["count"] += 1
            edge["lastAt"] = max(edge["lastAt"], at)
            ms = max(0, int((end - at) * 1000)) if finished and end and at else None
            call = {"at": at, "src": agent, "dst": target["id"], "kind": target.get("edge", "call"),
                    "tool": name, "detail": detail or target.get("detail", ""), "ms": ms}
            events.append(call)
            calls.append(call)
        if not finished and calls:
            open_calls[call_id or f"{at}:{name}"] = calls
        steps = ship_steps(command_of(name, inp), cwd)
        if steps and finished:
            failed = status != AG_STATUS_DONE or _exit_failed(result)
            ships.extend(ship_events(agent, call_id, steps, result, end or at, failed))

    try:
        con = _sqlite_ro(path)
        try:
            rows = con.execute("select step_type, status, step_payload from steps order by idx desc limit ?",
                               (ANTIGRAVITY_STEP_ROWS,)).fetchall()[::-1]
            gen = con.execute("select data from gen_metadata order by idx desc limit 1").fetchone()
        finally:
            con.close()
        files = [p for p in (path, path.with_name(path.name + "-wal")) if p.exists()]
        mtime = max(p.stat().st_mtime for p in files)
    except (sqlite3.Error, OSError, ValueError):
        return None
    model = None
    if gen:
        model = _pb_text(_pb_get(gen[0], 1, 19)) or _pb_text(_pb_get(gen[0], 3, 28)) or None

    latest_activity, last_type = 0.0, None
    for step_type, status, payload in rows:
        meta = _pb_get(payload, 5)
        at = _pb_time(_pb_get(meta, 6)) or _pb_time(_pb_get(meta, 1))
        end = _pb_time(_pb_get(meta, 8))
        latest_activity = max(latest_activity, at, end)
        last_type = step_type
        call = _pb_get(meta, 4)
        if step_type != AG_STEP_TOOL or not isinstance(call, (bytes, bytearray)):
            continue
        name = _pb_text(_pb_get(call, 2))
        if not name:
            continue
        try:
            args = json.loads(_pb_text(_pb_get(call, 3)) or "{}")
        except ValueError:
            args = {}
        result = _pb_text(_pb_get(payload, 140, 2, 1))
        add_call(name, args if isinstance(args, dict) else {}, at, end, _pb_text(_pb_get(call, 1)), status, result)

    latest_activity = max(latest_activity, mtime)
    recent = now - latest_activity <= ANTIGRAVITY_ACTIVE_S
    is_open = bool(present) or recent   # a free lock is not proof of exit; recent writes are proof of life
    if summary.get("killed"):
        is_open = False
    if not is_open:
        open_calls.clear()   # steps left "running" by a CLI that went quiet are not in flight
    running = str(summary.get("status") or "").endswith("_RUNNING") and bool(summary.get("not_fully_idle", 1))
    working = bool(is_open and recent and (open_calls or running or last_type == AG_STEP_TOOL))
    title = str(summary.get("agent_name") or summary.get("title") or "").strip()
    label = f"Google · {project} · {conversation_id[-6:]}"
    if parent_id:
        nodes.setdefault(parent_id, {"kind": "agent", "label": f"Google · {project} · {parent_cid[-6:]}",
                                     "provider": "antigravity", "domain": "antigravity", "space": "antigravity",
                                     "environment": "local", "cwd": cwd, "mtime": mtime, "open": is_open,
                                     "_parentStub": True})
        edges[(parent_id, agent, "spawn")] = {"count": 1, "lastAt": mtime}
    nodes[agent] = {"kind": "subagent" if parent_id else "agent", "label": label, "name": label,
                    "parent": parent_id, "provider": "antigravity", "domain": "antigravity",
                    "space": "antigravity", "environment": "local", "title": title or None,
                    "state": "working" if working else "idle" if is_open else "done",
                    "model": model, "cwd": cwd, "mtime": mtime, "open": is_open}
    return {"agent": agent, "provider": "antigravity", "nodes": nodes, "edges": edges,
            "open": open_calls, "events": events[-EVENTS_PER_FILE:], "ships": ships[-SHIP_EVENTS_TOTAL:]}


# ---- DeepSeek Harness (DSH): the transcript is zstd-compressed (sessions/<cwd>/<id>/session.v4.jsonl.zstd),
# but the Harness keeps a plain-JSON projection of every Session beside it, the same projection its own
# dashboard plugin reads: storages/session_projcache/sessions/<id>.json -> record.rows.<name>.val.
# Parent -> child links live only in the parent's subagentCatalog, so all files are read together.
DSH_ACTIVE_S = 6 * 60      # the projection is rewritten on every step; quiet this long = not open
DSH_TURN_S = 30 * 60       # an open turn or a pending question keeps a quiet Session open this long


def _dsh_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _dsh_rows(path):
    rows = ((_dsh_json(path) or {}).get("record") or {}).get("rows")
    if not isinstance(rows, dict):
        return None
    return {k: v.get("val") for k, v in rows.items() if isinstance(v, dict)}


def _dsh_workspace(root):
    """(archived session ids, {session id: workspace title}) from storages/workspace.json."""
    data = _dsh_json(Path(root) / "storages" / "workspace.json") or {}
    archived = set((data.get("global") or {}).get("archivedSessionIds") or [])
    titles = {}
    for ws in ((data.get("tables") or {}).get("workspaces") or {}).values():
        if isinstance(ws, dict):
            for sid in ws.get("sessionIds") or []:
                titles[sid] = ws.get("title") or ""
    return archived, titles


def _dsh_inputs(root):
    root = Path(root)
    return [*(root / "storages" / "session_projcache" / "sessions").glob("*.json"),
            *(root / "sessions").glob("*/*/session.v4.jsonl.zstd"), root / "storages" / "workspace.json"]


def dsh_key(root=None, now=None):
    """Cache key for parse_dsh: every input file's (name, mtime, size) and the current minute, because
    open/closed also changes as time passes with no write."""
    stats = []
    for p in _dsh_inputs(root or DSH_DIR):
        try:
            st = p.stat()
        except OSError:
            continue
        stats.append((str(p), st.st_mtime, st.st_size))
    return tuple(sorted(stats)), int((now or time.time()) // 60)


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
DSH_READ_MAX = 32 * 1024 * 1024   # compressed bytes decoded per pass; a longer backlog is read over several passes
DSH_TAIL_RECORDS = 4000            # newest tool records kept per transcript
DSH_ARG_MAX = 4000                 # longest argument / result text kept (enough for a command and git's output)
# Splits appended zstd frames at the magic number and decodes each; a cut that fails to decode was the magic
# number inside compressed data (or a frame still being written), so it is merged with the next piece.
# Prints how many bytes decoded cleanly, a newline, then the text.
_NODE_ZSTD = ("const z=require('zlib'),c=[];process.stdin.on('data',d=>c.push(d)).on('end',()=>{"
              "const b=Buffer.concat(c),M=Buffer.from([0x28,0xb5,0x2f,0xfd]),cuts=[];"
              "for(let i=b.indexOf(M,1);i>=0;i=b.indexOf(M,i+1))cuts.push(i);cuts.push(b.length);"
              "let s=0;const o=[];for(const e of cuts){try{o.push(z.zstdDecompressSync(b.subarray(s,e)));s=e}catch{}}"
              "process.stdout.write(s+'\\n');process.stdout.write(Buffer.concat(o))})")
_zstd_decoder = None   # resolved on first use: "stdlib", "node", or "" when neither works
_DSH_TAILS = {}        # transcript path -> incremental decode state


def _zstd_split(data, decode_one):
    cuts, i = [], data.find(ZSTD_MAGIC, 1)
    while i >= 0:
        cuts.append(i)
        i = data.find(ZSTD_MAGIC, i + 1)
    cuts.append(len(data))
    start, out = 0, []
    for end in cuts:
        try:
            out.append(decode_one(data[start:end]))
            start = end
        except Exception:   # noqa: BLE001 - any decoder error means "not a whole frame yet"
            continue
    return b"".join(out), start


def _zstd_node(data):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        raise OSError("node not found")
    done = subprocess.run([node, "-e", _NODE_ZSTD], input=data, capture_output=True, timeout=30,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True)
    head, _, text = done.stdout.partition(b"\n")
    return text, int(head)


def zstd_frames(data):
    """(decoded bytes, bytes consumed) for a run of appended zstd frames; consumed stops before a frame that
    is incomplete. Python 3.14's compression.zstd when present, else Node's zlib (Node 22.15+)."""
    global _zstd_decoder
    if not data:
        return b"", 0
    if _zstd_decoder in (None, "stdlib"):
        try:
            from compression import zstd   # Python 3.14+
        except ImportError:
            pass
        else:
            _zstd_decoder = "stdlib"
            return _zstd_split(data, zstd.decompress)
    if _zstd_decoder in (None, "node"):
        try:
            out = _zstd_node(data)
        except Exception:   # noqa: BLE001 - no decoder: Sessions still show, without their calls
            if _zstd_decoder is None:
                _zstd_decoder = ""
        else:
            _zstd_decoder = "node"
            return out
    return b"", 0


def _dsh_clip(value):
    if isinstance(value, str):
        return value[:DSH_ARG_MAX]
    if isinstance(value, dict):
        return {k: _dsh_clip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dsh_clip(v) for v in value[:50]]
    return value


def dsh_records(path):
    """Tool calls and results of one DSH transcript, decoding only the frames appended since the last read.
    Records: ("call", at, call_id, name, args) and ("result", at, call_id, is_error, text)."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return []
    state = _DSH_TAILS.get(path)
    if state is None or size < state["offset"]:   # new, or rewritten from scratch
        state = _DSH_TAILS[path] = {"offset": 0, "partial": b"", "records": []}
    if size > state["offset"]:
        try:
            with path.open("rb") as f:
                f.seek(state["offset"])
                data = f.read(DSH_READ_MAX)
        except OSError:
            return state["records"]
        text, used = zstd_frames(data)
        if used:
            state["offset"] += used
            lines = (state["partial"] + text).split(b"\n")
            state["partial"] = lines.pop()
            records = state["records"]
            for raw in lines:
                if b'"tool/' not in raw:
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                kind, body = entry.get("type"), entry.get("data") or {}
                at = (entry.get("time") or 0) / 1000
                if kind == "tool/call":
                    try:
                        args = json.loads(body.get("arguments") or "{}")
                    except (TypeError, ValueError):
                        args = {}
                    records.append(("call", at, body.get("callId"), str(body.get("name") or ""),
                                    _dsh_clip(args) if isinstance(args, dict) else {}))
                elif kind == "tool/result":
                    msg = body.get("message") or {}
                    records.append(("result", at, msg.get("toolCallId"), bool(msg.get("isError")),
                                    _result_text(msg.get("content"))[:DSH_ARG_MAX]))
            del records[:-DSH_TAIL_RECORDS]
    return state["records"]


def _dsh_calls(agent, cwd, records, is_open, nodes, edges):
    """Events, in-flight calls and ship events of one DSH Session, drawn like the other runtimes'."""
    events, open_calls, ships, pending = [], {}, [], {}
    for rec in records:
        if rec[0] == "call":
            _, at, call_id, name, inp = rec
            if not name:
                continue
            unique = {}
            for target in (target_of(name, inp), shared_type_target(name), path_target_from_input(inp, cwd),
                           shared_target_from_input(inp, cwd)):
                if target:
                    target.setdefault("space", "shared" if target.get("space") == "shared" else "deepseek")
                    unique[target["id"]] = target
            calls, detail = [], detail_of(inp)
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
            steps = ship_steps(command_of(name, inp), cwd)
            if call_id and steps:
                pending[call_id] = (steps, at)
        else:
            _, at, call_id, is_error, text = rec
            for call in open_calls.pop(call_id, None) or []:
                call["ms"] = max(0, int((at - call["at"]) * 1000)) if at and call["at"] else None
            steps = pending.pop(call_id, None)
            if steps:
                ships.extend(ship_events(agent, call_id, steps[0], text, at or steps[1],
                                         is_error or _exit_failed(text)))
    if not is_open:
        open_calls.clear()   # calls left without a result by a Session that went quiet are not in flight
    return events[-EVENTS_PER_FILE:], open_calls, ships


def parse_dsh(root=None, now=None):
    """Every DeepSeek Harness Session touched within the graph window, as one parsed bundle."""
    now = now or time.time()
    root = Path(root or DSH_DIR)
    transcripts = {}
    for t in (root / "sessions").glob("*/*/session.v4.jsonl.zstd"):
        try:
            transcripts[t.parent.name] = (t, t.stat().st_mtime)
        except OSError:
            pass
    archived, ws_titles = _dsh_workspace(root)
    sessions, parents = {}, {}
    for path in (root / "storages" / "session_projcache" / "sessions").glob("*.json"):
        try:
            mtime = max(path.stat().st_mtime, transcripts.get(path.stem, (None, 0.0))[1])
        except OSError:
            continue
        if now - mtime > GRAPH_WINDOW_S:
            continue
        rows = _dsh_rows(path)
        if rows is None:
            continue
        sessions[path.stem] = (rows, mtime)
        for child in ((rows.get("subagentCatalog") or {}).get("head") or {}).get("values") or []:
            if isinstance(child, dict) and child.get("childId"):
                parents[child["childId"]] = (path.stem, child.get("label"))
    nodes, edges, events, open_calls, ships = {}, {}, [], {}, []
    for sid, (rows, mtime) in sessions.items():
        agent = "deepseek:" + sid
        cwd = rows.get("workingDirectory") or ""
        project = ws_titles.get(sid) or (Path(cwd).name if cwd else "DeepSeek")
        last_ms = (rows.get("timeContext") or {}).get("lastMessageTime")
        activity = max(mtime, (last_ms or 0) / 1000)
        age = now - activity
        turn_open = (rows.get("turnBoundary") or {}).get("openTurnStartSeq") is not None
        asking = bool(((rows.get("userQuestions") or {}).get("questions") or {}).get("active"))
        is_open = sid not in archived and (age <= DSH_ACTIVE_S or ((turn_open or asking) and age <= DSH_TURN_S))
        state = ("blocked" if asking else "working" if turn_open else "idle") if is_open else "done"
        parent_sid, child_label = parents.get(sid, (None, None))
        parent_id = "deepseek:" + parent_sid if parent_sid else None
        sub_label = ((rows.get("subagent") or {}).get("identity") or {}).get("label")
        # a subagent's own title row is drawn from its prompt; the label its parent gave it reads better
        own = rows.get("title")
        title = str(((child_label or sub_label or own) if parent_id else own) or "").strip()
        model = ((rows.get("modelSelection") or {}).get("lastUsed") or {}).get("model")
        totals = (rows.get("tokenUsage") or {}).get("totals") or {}
        label = f"DeepSeek · {project} · {sid[-6:]}"
        if parent_id:
            edges[(parent_id, agent, "spawn")] = {"count": 1, "lastAt": mtime}
        transcript = transcripts.get(sid, (None, 0.0))[0]
        if transcript is not None:
            ev, oc, sh = _dsh_calls(agent, cwd, dsh_records(transcript), is_open, nodes, edges)
            events.extend(ev)
            open_calls.update(oc)
            ships.extend(sh)
        nodes[agent] = {"kind": "subagent" if parent_id else "agent", "label": label, "name": label,
                        "parent": parent_id, "provider": "deepseek", "domain": "deepseek", "space": "deepseek",
                        "environment": "local", "title": title or None, "state": state, "model": model,
                        "turns": (rows.get("sessionStats") or {}).get("turns"),
                        "tokens": sum(v for v in totals.values() if isinstance(v, (int, float))) or None,
                        "cwd": cwd, "mtime": activity, "open": is_open}
    # a child whose parent fell out of the window still hangs off a stub, as the other runtimes do
    for node in list(nodes.values()):
        parent = node.get("parent")
        if parent and parent not in nodes:
            nodes[parent] = {"kind": "agent", "label": f"DeepSeek · {parent[-6:]}", "provider": "deepseek",
                             "domain": "deepseek", "space": "deepseek", "environment": "local",
                             "mtime": node["mtime"], "open": node["open"], "_parentStub": True}
    activity = max((n["mtime"] for n in nodes.values() if n.get("kind") in ("agent", "subagent")), default=0.0)
    for path in [p for p in _DSH_TAILS if p.parent.parent.parent == root / "sessions"]:
        if path.parent.name not in sessions:
            _DSH_TAILS.pop(path, None)   # Sessions that left the window stop holding decoded records
    return {"agent": "deepseek", "provider": "deepseek", "nodes": nodes, "edges": edges, "open": open_calls,
            "events": events, "ships": ships[-SHIP_EVENTS_TOTAL:], "activity": activity,
            "activeWindow": DSH_ACTIVE_S}


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
        self._snapshot = {"nodes": {}, "edges": [], "generatedAt": None, "ships": [], "jevs": []}
        self._events = []
        self._ships = []    # newest first, at most SHIP_EVENTS_TOTAL
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
        conversations = ANTIGRAVITY_DIR / "conversations"
        if conversations.is_dir():
            summaries = None
            for path in conversations.glob("*.db"):
                seen.add(path)
                try:
                    stats = [p.stat() for p in (path, path.with_name(path.name + "-wal")) if p.exists()]
                except OSError:
                    continue
                newest = max((st.st_mtime for st in stats), default=0.0)
                if now - newest > GRAPH_WINDOW_S:
                    self._files.pop(path, None)
                    continue
                present = antigravity_present(path.stem)
                # the presence lock and the "recent" window change with no write to the database,
                # so both are part of the cache key
                key = (tuple((st.st_mtime, st.st_size) for st in stats), present,
                       now - newest <= ANTIGRAVITY_ACTIVE_S)
                cached = self._files.get(path)
                if cached and cached[0] == key:
                    continue
                if summaries is None:
                    summaries = antigravity_summaries()
                parsed = parse_antigravity_db(path, now, summaries.get(path.stem), present)
                if parsed is not None:
                    self._files[path] = (key, parsed)
        dsh_cache = DSH_DIR / "storages" / "session_projcache"
        if dsh_cache.is_dir():
            key = dsh_key(DSH_DIR, now)
            cached = self._files.get(dsh_cache)
            if not cached or cached[0] != key:
                parsed = parse_dsh(DSH_DIR, now)
                if parsed["nodes"]:
                    self._files[dsh_cache] = (key, parsed)
                else:
                    self._files.pop(dsh_cache, None)
        else:
            self._files.pop(dsh_cache, None)
        for path in list(self._files):
            if (PROJECTS_DIR in path.parents or CODEX_SESSIONS_DIR in path.parents or
                    PI_SESSIONS_DIR in path.parents or conversations in path.parents) and path not in seen:
                self._files.pop(path, None)
        self._publish(now, load_rosters(now))

    def _publish(self, now, rosters=()):
        nodes, edges, events, ships = {}, {}, [], []
        for _, parsed in self._files.values():
            events.extend(parsed.get("events", []))
            ships.extend(parsed.get("ships", []))
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
        # A roster can fold an agent's extra sessions into one node; their ships follow it there.
        aliases = {alias: a["sessionId"] for roster in rosters for a in roster.get("agents") or []
                   if isinstance(a, dict) and a.get("sessionId") for alias in a.get("sessions") or []}
        doc_groups = {}
        for node in nodes.values():
            if node.get("kind") == "doc" and node.get("group"):
                doc_groups[node["group"]] = doc_groups.get(node["group"], 0) + 1
        # Newest first. Steps of one command share a time, so flip first and let the stable sort keep
        # them latest-step-first (a PR above the push it followed).
        ships.reverse()
        ships.sort(key=lambda s: s["at"], reverse=True)
        # Copies, so the per-file cache keeps its events untouched between publishes.
        ships = [{**s, "agent": aliases.get(s["agent"], s["agent"]),
                  "target": ship_target(s.get("folder"), doc_groups)} for s in ships[:SHIP_EVENTS_TOTAL]]
        jevs = jev_rows([parsed for _, parsed in self._files.values()], now, aliases)
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
            "ships": ships,
            "jevs": jevs,       # newest first; see jev_rows
        }
        with self._lock:
            self._snapshot = snapshot
            self._events = events[-FLOW_EVENTS_TOTAL:]
            self._ships = ships

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
