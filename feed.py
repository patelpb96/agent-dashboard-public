"""Write an open feed, so any harness, wrapper script or CLI hook shows up on the dashboard's plane.

One file per session, <root>/<runtime>/<session>.jsonl, one JSON line per event (docs/feed-format.md). The root is
AGENT_DASH_FEED_DIR, default ~/.agent-dashboard/feeds. Standard library only; there are no text fields, so no
message text can be written.

From Python:
    from feed import FeedWriter
    w = FeedWriter("opencode", "abc123")
    w.session(name="Fix the login bug", model="gpt-5", cwd="/home/me/app")
    w.tool("Bash", target="src/app.py")
    w.file("src/app.py", "edit")
    w.ship("push", ref="main")
    w.end()

From a hook (one line per call):
    python feed.py --runtime opencode --session abc123 session --name "Fix login" --model gpt-5 --cwd .
    python feed.py --runtime opencode --session abc123 tool --name Bash --target src/app.py
    python feed.py --runtime opencode --session abc123 file --path src/app.py --op edit
    python feed.py --runtime opencode --session abc123 ship --kind push --ref main
    python feed.py --runtime opencode --session abc123 end
    python feed.py --runtime opencode describe --label OpenCode --color "#7bd88f" --alias oc

Appends are one write each, under a lock (flock, or a byte-range lock past the end on Windows), so two processes
can append to one session at once.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    from paths import FEED_DIR as DEFAULT_ROOT
except ImportError:   # copied on its own next to a hook
    _env = os.environ.get("AGENT_DASH_FEED_DIR")
    DEFAULT_ROOT = Path(os.path.expanduser(_env)) if _env else Path.home() / ".agent-dashboard" / "feeds"

VERSION = 1
RUNTIME_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")          # graph.RUNTIME_ID
SESSION_ID = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")       # graph.FEED_SESSION_ID
RUNTIME_ID_MAX, SESSION_ID_MAX = 40, 128
TEXT_MAX, PATH_MAX, ROLE_MAX, LABEL_MAX, ALIASES_MAX = 200, 1024, 64, 40, 8   # graph.FEED_*_MAX
LINE_MAX = 4096                                                # graph.FEED_LINE_MAX: longer lines are skipped
FILE_OPS = ("read", "write", "edit")
SHIP_KINDS = ("commit", "push", "pr")
COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_LOCK_AT = 2 ** 31 - 2   # Windows: lock one byte far past any real feed's end, so readers are never blocked


def _check_id(value, pattern, cap, what):
    if not isinstance(value, str) or len(value) > cap or not pattern.fullmatch(value):
        raise ValueError(f"{what} must be lowercase letters and digits joined by single "
                         f"{'hyphens or underscores' if pattern is SESSION_ID else 'hyphens'}, at most {cap} "
                         f"characters: {value!r}")
    return value


def _text(value, cap):
    """Plain text the reader will keep: control characters dropped, trimmed, capped. None stays None."""
    if value is None:
        return None
    value = _CONTROL.sub(" ", str(value)).strip()[:cap].strip()
    return value or None


def _root(root):
    root = Path(os.path.expanduser(str(root))) if root else Path(DEFAULT_ROOT)
    if str(root).startswith(("\\\\", "//")):
        raise ValueError("the dashboard does not read a UNC feed folder; use a local one")
    return root


def _append(path, data):
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o644)
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, _LOCK_AT, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)   # retries for about 10 s, then raises
            try:
                _write_all(fd, data)                  # O_APPEND: each write goes to the current end
            finally:
                os.lseek(fd, _LOCK_AT, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                _write_all(fd, data)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_all(fd, data):
    while data:
        data = data[os.write(fd, data):]


class FeedWriter:
    """Appends events for one session to <root>/<runtime>/<session>.jsonl. Each call writes one line and returns
    the object written."""

    def __init__(self, runtime, session, root=None):
        self.runtime = _check_id(runtime, RUNTIME_ID, RUNTIME_ID_MAX, "runtime")
        self.session_id = _check_id(session, SESSION_ID, SESSION_ID_MAX, "session")
        self.folder = _root(root) / self.runtime
        self.path = self.folder / f"{self.session_id}.jsonl"

    def _write(self, event_type, **fields):
        line = {"v": VERSION, "t": round(time.time(), 3), "type": event_type}
        line.update({k: v for k, v in fields.items() if v is not None})
        data = (json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(data) > LINE_MAX:
            raise ValueError(f"line is {len(data)} bytes; the reader skips lines over {LINE_MAX}")
        self.folder.mkdir(parents=True, exist_ok=True)
        _append(self.path, data)
        return line

    def session(self, name=None, model=None, cwd=None, parent=None, role=None):
        """What the session is: a name, its model, its working folder, its parent session (for a subagent)."""
        if parent is not None:
            _check_id(parent, SESSION_ID, SESSION_ID_MAX, "parent")
        if cwd is not None:
            cwd = os.path.abspath(os.path.expanduser(str(cwd)))
        return self._write("session", name=_text(name, TEXT_MAX), model=_text(model, TEXT_MAX),
                           cwd=_text(cwd, PATH_MAX), parent=parent, role=_text(role, ROLE_MAX))

    def tool(self, name, target=None):
        """A tool call, and the file path or URL it acted on (a label: never a command or its output)."""
        name = _text(name, TEXT_MAX)
        if not name:
            raise ValueError("tool needs a name")
        return self._write("tool", name=name, target=_text(target, PATH_MAX))

    def file(self, path, op):
        """A file read, written or edited."""
        if op not in FILE_OPS:
            raise ValueError(f"op must be one of {', '.join(FILE_OPS)}: {op!r}")
        path = _text(path, PATH_MAX)
        if not path:
            raise ValueError("file needs a path")
        return self._write("file", path=path, op=op)

    def ship(self, kind, ref=None, ok=True):
        """A commit, push or pull request; ref is the branch, or the PR's https://github.com/.../pull/N URL."""
        if kind not in SHIP_KINDS:
            raise ValueError(f"kind must be one of {', '.join(SHIP_KINDS)}: {kind!r}")
        return self._write("ship", kind=kind, ref=_text(ref, TEXT_MAX), ok=None if ok else False)

    def end(self):
        """The session is over (it leaves the plane at once instead of after the 6 minute window)."""
        return self._write("end")


def describe_runtime(runtime, label=None, color=None, aliases=(), root=None):
    """Write <root>/<runtime>/runtime.json: the runtime's name, colour (#rrggbb) and other spellings."""
    runtime = _check_id(runtime, RUNTIME_ID, RUNTIME_ID_MAX, "runtime")
    meta = {}
    if label is not None:
        meta["label"] = _text(label, LABEL_MAX)
        if not meta["label"]:
            raise ValueError("label is empty")
    if color is not None:
        if not COLOR.fullmatch(color):
            raise ValueError(f"color must be #rrggbb: {color!r}")
        meta["color"] = color
    aliases = [a for a in (_text(a, LABEL_MAX) for a in aliases or ()) if a][:ALIASES_MAX]
    if aliases:
        meta["aliases"] = [a.lower() for a in aliases]
    folder = _root(root) / runtime
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".runtime.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, folder / "runtime.json")
    return meta


def main(argv=None):
    parser = argparse.ArgumentParser(prog="feed.py", description="Append one event to an agent-dashboard feed.")
    parser.add_argument("--runtime", required=True, help="runtime id, e.g. opencode (lowercase, digits, hyphens)")
    parser.add_argument("--session", help="session id (lowercase, digits, hyphens, underscores); not for describe")
    parser.add_argument("--root", help="feed folder (default: AGENT_DASH_FEED_DIR or ~/.agent-dashboard/feeds)")
    sub = parser.add_subparsers(dest="type", required=True)
    p = sub.add_parser("session", help="name the session")
    p.add_argument("--name")
    p.add_argument("--model")
    p.add_argument("--cwd", help="working folder (made absolute)")
    p.add_argument("--parent", help="parent session id, for a subagent")
    p.add_argument("--role")
    p = sub.add_parser("tool", help="a tool call")
    p.add_argument("--name", required=True)
    p.add_argument("--target", help="file path or URL it acted on")
    p = sub.add_parser("file", help="a file read, written or edited")
    p.add_argument("--path", required=True)
    p.add_argument("--op", required=True, choices=FILE_OPS)
    p = sub.add_parser("ship", help="a commit, push or pull request")
    p.add_argument("--kind", required=True, choices=SHIP_KINDS)
    p.add_argument("--ref", help="branch, or the pull request URL")
    p.add_argument("--failed", action="store_true", help="it did not go through")
    sub.add_parser("end", help="the session is over")
    p = sub.add_parser("describe", help="name and colour the runtime (writes runtime.json)")
    p.add_argument("--label")
    p.add_argument("--color", help="#rrggbb")
    p.add_argument("--alias", action="append", default=[], help="another spelling (repeatable)")
    args = parser.parse_args(argv)
    try:
        if args.type == "describe":
            describe_runtime(args.runtime, args.label, args.color, args.alias, args.root)
            return 0
        if not args.session:
            parser.error(f"{args.type} needs --session")
        w = FeedWriter(args.runtime, args.session, args.root)
        if args.type == "session":
            w.session(args.name, args.model, args.cwd, args.parent, args.role)
        elif args.type == "tool":
            w.tool(args.name, args.target)
        elif args.type == "file":
            w.file(args.path, args.op)
        elif args.type == "ship":
            w.ship(args.kind, args.ref, ok=not args.failed)
        else:
            w.end()
    except (ValueError, OSError) as exc:
        print(f"feed.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
