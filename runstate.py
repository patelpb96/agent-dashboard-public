"""Remembers whether the dashboard server is running and, when it is not, how it stopped.

One small JSON record per port lives in STATE_DIR (default ~/.agent-dashboard, see paths.py):

  {"state": "running", "pid": 1234, "port": 8765, "startedAt": ...}
  {"state": "stopped", ..., "stoppedAt": ..., "reason": "window closed"}

The server writes "running" when it starts and "stopped" with a reason when it is told to stop:
its console window closed, Ctrl+C or Ctrl+Break, the user signing out, Windows shutting down, or
SIGTERM/SIGHUP elsewhere. A forced kill (Task Manager "End task" on the process, taskkill /F, kill -9)
or a power cut cannot be caught, so it leaves "running" behind with a dead pid; status() reports that
as ended without a clean stop.

`python server.py --status` prints status(). Stdlib only.
"""
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from paths import STATE_DIR

# Windows console control events (wincon.h)
CTRL_REASONS = {
    0: "Ctrl+C",
    1: "Ctrl+Break",
    2: "window closed",
    5: "user signed out",
    6: "Windows shut down",
}
SIGNAL_REASONS = {"SIGTERM": "terminated (SIGTERM)", "SIGHUP": "terminal closed (SIGHUP)"}


def record_path(port, state_dir=None):
    return Path(state_dir or STATE_DIR) / f"server-{int(port)}.json"


def read(port, state_dir=None):
    try:
        data = json.loads(record_path(port, state_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)   # never leave a half-written record


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if isinstance(ts, (int, float)) else "?"


def status(port, pid_alive, state_dir=None):
    """(code, text). code: 0 running, 1 stopped cleanly, 2 ended without a clean stop, 3 no record."""
    data = read(port, state_dir)
    if not data:
        return 3, f"No record for port {port}: the dashboard has not run here since this feature was added."
    since = _when(data.get("startedAt"))
    if data.get("state") == "running":
        if pid_alive(data.get("pid")):
            return 0, f"Running on port {port} (pid {data.get('pid')}) since {since}."
        return 2, (f"Not running. The server started {since} (pid {data.get('pid')}) ended without a clean stop: "
                   "it was force-killed, crashed, or the machine lost power.")
    reason = data.get("reason") or "unknown"
    return 1, f"Stopped at {_when(data.get('stoppedAt'))}: {reason}. It had been running since {since}."


class RunState:
    """The record for this server process. stop() keeps the first reason it is given."""

    def __init__(self, port, pid=None, state_dir=None, clock=time.time):
        self.path = record_path(port, state_dir)
        self.clock = clock
        self.data = {"state": "running", "pid": pid or os.getpid(), "port": int(port), "startedAt": clock()}
        self._lock = threading.Lock()
        self._stopped = False

    def start(self):
        try:
            _write(self.path, self.data)
        except OSError as exc:   # the dashboard still works without its record
            print(f"(could not write {self.path}: {exc})", file=sys.stderr)

    def stop(self, reason):
        with self._lock:
            if self._stopped:
                return False
            self._stopped = True
            self.data.update(state="stopped", stoppedAt=self.clock(), reason=str(reason))
            try:
                _write(self.path, self.data)
            except OSError:
                pass
            return True

    def console_event(self, event):
        """Windows console control handler body. False lets the next handler run (Python's own turns
        Ctrl+C into KeyboardInterrupt); for close, sign-out and shutdown Windows ends the process
        once handlers return, so the record has to be on disk before this returns."""
        self.stop(CTRL_REASONS.get(event, f"console event {event}"))
        return False

    def install(self):
        """Hook the ways a user ends the server, so each one leaves its reason behind."""
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            routine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
            self._handler = routine(lambda event: bool(self.console_event(event)))   # keep a reference
            ctypes.windll.kernel32.SetConsoleCtrlHandler(self._handler, True)
        else:
            def on_signal(signum, _frame):
                name = signal.Signals(signum).name
                self.stop(SIGNAL_REASONS.get(name, name))
                raise SystemExit(0)
            for name in SIGNAL_REASONS:
                if hasattr(signal, name):
                    signal.signal(getattr(signal, name), on_signal)
