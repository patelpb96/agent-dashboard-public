"""Local read-only dashboard for Claude Code background agents.

Reads ~/.claude/jobs/*/state.json (+ timeline.jsonl tail) and ~/.claude/sessions/*.json.
Stdlib only. Binds to 127.0.0.1. Run:  python server.py  (then open http://127.0.0.1:8765)
"""
import json
import os
import sys
import re
import shutil
import time
import subprocess
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import waits
from graph import GraphStore
from metrics import Metrics
from paths import CLAUDE_DIR, PROJECTS_DIR, SHARED_DIR

JOBS_DIR = CLAUDE_DIR / "jobs"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("AGENT_DASH_PORT", "8765"))
TIMELINE_LINES = 12
TEXT_LIMIT = 400
LOG_BYTES = 300 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
METRICS = Metrics()
GRAPH = GraphStore()
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
JSON = "application/json; charset=utf-8"
STATIC = {"/space.jpg": ("space.jpg", "image/jpeg"), "/plane.js": ("plane.js", "application/javascript; charset=utf-8")}


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def tail_timeline(path, n=TIMELINE_LINES):
    """Read only the end of timeline.jsonl instead of the whole file."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 96 * 1024))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-n:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # first line may be partial after seek
        text = entry.get("text") or ""
        out.append({
            "at": entry.get("at"),
            "state": entry.get("state"),
            "detail": entry.get("detail") or "",
            "text": text[:TEXT_LIMIT] + ("…" if len(text) > TEXT_LIMIT else ""),
        })
    return out


def auth_status():
    """Return the daemon auth status, ignoring it if a later refresh succeeded.

    daemon-auth-status.json is only rewritten when auth fails, so it can stay at
    auth_required after the daemon recovers. daemon.log records the recovery.
    """
    status = read_json(CLAUDE_DIR / "daemon-auth-status.json")
    if not status or status.get("status") != "auth_required":
        return status
    since = status.get("since") or 0
    recovered = last_auth_recovery(CLAUDE_DIR / "daemon.log")
    if recovered and recovered > since:
        return None
    return status


def last_auth_recovery(path):
    """Epoch ms of the newest 'proactive refresh succeeded' / 'token found' line, or None."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 256 * 1024))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    latest = None
    for line in lines:
        if "[supervisor] auth:" not in line:
            continue
        if "proactive refresh succeeded" not in line and "token found" not in line:
            continue
        try:
            iso = line[1:line.index("]")].replace("Z", "+00:00")
            ms = int(datetime.fromisoformat(iso).timestamp() * 1000)
        except ValueError:
            continue
        latest = max(latest or 0, ms)
    return latest


def model_from_flags(flags):
    if not flags:
        return None
    for i, flag in enumerate(flags[:-1]):
        if flag == "--model":
            return flags[i + 1]
    return None


def live_sessions():
    """Map jobId -> session status for sessions whose metadata file exists."""
    live = {}
    if not SESSIONS_DIR.is_dir():
        return live
    for path in SESSIONS_DIR.glob("*.json"):
        data = read_json(path)
        if data and data.get("jobId"):
            live[data["jobId"]] = {"status": data.get("status"), "pid": data.get("pid")}
    return live


def last_assistant_text(path, limit=300):
    """Most recent assistant text in a transcript, read from the file tail only."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 64 * 1024))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        msg = entry.get("message") or {}
        if entry.get("type") != "assistant" or not isinstance(msg.get("content"), list):
            continue
        text = "\n".join(part.get("text", "") for part in msg["content"] if part.get("type") == "text").strip()
        if text:
            return text[:limit] + ("…" if len(text) > limit else "")
    return ""


def subagents_by_session():
    """Subagents keyed by parent session id, from projects/<project>/<session>/subagents/."""
    out = {}
    if not PROJECTS_DIR.is_dir():
        return out
    for meta_path in PROJECTS_DIR.glob("*/*/subagents/*.meta.json"):
        jsonl = meta_path.with_name(meta_path.name.replace(".meta.json", ".jsonl"))
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        meta = read_json(meta_path) or {}
        info = {
            "id": jsonl.stem,
            "type": meta.get("agentType") or "agent",
            "description": meta.get("description") or "",
            "depth": meta.get("spawnDepth") or 1,
            # A subagent whose transcript was written in the last 90 s is still going.
            "status": "running" if time.time() - mtime < 90 else "finished",
            "updatedAt": datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds"),
            "lastText": last_assistant_text(jsonl),
        }
        out.setdefault(meta_path.parent.parent.name, []).append(info)
    for items in out.values():
        items.sort(key=lambda a: a["updatedAt"], reverse=True)
    return out


def session_usage(state):
    """Cost and tokens for one agent, from its transcript totals (matched by session id)."""
    snap = METRICS.snapshot() or {}
    sessions = snap.get("sessions", {})
    for sid in (state.get("sessionId"), state.get("resumeSessionId")):
        if not sid:
            continue
        for key in (f"anthropic:{sid}", sid):
            if key in sessions:
                return sessions[key]
    return None


def agent_log(agent_id):
    """Recent terminal output for one agent, via the supported `claude logs` command."""
    exe = shutil.which("claude")
    if not exe or not ID_RE.match(agent_id):
        return None, "claude CLI not found" if not exe else "bad id"
    try:
        out = subprocess.run([exe, "logs", agent_id], capture_output=True, timeout=8)
    except subprocess.TimeoutExpired:
        return None, "timed out reading logs"
    if out.returncode != 0:
        return None, out.stderr.decode("utf-8", errors="replace").strip() or "no logs"
    return out.stdout[-LOG_BYTES:], None


SUBAGENT_LIVE_S = 90   # a subagent whose transcript moved this recently is still going
SUBAGENT_BUSY_S = 30 * 60  # one mid-turn (waiting on a tool or the model) counts as going this long


def pid_alive(pid):
    """Is this process still running? (session files can outlive a crashed process)"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def open_session_ids():
    """Session ids of Claude Code processes that are running right now."""
    out = set()
    if SESSIONS_DIR.is_dir():
        for path in SESSIONS_DIR.glob("*.json"):
            data = read_json(path)
            if data and data.get("sessionId") and pid_alive(data.get("pid")):
                out.add(data["sessionId"])
    return out


def mark_open(nodes, open_ids, now):
    """`open` on every agent: a live session, a running orchestrated run, or a recent subagent of an open one."""
    by_id = {n["id"]: n for n in nodes}
    for n in nodes:
        if n["kind"] == "agent":
            if n.get("provider") in ("codex", "pi"):
                n["open"] = bool(n.get("open"))
            else:
                n["open"] = n["id"] in open_ids or bool(n.get("rosterLive"))
    for n in nodes:
        if n["kind"] == "subagent":
            parent = by_id.get(n.get("parent"))
            age = now - (n.get("mtime") or 0)
            recent = age <= SUBAGENT_LIVE_S or (bool(n.get("busy")) and age <= SUBAGENT_BUSY_S)
            provider_active = n.get("provider") in ("codex", "pi") and bool(n.get("open"))
            if n.get("provider") in ("codex", "pi"):
                n["open"] = provider_active
            else:
                n["open"] = bool(parent and parent.get("open")) and recent
            if n["open"] and parent:
                parent["open"] = True


def graph_payload():
    """Plane view data: the transcript graph, with agent nodes named after their jobs."""
    snap = GRAPH.snapshot()
    nodes = [{"id": nid, **info} for nid, info in snap["nodes"].items()]
    names = {}
    if JOBS_DIR.is_dir():
        for job_dir in JOBS_DIR.iterdir():
            state = read_json(job_dir / "state.json")
            if state and state.get("sessionId"):
                names[state["sessionId"]] = {
                    "name": state.get("name") or (state.get("intent") or "untitled")[:60],
                    "state": state.get("state") or "unknown",
                    "jobId": job_dir.name,
                }
    for node in nodes:
        if node["kind"] == "agent" and node["id"] in names:
            node.update(names[node["id"]])
            node["label"] = node["name"]
    mark_open(nodes, open_session_ids(), time.time())
    # ships: newest commits, pushes and PRs; the page beams on new ones and lists them
    return {"generatedAt": snap["generatedAt"], "nodes": nodes, "edges": snap["edges"],
            "ships": snap.get("ships", [])}


def waits_payload():
    """Wait share for every job with a timeline: who spends its time waiting, and why."""
    now = time.time()
    rows = []
    if JOBS_DIR.is_dir():
        for job_dir in JOBS_DIR.iterdir():
            entries = waits.read_entries(job_dir / "timeline.jsonl")
            if not entries:
                continue
            state = read_json(job_dir / "state.json") or {}
            summary = waits.summarize(entries, now)
            if summary["total"] == 0:
                continue
            rows.append({
                "jobId": job_dir.name,
                "name": state.get("name") or (state.get("intent") or "untitled")[:60],
                "state": state.get("state") or "unknown",
                **summary,
            })
    rows.sort(key=lambda r: -r["waiting"])
    return {"generatedAt": now, "jobs": rows[:20]}


def collect():
    sessions = live_sessions()
    subs = subagents_by_session()
    jobs = []
    if JOBS_DIR.is_dir():
        for job_dir in JOBS_DIR.iterdir():
            state = read_json(job_dir / "state.json")
            if not state:
                continue  # skip dirs with no state (e.g. tmp-only folders)
            short = job_dir.name
            result = state.get("output") or {}
            sess = sessions.get(short) or sessions.get(state.get("daemonShort") or "")
            usage = session_usage(state)
            subagents = subs.get(state.get("sessionId") or "", [])
            jobs.append({
                "id": short,
                "name": state.get("name") or (state.get("intent") or "untitled")[:60],
                "intent": state.get("intent") or "",
                "state": state.get("state") or "unknown",
                "detail": state.get("detail") or "",
                "needs": state.get("needs") or "",
                "suggestedReply": state.get("suggestedReply") or "",
                "result": result.get("result") if isinstance(result, dict) else None,
                "tempo": state.get("tempo"),
                "tokens": state.get("tokens"),
                "model": model_from_flags(state.get("respawnFlags")),
                "cwd": state.get("cwd"),
                "createdAt": state.get("createdAt"),
                "updatedAt": state.get("updatedAt"),
                "session": sess,
                "usage": usage,
                "subagents": subagents,
                "timeline": tail_timeline(job_dir / "timeline.jsonl"),
            })
    jobs.sort(key=lambda j: j.get("updatedAt") or "", reverse=True)
    return {"jobs": jobs, "daemon": read_json(CLAUDE_DIR / "daemon.status.json"),
            "authStatus": auth_status(), "metrics": METRICS.snapshot()}


def host_name(value):
    """The hostname in a Host header, without the port: "[::1]:8765" -> "::1"."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else ""
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def local_origin(origin):
    """Is this Origin header a page served from this machine (any port)?"""
    try:
        parts = urlsplit(origin)
        return parts.scheme == "http" and parts.hostname in LOOPBACK
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    def _local_host(self):
        """Refuse (421) a Host that is not loopback: a rebound DNS name still sends its own Host."""
        # The port is ignored on purpose; any loopback port is this machine.
        if host_name(self.headers.get("Host")) in LOOPBACK:
            return True
        self._send(421, JSON, b'{"error":"local requests only"}')
        return False

    def do_GET(self):
        if not self._local_host():
            return
        route, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if route == "/api/jobs":
            body = json.dumps(collect(), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif route == "/api/logs":
            data, err = agent_log(params.get("id", ""))
            if err:
                self._send(404, "text/plain; charset=utf-8", err.encode("utf-8"))
            else:
                self._send(200, "application/octet-stream", data)
        elif route in ("/", "/index.html", "/watch"):
            self._send(200, "text/html; charset=utf-8", (HERE / "index.html").read_bytes())
        elif route == "/api/graph":
            body = json.dumps(graph_payload(), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif route == "/api/flow":
            # Node ids hold colons and paths, so this query is decoded properly.
            q = parse_qs(query)
            src, dst, kind = (q.get(k, [""])[0] for k in ("src", "dst", "kind"))
            body = {
                "edge": GRAPH.flow(src, dst, kind) if dst and kind else [],
                "agent": GRAPH.agent_events(src) if src else [],
            }
            self._send(200, "application/json; charset=utf-8", json.dumps(body, ensure_ascii=False).encode("utf-8"))
        elif route == "/api/waits":
            body = json.dumps(waits_payload(), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif route in STATIC:
            name, ctype = STATIC[route]
            self._send(200, ctype, (HERE / name).read_bytes())
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        if not self._local_host():
            return
        # A text/plain POST needs no CORS preflight, so any site could send one; browsers add Origin.
        # No Origin means curl or a script, which could do anything anyway.
        origin = self.headers.get("Origin")
        if origin is not None and not local_origin(origin):
            self._send(403, JSON, b'{"error":"cross-site request refused"}')
            return
        route = self.path.partition("?")[0]
        if route != "/api/open-folder":
            self._send(404, "application/json; charset=utf-8", b'{"error":"not found"}')
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8192:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            node_id = payload.get("id") if isinstance(payload, dict) else None
        except (ValueError, UnicodeDecodeError):
            self._send(400, "application/json; charset=utf-8", b'{"error":"invalid request"}')
            return

        node = GRAPH.snapshot()["nodes"].get(node_id) if isinstance(node_id, str) else None
        if node_id == "area:shared" and SHARED_DIR and node and node.get("kind") == "area":
            folder = Path(SHARED_DIR)
        elif node and node.get("kind") == "doc" and node_id.startswith("doc:"):
            raw = node_id[4:]
            target = Path(raw)
            # Node ids come from transcripts. Opening //host/share would connect to that host
            # (and offer it your Windows login), so only local folders, before anything touches it.
            if raw.startswith(("\\\\", "//")) or not target.anchor:
                self._send(400, JSON, b'{"error":"only local folders can be opened"}')
                return
            if not target.is_absolute():
                self._send(400, "application/json; charset=utf-8", b'{"error":"file path is unavailable"}')
                return
            folder = target if target.is_dir() else target.parent
        else:
            self._send(404, "application/json; charset=utf-8", b'{"error":"file is no longer in the graph"}')
            return

        if not folder.is_dir():
            self._send(404, "application/json; charset=utf-8", b'{"error":"containing folder is unavailable"}')
            return
        try:
            if os.name == "nt":
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self._send(500, "application/json; charset=utf-8", b'{"error":"could not open folder"}')
            return
        response = json.dumps({"folder": str(folder)}, ensure_ascii=False).encode("utf-8")
        self._send(200, "application/json; charset=utf-8", response)

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the terminal quiet during polling
        pass


def main():
    url = f"http://127.0.0.1:{PORT}/"
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Agent dashboard at {url}  (Ctrl+C to stop)")
    if "--open" in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
