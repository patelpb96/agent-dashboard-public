"""Wait share per job: how much of an agent's wall-clock time went to waiting.

Built from each job's timeline.jsonl, whose entries record a state change at a
time. Waiting means "blocked" (it needs you) or "idle" (between turns, awaiting
orders). Everything else counts as working.
"""
import json
from datetime import datetime
from pathlib import Path

WAIT_STATES = {"blocked", "idle"}
MIN_TOTAL_S = 60        # a job needs a minute of history before it gets advice


def _ts(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_entries(path):
    """(time, state) pairs from a timeline file, oldest first. Missing file gives []."""
    out = []
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                at = _ts(entry.get("at"))
                if at is not None:
                    out.append((at, entry.get("state") or "unknown"))
    except OSError:
        return []
    return sorted(out)


def suggest(total, totals):
    """One plain-language change that would give the agent more time working."""
    if total < MIN_TOTAL_S:
        return ""
    blocked, idle = totals.get("blocked", 0), totals.get("idle", 0)
    if blocked >= 0.25 * total:
        return "It waits on you a lot: settle decisions up front so it doesn't stop to ask."
    if idle >= 0.25 * total:
        return "It sits idle between turns: put the next step in the same prompt."
    return ""


def summarize(entries, now):
    """Time per state, the wait share, the longest single wait and a suggestion."""
    totals = {}
    longest = None
    for i, (at, state) in enumerate(entries):
        if i + 1 < len(entries):
            end = entries[i + 1][0]
        elif state == "done":
            end = at            # a finished job stops its clock
        else:
            end = now           # still running: the clock runs to now
        secs = max(0.0, end - at)
        totals[state] = totals.get(state, 0.0) + secs
        if state in WAIT_STATES and (longest is None or secs > longest["seconds"]):
            longest = {"state": state, "seconds": round(secs), "at": at}
    total = sum(totals.values())
    waiting = sum(totals.get(s, 0.0) for s in WAIT_STATES)
    return {
        "total": round(total),
        "waiting": round(waiting),
        "working": round(total - waiting),
        "share": round(waiting / total, 3) if total else 0.0,
        "longest": longest,
        "suggestion": suggest(total, totals),
    }
