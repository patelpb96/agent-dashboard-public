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
import shlex
import threading
import time
from datetime import datetime
from pathlib import Path, PurePath

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
SHELL_RUN_TOOLS = {"Bash", "PowerShell", "exec", "exec_command", "shell", "powershell", "bash", "run_command"}
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
    ships = []          # commits, pushes and PRs this agent ran, judged once their result is in
    pending_ships = {}  # tool_use id -> (steps, at) for a shell call that may ship, until its result
    cwd = None          # every Claude transcript line records the folder the agent was working in
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
            "events": events[-EVENTS_PER_FILE:], "ships": ships[-SHIP_EVENTS_TOTAL:]}


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
                        finish_ship(payload.get("call_id") or payload.get("tool_call_id"), payload.get("output"), at)
                elif kind in ("custom_tool_call", "function_call", "tool_use"):
                    args, raw_text = _codex_args(entry)
                    add_call(entry.get("name") or entry.get("tool_name") or "", args, raw_text,
                             at, entry.get("call_id") or entry.get("id"))
                elif kind in ("custom_tool_call_output", "function_call_output", "tool_result"):
                    calls = open_calls.pop(entry.get("call_id") or entry.get("tool_call_id"), None) or []
                    for call in calls:
                        if call["at"] and at:
                            call["ms"] = max(0, int((at - call["at"]) * 1000))
                    finish_ship(entry.get("call_id") or entry.get("tool_call_id"), entry.get("output"), at)
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
            "open": open_calls, "events": events[-EVENTS_PER_FILE:], "ships": ships[-SHIP_EVENTS_TOTAL:]}


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
                    pending = pending_ships.pop(message.get("toolCallId"), None)
                    if pending:
                        text = _result_text(message.get("content"))
                        ships.extend(ship_events(agent, message.get("toolCallId"), pending[0], text, at or pending[1],
                                                 bool(message.get("isError")) or _exit_failed(text)))

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
            "open": open_calls, "events": events[-EVENTS_PER_FILE:], "ships": ships[-SHIP_EVENTS_TOTAL:]}


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
        self._snapshot = {"nodes": {}, "edges": [], "generatedAt": None, "ships": []}
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
        for path in list(self._files):
            if (PROJECTS_DIR in path.parents or CODEX_SESSIONS_DIR in path.parents or
                    PI_SESSIONS_DIR in path.parents) and path not in seen:
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
