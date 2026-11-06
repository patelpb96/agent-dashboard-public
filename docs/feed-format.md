# Open feed format (v1)

Any harness, wrapper script or CLI hook can put its sessions on the dashboard's plane, without dashboard code, by
appending JSON lines to a file. `feed.py` writes them for you, from Python or from a shell.

## Where

```
<root>/<runtime>/<session>.jsonl     one file per session, append-only
<root>/<runtime>/runtime.json        optional: the runtime's name, colour and other spellings
```

- `<root>` is `AGENT_DASH_FEED_DIR`, default `~/.agent-dashboard/feeds`. A UNC path (`\\host\share`) turns feeds off.
- `<runtime>`: lowercase letters and digits joined by single hyphens, at most 40 characters (`opencode`,
  `my-tool`). A folder named after a runtime the dashboard already reads, or one of its other spellings
  (`claude`, `codex`, `openai`, `pi`, `antigravity`, `deepseek`, ...), adds its sessions to that runtime and keeps
  its name, colour and logo. Any other name is a new runtime.
- `<session>`: lowercase letters and digits joined by single hyphens or underscores, at most 128 characters.
- Anything else in the folder (other names, symlinks, junctions, hard-linked files) is skipped.

## Lines

Every line is one JSON object with `"v": 1`, `"t"` (epoch seconds, or ISO 8601) and `"type"`:

| type | fields | drawn as |
|---|---|---|
| `session` | `name`, `model`, `cwd`, `parent`, `role` (all optional; a later line overrides) | the agent's name, model and folder; `parent` (a session id in the same runtime folder) makes this a subagent with a spawn line from its parent |
| `tool` | `name` (required), `target` (optional file path or URL) | a line to the tool, its tool family, and the file it acted on |
| `file` | `path`, `op`: `read` \| `write` \| `edit` | a line to the file (and to the Read tool for a read) |
| `ship` | `kind`: `commit` \| `push` \| `pr`, `ref` (branch, or for a PR its `https://github.com/<o>/<r>/pull/<n>` URL), `ok` (optional, default `true`) | a ship beam and a row in the Ships window |
| `end` | none | the session is over |

Unknown types and unknown fields are ignored. Paths are labels only: the dashboard never opens, stats or resolves a
path named in a line. A relative path is taken against the session's `cwd`.

A session is **open** while its newest line is under 6 minutes old and no `end` has come after it, and working
while its newest `tool` or `file` line is under 2 minutes old. A line's time is never taken as later than the file's
last write. Sessions untouched for 6 hours drop off the plane.

`runtime.json` is `{"label": "OpenCode", "color": "#7bd88f", "aliases": ["oc"]}`, all optional: `label` 1 to 40
characters, `color` `#rrggbb` (otherwise the page picks one from the id), at most 8 aliases. An invalid field falls
back to the default (the folder name in title case).

## Privacy

There are no text fields. Message text, prompts, tool input and output are never written by `feed.py` and never
read by the dashboard: fields such as `text`, `content`, `prompt` or `output` are dropped like any unknown field. A
`target` is meant to be a path or URL, not a command line.

## Caps

The folder is untrusted: anything on the machine can write there. Per refresh the dashboard reads at most 32
runtime folders (by name), the 64 newest sessions per folder (of the first 2000 entries), the last 1 MiB and
4000 lines of each file, lines up to 4096 bytes (longer ones are skipped) and `runtime.json` up to 4 KiB. Strings
are cut to 200 characters (paths and `cwd`: 1024, `role`: 64) after control characters are removed. Malformed lines
are skipped; a file that fails to read is left out, never fatal.

## Example

```jsonl
{"v":1,"t":1793990000.0,"type":"session","name":"Fix login bug","model":"gpt-5","cwd":"/home/me/app"}
{"v":1,"t":1793990003.2,"type":"tool","name":"Grep","target":"src"}
{"v":1,"t":1793990004.0,"type":"file","path":"src/login.py","op":"read"}
{"v":1,"t":1793990011.5,"type":"file","path":"src/login.py","op":"edit"}
{"v":1,"t":1793990012.0,"type":"tool","name":"WebFetch","target":"https://docs.python.org/3/"}
{"v":1,"t":1793990020.0,"type":"tool","name":"Bash","target":"tests/test_login.py"}
{"v":1,"t":1793990031.0,"type":"ship","kind":"commit","ref":"fix-login"}
{"v":1,"t":1793990033.0,"type":"ship","kind":"push","ref":"fix-login"}
{"v":1,"t":1793990040.0,"type":"ship","kind":"pr","ref":"https://github.com/me/app/pull/42"}
{"v":1,"t":1793990041.0,"type":"end"}
```

## feed.py

```python
from feed import FeedWriter, describe_runtime
describe_runtime("opencode", label="OpenCode", color="#7bd88f", aliases=["oc"])
w = FeedWriter("opencode", "abc123")            # root=None: AGENT_DASH_FEED_DIR or ~/.agent-dashboard/feeds
w.session(name="Fix login bug", model="gpt-5", cwd=".")
w.tool("Bash", target="src/app.py")
w.file("src/app.py", "edit")
w.ship("push", ref="main")                      # ok=False for one that failed
w.end()
```

```sh
python feed.py --runtime opencode describe --label OpenCode --color "#7bd88f" --alias oc
python feed.py --runtime opencode --session abc123 session --name "Fix login bug" --model gpt-5 --cwd .
python feed.py --runtime opencode --session abc123 tool --name Bash --target src/app.py
python feed.py --runtime opencode --session abc123 file --path src/app.py --op edit
python feed.py --runtime opencode --session abc123 ship --kind push --ref main [--failed]
python feed.py --runtime opencode --session abc123 end
```

Each call appends one line under a lock, so several processes can write to one session. `--root` (or `root=`)
picks another folder. Bad ids or values exit 1 (raise `ValueError`) with a message, and nothing is written.
