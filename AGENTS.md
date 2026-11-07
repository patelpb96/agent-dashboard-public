# Agent Dashboard: notes for coding agents

A local web dashboard (a Python standard-library server and a canvas page) that reads the session files
coding agents write to disk (Claude Code, Codex, Pi, Google Antigravity, DeepSeek Harness, and open feeds) and
draws them as a 3D graph of agents and the files and tools they touch.

## Run and test

```sh
python server.py --open                          # http://127.0.0.1:8765/ (AGENT_DASH_PORT changes the port)
python server.py --status                        # is it running, and how did it last stop
python -m unittest discover -p "test_*.py"       # Python tests
node --test filters.test.js jev.test.js runtimes.test.js   # page logic tests (Node 18+)
```

Port 8765 is probably the user's own running dashboard: test on another port, with `AGENT_DASH_STATE_DIR` set to
a temporary folder.

## Rules

- **Privacy (the graph APIs: `/api/graph`, `/api/flow`, `/api/waits`):** kept are ids, names and titles
  (either can be the start of a prompt: a job's name falls back to 60 characters of its intent, a title is cut
  to 200), models, folders, tool names, paths and URLs tools acted on, timestamps, and a first-line detail of each tool call (at most 160 characters, URL credentials
  removed). Never message text or tool output. Output is read in two bounded cases only: a shell call that
  commits, pushes or opens a PR (to tell whether it went through), and a Jev result (a fixed list of fields, see
  the README). `/api/jobs` and `/api/logs` serve a job's text and its terminal output by design.
- Standard library only (Python); no new scripts or styles on the page. No network calls.
- Files read from disk are untrusted: cap what is read, do not follow links, and in new code never open or stat a
  path named inside the data.

## Map

| File | What it is |
|---|---|
| `server.py` | HTTP server and JSON API (`/api/graph` and others) |
| `graph.py` | Builds the graph; every runtime is a registered `Runtime` (see its docstring) |
| `feed.py` | Writes an open feed (`docs/feed-format.md`) |
| `paths.py` | Every folder read, with its environment variable |
| `index.html`, `plane.js`, `filters.js`, `jev.js` | The page |

## Adding another agent or harness

Read [docs/adding-a-runtime.md](docs/adding-a-runtime.md). In short: write a feed (no dashboard code) unless the
harness already writes session files you must read with a native adapter.
