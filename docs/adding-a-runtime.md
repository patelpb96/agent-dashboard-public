# Adding a runtime: make another agent or harness show up on the dashboard

This guide is written so a coding agent can follow it top to bottom. It covers two ways to get an agent, harness
or CLI onto the plane, how to test each, and a definition of done you can check yourself.

Terms used below:

- **runtime**: one kind of agent (Claude Code, Codex, your harness). It gets one chip in the filter bar, one
  colour, one patch on the plane. Its **id** is lowercase letters and digits joined by single hyphens, at most
  40 characters (`my-harness`).
- **session**: one run of that runtime. It is drawn as one agent node.
- **feed**: a folder of JSON-lines files the dashboard reads, one file per session (`docs/feed-format.md`).
- **native adapter**: Python code in `graph.py` that reads files the harness already writes.

## 1. Decide: feed or native adapter

Use the **feed** (Path A) when any of these is true:

- You control the harness's code (it is yours, or it has a plugin API you can write to).
- The harness can run a command on each tool call (a hook, plugin or callback mechanism).
- You can wrap the CLI in a script.
- You are not contributing to this repository.

Use a **native adapter** (Path B) only when all of these are true:

- The harness already writes its sessions to disk, in a format you can read without running it.
- You cannot or do not want to change or hook the harness.
- You are contributing the adapter to this repository (it needs code review and tests here).

**Prefer the feed.** It needs no change to this repository, it works for any language, the dashboard reads it with
the same safety rules for every writer, and a format change in the harness cannot break the dashboard. A native
adapter has to track a format someone else owns and is maintained here forever.

If unsure, use the feed.

## 2. Path A: write a feed

### 2.1 The minimum

Three calls make a runtime with one session and one tool call. Run them from the root of this repository (or
give the full path to `feed.py`):

```sh
python feed.py --runtime my-harness describe --label "My Harness" --color "#3fa7d6"
python feed.py --runtime my-harness --session test-1 session --name "Smoke test" --cwd .
python feed.py --runtime my-harness --session test-1 tool --name Bash --target README.md
```

- `describe` writes `<root>/my-harness/runtime.json` (the chip's label and colour). It is optional: without it the
  label is the id in title case (`My Harness`) and the page picks a colour from the id.
- `session` names the session. `--cwd` is made absolute; relative paths in later lines are taken against it.
- `tool` records one tool call and the file path or URL it acted on.
- `<root>` is `AGENT_DASH_FEED_DIR`, default `~/.agent-dashboard/feeds`. `--root DIR` overrides it per call.

The other event types are `file` (`--path`, `--op read|write|edit`), `ship` (`--kind commit|push|pr`, `--ref`,
`--failed`) and `end`. The field table, caps and privacy rule are in [feed-format.md](feed-format.md); do not
duplicate them, link them.

Rules your writer must follow:

- Runtime id: `[a-z0-9]+(-[a-z0-9]+)*`, at most 40 characters. Session id: the same plus single underscores, at most
  128 characters. `feed.py` refuses anything else (exit code 1, nothing written). If your harness's session ids
  do not fit, map them, for example in Python:
  `re.sub(r"[^a-z0-9]+", "-", raw.lower())[:128].strip("-") or "session"`.
- A runtime id that is already a built-in runtime or one of its other spellings (`claude`, `codex`, `openai`,
  `pi`, `antigravity`, `deepseek`, ...) adds your sessions to that runtime instead of making a new one.
- Never pass message text, prompts, tool input or tool output. `--target` is a path or URL, not a command line.
- A session is drawn as open while its newest line is under 6 minutes old and no `end` came after it. Any line
  counts, so a long run with no tool calls can repeat its `session` line every few minutes to stay open.
- Write `end` when the session finishes, so it leaves the plane at once.

### 2.2 Wiring it into a harness

Pick the integration point your harness has. Do not guess hook names or config keys: read your harness's own
documentation for its hook or plugin mechanism.

**A per-tool-call hook that runs a command.** If the harness can run a shell command before or after each tool
call and tells it the tool name (and, for file tools, the path), the hook runs:

```sh
python /path/to/agent-dashboard-public/feed.py --runtime my-harness --session "$SESSION_ID" tool --name "$TOOL_NAME" --target "$FILE_PATH"
```

`$SESSION_ID`, `$TOOL_NAME` and `$FILE_PATH` stand for whatever your harness's hook mechanism provides; map the
session id to a valid one first (2.1). Omit `--target` when there is no path or URL. Add a `session` call when a
session starts and an `end` call when it stops, if the harness has hooks for those. Each call starts Python once
(tens of milliseconds); if that is too slow for your hook, use the Python import below from a long-lived process.

**A wrapper script around a CLI.** If the CLI has no hook mechanism, a wrapper can at least show the run as
present. It cannot see tool calls unless the CLI reports them somewhere you can read.

```sh
#!/bin/sh
FEED="python /path/to/agent-dashboard-public/feed.py --runtime my-cli"
SESSION="run-$(date +%s)"
$FEED --session "$SESSION" session --name "my-cli run" --cwd "$PWD"
my-cli "$@"
status=$?
$FEED --session "$SESSION" end
exit $status
```

Runs longer than 6 minutes need a `session` line repeated in the background to stay open (2.1).

**A Python harness.** Import the writer. `feed.py` uses only the standard library, and can be imported from this
repository's folder or copied next to your code on its own (it then reads `AGENT_DASH_FEED_DIR` itself):

```python
from feed import FeedWriter, describe_runtime

describe_runtime("my-harness", label="My Harness", color="#3fa7d6")
w = FeedWriter("my-harness", "test-1")          # root=None: AGENT_DASH_FEED_DIR or ~/.agent-dashboard/feeds
w.session(name="Smoke test", model="my-model", cwd=".")
w.tool("Bash", target="README.md")
w.file("README.md", "read")
w.ship("push", ref="main")                      # ok=False for one that failed
w.end()
```

Each call appends one line under a lock, so several processes may write to one session. Bad ids or values raise
`ValueError` and write nothing.

**Any other language.** Append one JSON object per line to `<root>/<runtime>/<session>.jsonl` as described in
[feed-format.md](feed-format.md). Keep each line under 4096 bytes, and write each line with a single append so two
writers never interleave.

### 2.3 Verify a feed

Use a temporary feed folder, a temporary state folder and a spare port, so nothing touches a dashboard that is
already running (it uses port 8765). From the root of this repository, in a POSIX shell (Git Bash works on
Windows):

```sh
export AGENT_DASH_FEED_DIR="$(mktemp -d)" AGENT_DASH_STATE_DIR="$(mktemp -d)" AGENT_DASH_PORT=8802
python feed.py --runtime my-harness describe --label "My Harness" --color "#3fa7d6"
python feed.py --runtime my-harness --session test-1 session --name "Smoke test" --cwd .
python feed.py --runtime my-harness --session test-1 tool --name Bash --target README.md
python server.py &
python -c "
import json, time, urllib.request
for _ in range(90):
    d = json.load(urllib.request.urlopen('http://127.0.0.1:8802/api/graph'))
    if d['generatedAt']: break
    time.sleep(1)
print([r for r in d['runtimes'] if r['id'] == 'my-harness'])
print([n for n in d['nodes'] if n['id'] == 'feed:my-harness:test-1'])
"
kill %1
```

In Windows PowerShell:

```powershell
$env:AGENT_DASH_FEED_DIR = (New-Item -ItemType Directory -Force (Join-Path $env:TEMP "feed-check")).FullName
$env:AGENT_DASH_STATE_DIR = (New-Item -ItemType Directory -Force (Join-Path $env:TEMP "feed-check-state")).FullName
$env:AGENT_DASH_PORT = "8802"
python feed.py --runtime my-harness describe --label "My Harness" --color "#3fa7d6"
python feed.py --runtime my-harness --session test-1 session --name "Smoke test" --cwd .
python feed.py --runtime my-harness --session test-1 tool --name Bash --target README.md
$server = Start-Process python -ArgumentList "server.py" -PassThru -WindowStyle Hidden
python -c "
import json, time, urllib.request
for _ in range(90):
    d = json.load(urllib.request.urlopen('http://127.0.0.1:8802/api/graph'))
    if d['generatedAt']: break
    time.sleep(1)
print([r for r in d['runtimes'] if r['id'] == 'my-harness'])
print([n for n in d['nodes'] if n['id'] == 'feed:my-harness:test-1'])
"
Stop-Process -Id $server.Id
```

Expected output, two lines:

- `[{'id': 'my-harness', 'label': 'My Harness', 'color': '#3fa7d6', 'aliases': [], 'order': 5}]`
  (`order` counts up from the five built-in runtimes.)
- One agent node with `'domain': 'my-harness'`, `'label': 'Smoke test'`, `'open': True`.

An empty list on the first line means the folder was not found or the runtime id is invalid. An empty list on the
second means the session file was skipped: check the session id, that each line has `"v": 1`, `"t"` and `"type"`,
and that the file is a plain file (not a symlink or hard link).

The server reads the agent sessions on your machine too, so the first refresh can take some seconds; the check
waits up to 90.

For the browser check, leave the server running, open `http://127.0.0.1:8802/` and check that the **Agents** bar
has a "My Harness" chip with its colour and count, and that an agent named "Smoke test" is drawn on the plane.
After restarting the server, hard-refresh the page (Ctrl+Shift+R).

## 3. Path B: a native adapter

A native adapter is a parser plus one `register_runtime(Runtime(...))` call in `graph.py`. The page needs no
change: `/api/graph` sends every registered runtime's id, label, colour, aliases and order in `runtimes`, and the
page uses that for the chip, the colour, the order and the patch on the plane. A runtime the page has no logo for
gets a plain square glyph.

### 3.1 The Runtime interface

`graph.Runtime` (search `class Runtime` in `graph.py`; its docstring is the reference):

```python
Runtime(id, label, color, *, roots, find, parse, key=file_key, aliases=(), order=None, source=None)
```

- `id`: the runtime id. `label`: 1 to 40 characters. `color`: `#rrggbb`, or `None` to let the page pick one.
- `roots()`: the folders to read, as a function, so environment overrides and test patches apply at refresh time.
- `find(root)`: the units under one root that is a folder: usually one `Path` per session file.
- `key(unit, now)`: a cache key; the unit is parsed again only when it changes. Return `None` when the unit can't be
  read right now (keep the cached parse), `graph.DROP` to forget it. The default, `file_key`, is the file's
  `(mtime, size)`, and `DROP` once it is older than `GRAPH_WINDOW_S` (6 hours).
- `parse(unit, key, now, ctx)`: the bundle (3.3), `None` to keep the older parse, or `DROP`. `ctx` is a dict shared
  by one refresh pass of this runtime.

Every registered runtime is read on each refresh (every 3 seconds) by `GraphStore.refresh()`.

### 3.2 Which parser to copy

Copy the structure of **`parse_feed_file`** in `graph.py`. It is the newest and smallest parser, it returns the
standard bundle, and it already follows the untrusted-input rules below through small helpers you can call:
`_feed_open` (opens a plain, single-link file and refuses links), `_feed_text` (caps a string and drops control
characters) and `_feed_doc_target` (a file node from the path text alone, never touching the disk).

For reading a third-party JSON-lines transcript, with tool calls matched to their results by id and a tail read of
large files, look at **`parse_pi_file`**. Do not copy its use of `document_target` or `path_target_from_input`:
they call `resolve()` on paths named in the data. New code uses `_feed_doc_target`.

### 3.3 The bundle a parser returns

```python
{
    "agent": "<node id of the session>",
    "provider": "<runtime id>",
    "nodes": {node_id: {...}},                       # the session's agent node, plus every tool / file it touched
    "edges": {(src, dst, kind): {"count": int, "lastAt": epoch_s}},   # kind: "call", "spawn" or "message"
    "open": {},                                      # call id -> [call events] still waiting for a result
    "events": [...], "ships": [...],                 # optional: flow-view calls and ship events
    "activity": epoch_s, "activeWindow": seconds,   # optional
}
```

The agent node has `"kind": "agent"` (or `"subagent"` with `"parent": <parent node id>`), `"label"`, `"name"`,
`"provider"` and `"domain"` and `"space"` all set to the runtime id, `"environment": "local"`, `"state"`
(`working`, `idle` or `done`), `"model"`, `"cwd"`, `"mtime"` and `"open"`. Node ids must not collide with other
runtimes': prefix them with the runtime id (`"my-harness:" + session_id`). For tool and file nodes use
`target_of`, `shared_type_target` and `_feed_doc_target`, as `parse_feed_file` does, so they land on the same
nodes as other runtimes'.

### 3.4 Checklist

1. **Folder setting.** In `paths.py`, add `MY_HARNESS_DIR = _dir("AGENT_DASH_MY_HARNESS_DIR", HOME / ".my-harness")`
   and a line for it in the module docstring. Add a row to the **Configuration** table in `README.md`, and a row to
   the supported-agents table under **Requirements**.
2. **Import it** in `graph.py`'s `from paths import (...)` line, so tests can patch `graph.MY_HARNESS_DIR`.
3. **Parser.** Add `parse_my_harness_file(path, now=None)` next to the other parsers in `graph.py`, returning the
   bundle in 3.3 (or `None` if the file can't be read).
4. **Register it** after the built-in registrations in the Runtime section:

   ```python
   register_runtime(Runtime(
       "my-harness", "My Harness", "#3fa7d6",
       roots=lambda: [MY_HARNESS_DIR], find=lambda root: root.rglob("*.jsonl"),
       parse=lambda path, key, now, ctx: parse_my_harness_file(path, now)))
   ```

5. **Open state.** If your parser decides `open` itself (from timestamps in its own files), add the id to
   `SELF_TRACKED` in `server.py`. Otherwise the server marks the agent open only if Claude Code's session list
   names it, which is never true for another harness. Parsers that decide `open` from time also need the time in
   the cache key, or a session stays open until its file changes: see `_antigravity_key` and `_feed_key`.
6. **Tests.** Add `test_my_harness.py` from the template in section 4, with a small fixture written in the test.
   The existing tests read the list of registered runtimes from `graph.RUNTIMES`, so they need no change.
7. **Privacy.** Read only what the graph draws: ids, names, model, working folder, tool names, the file paths and
   URLs tools acted on, timestamps. Never keep message text, prompts, tool input or output. A test must prove a
   sentinel string in those fields does not reach the snapshot.
8. **Untrusted input.** Anything on the machine can write the files you read:
   - read a capped tail of each file (as `FEED_TAIL_BYTES`), skip lines over a cap, cap every string;
   - do not follow symlinks or junctions: `_feed_open` and `_plain_dir` show how;
   - never open, stat or resolve a path named inside the data; such paths are labels;
   - skip malformed lines; never let one bad file raise out of `parse` (catch, and return `DROP`).

## 4. Test template

Copy this to `test_my_harness.py`, rename `my_harness` / `MY_HARNESS` / `my-harness`, and replace `write_session`
with your harness's real on-disk format. The fixture rows below are a placeholder format: one JSON object per
line with `"tool"`, plus fields that must never be read.

```python
"""Tests for the My Harness runtime. Run with: python -m unittest test_my_harness -v"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import graph

SECRET = "SECRET-MESSAGE-TEXT"   # must never reach the graph


class MyHarnessTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.root = self.dir / "my-harness"
        self.root.mkdir()
        self.now = time.time()
        empty = self.dir / "none"
        # Every other runtime reads a missing folder, so only this one shows up.
        patch = mock.patch.multiple(graph, PROJECTS_DIR=empty, CODEX_SESSIONS_DIR=empty, PI_SESSIONS_DIR=empty,
                                    ANTIGRAVITY_DIR=empty, DSH_DIR=empty, FEED_DIR=empty, ROSTER_DIR=empty,
                                    MY_HARNESS_DIR=self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def write_session(self, session_id, rows, raw=()):
        # REPLACE: write a fixture in your harness's own format.
        path = self.root / f"{session_id}.jsonl"
        lines = [json.dumps({"t": self.now - 5, **row}) for row in rows] + list(raw)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def snapshot(self):
        store = graph.GraphStore.__new__(graph.GraphStore)   # no background thread in tests
        store._files, store._lock, store._snapshot, store._events, store._ships = {}, threading.Lock(), None, [], []
        store.refresh()
        return store.snapshot()

    def test_parser_returns_the_standard_bundle(self):
        path = self.write_session("s1", [{"tool": "Bash"}])
        bundle = graph.parse_my_harness_file(path, self.now)
        agent = bundle["nodes"][bundle["agent"]]
        self.assertEqual((agent["kind"], agent["provider"], agent["domain"]), ("agent", "my-harness", "my-harness"))
        self.assertTrue({"nodes", "edges", "open"} <= set(bundle))

    def test_runtime_is_listed_and_its_session_drawn(self):
        self.write_session("s1", [{"tool": "Bash"}, {"tool": "Grep"}])
        snap = self.snapshot()
        self.assertIn("my-harness", [r["id"] for r in snap["runtimes"]])
        agents = [nid for nid, n in snap["nodes"].items() if n.get("domain") == "my-harness" and n["kind"] == "agent"]
        self.assertEqual(len(agents), 1)
        tools = {e["dst"] for e in snap["edges"] if e["src"] == agents[0]}
        self.assertTrue({"tool:Bash", "tool:Grep"} <= tools, tools)

    def test_message_text_is_never_kept(self):
        self.write_session("s1", [{"tool": "Bash", "text": SECRET, "prompt": SECRET, "output": SECRET}])
        self.assertNotIn(SECRET, json.dumps(self.snapshot(), default=str))

    def test_malformed_lines_are_skipped(self):
        self.write_session("s1", [{"tool": "Bash"}], raw=["not json", "[1, 2]", "{" * 3000])
        snap = self.snapshot()
        self.assertTrue(any(n.get("domain") == "my-harness" for n in snap["nodes"].values()))


if __name__ == "__main__":
    unittest.main()
```

Add tests for anything your parser decides: open and closed sessions, subagents, ship events, and a fixture larger
than your tail cap.

## 5. Definition of done

Feed (Path A):

- [ ] The verification in 2.3 prints your runtime in `runtimes` and your session's agent node.
- [ ] In the browser at `http://127.0.0.1:8802/`, your runtime's chip is in the **Agents** bar and your session's
      agent is drawn on the plane.
- [ ] Your harness writes `session`, `tool` (and `file`, `ship`, `end` where it can) during a real run, and no
      message text.

Native adapter (Path B), all of the above with your own folder instead of a feed, plus:

- [ ] `python -m unittest discover -p "test_*.py"` passes, including your new test file.
- [ ] `node --test filters.test.js jev.test.js runtimes.test.js` passes.
- [ ] `README.md` has your folder in the **Requirements** table and your environment variable in the
      **Configuration** table; `paths.py`'s docstring lists the variable.
- [ ] Your id is in `SELF_TRACKED` in `server.py` if your parser decides `open`.

## 6. What not to do

- Do not keep or send message text, prompts, tool input or tool output, in a feed or a parser.
- Do not make network calls. The dashboard only reads local files.
- Do not add dependencies. The server and `feed.py` are Python standard library only; the page loads nothing new.
- Do not edit the five built-in parsers (`parse_file`, `parse_codex_file`, `parse_pi_file`, `parse_antigravity_db`,
  `parse_dsh`) to add a runtime.
- Do not hard-code your runtime in `filters.js`, `plane.js` or `index.html`. Its label, colour, aliases and order
  come from the `runtimes` metadata in `/api/graph`.
- Do not use port 8765 for testing: that is where the user's dashboard runs.
