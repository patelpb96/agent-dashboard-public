# Agent Dashboard

A local web dashboard for the coding agents running on your machine. It reads the session transcripts
that Claude Code, Codex and Pi already write to disk, and draws them as a rotatable 3D graph: each
agent is a node, linked to the files, web tools, MCP servers and other agents it touches. Around the
graph it shows live usage and cost, which agents are waiting on you, and a terminal view of any
background job.

- **Read-only and local.** The server binds to `127.0.0.1`, only reads files, and sends nothing
  anywhere. The page loads xterm.js, marked, DOMPurify and Google Fonts from public CDNs.
- **No dependencies.** The server is plain Python standard library.
- **Six themes:** Ember, Walnut, Space, 8-Bit, Paper (black on white) and Ink (white on charcoal).

## Requirements

- Python 3.10 or newer
- A modern browser
- At least one supported agent with sessions on this machine:
  | Agent | Where it is read from |
  |---|---|
  | Claude Code | `~/.claude/projects`, `~/.claude/jobs`, `~/.claude/sessions` |
  | Codex CLI | `~/.codex/sessions` |
  | Pi | `~/.pi/agent/sessions` |
- Optional: the `claude` CLI on `PATH`, for the live log view and for `newsroom.py` live runs

## Install

```bash
git clone https://github.com/<your-org>/agent-dashboard.git
cd agent-dashboard
```

Nothing else to install. To check the copy works:

```bash
python -m unittest discover -p "test_*.py"
```

## Run

```bash
python server.py --open
```

That opens `http://127.0.0.1:8765/`. There are also launchers that do the same thing: `agents.cmd` on
Windows and `./agents.sh` on macOS/Linux. Put the folder on your `PATH` to run `agents` from anywhere.

If you restart the server, hard-refresh the tab (Ctrl+Shift+R) so the browser loads the new `plane.js`.

## Configuration

Everything is set with environment variables. All of them are optional.

| Variable | Default | What it does |
|---|---|---|
| `AGENT_DASH_PORT` | `8765` | Port to listen on |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code config folder (Claude Code uses the same variable) |
| `CODEX_HOME` | `~/.codex` | Codex home folder (Codex uses the same variable) |
| `AGENT_DASH_PI_SESSIONS` | `~/.pi/agent/sessions` | Pi session transcripts |
| `AGENT_DASH_ROSTER_DIR` | `<CLAUDE_CONFIG_DIR>/agent-roster` | Roster files (below) |
| `AGENT_DASH_SHARED_DIR` | unset (off) | A folder your agents use to pass work to each other. When set, every touch inside it is drawn as one shared node instead of many file nodes |
| `AGENT_DASH_SIM_ROOT` | `~/newsroom-sim` | Workspaces for `newsroom.py` |

All of them are read in [`paths.py`](paths.py).

## Naming agents with a roster

An orchestrator can give its sessions names, roles and a reporting line by writing a JSON file to the
roster folder:

```json
{
  "agents": [
    {"sessionId": "<session uuid>", "name": "Lead", "role": "planner", "model": "opus",
     "tier": 0, "reportsTo": null},
    {"sessionId": "<session uuid>", "name": "Scout", "role": "researcher", "model": "haiku",
     "tier": 1, "reportsTo": "<lead session uuid>"}
  ],
  "events": [{"from": "<session uuid>", "to": "<session uuid>", "kind": "message", "at": 1767268800}]
}
```

`reportsTo` draws a manager → report edge, and an event (`at` in Unix seconds) makes that edge pulse.

## Demo workload: newsroom.py

`newsroom.py` fills the graph so you can see the dashboard working. It runs a pretend newspaper
(editor → senior editors → reporters).

```bash
python newsroom.py --mock --reporters 40   # synthetic transcripts, no API calls, free
python newsroom.py --reporters 2           # live: 4 real Claude Code sessions with a per-call dollar cap
python newsroom.py --clean                 # remove the mock transcripts, workspaces and rosters
```

Live mode needs the `claude` CLI and costs real usage. Mock mode costs nothing.

## Publishing your own copy

To share this with a group, put it in a GitHub repository or organization:

```bash
git init -b main
git add .
git commit -m "Initial public release"
gh repo create <your-org>/agent-dashboard --public --source . --push
```

Members then install it with the `git clone` command above, and update with `git pull`.

## Project layout

| File | What it is |
|---|---|
| `server.py` | HTTP server and JSON API (`/api/jobs`, `/api/graph`, `/api/flow`, `/api/waits`, `/api/logs`) |
| `graph.py` | Builds the agent graph from the transcripts |
| `metrics.py` | Token, cost and model usage |
| `waits.py` | How long agents have been waiting on you |
| `paths.py` | Every folder the dashboard reads, plus the environment overrides |
| `index.html`, `plane.js` | The page and the 3D view |
| `newsroom.py` | Demo workload |
| `test_*.py` | Unit tests (`python -m unittest discover -p "test_*.py"`) |

## License

MIT. See [LICENSE](LICENSE).
