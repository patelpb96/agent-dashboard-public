"""Usage and recorded cost from every connected local agent runtime.

Claude Code provides assistant usage plus cumulative cost-state records. Codex
rollouts provide per-response token usage but no dollar cost. Pi sessions provide
per-assistant usage and provider/model cost records for any Pi-connected model.
Dollar amounts are only included when the runtime recorded them; missing costs
remain explicitly unreported instead of being estimated as zero.
"""
import json
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from paths import CODEX_SESSIONS_DIR, PI_SESSIONS_DIR, PROJECTS_DIR
REFRESH_SECONDS = 60
TOKEN_KEYS = ("input", "output", "cacheRead", "cacheWrite", "messages")


def _tokens():
    return {key: 0 for key in TOKEN_KEYS}


def _int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _model_key(provider, model):
    provider = str(provider or "unknown").strip().casefold() or "unknown"
    model = str(model or "unknown").strip() or "unknown"
    return f"{provider}/{model}"


def _day(timestamp):
    return timestamp[:10] if isinstance(timestamp, str) and len(timestamp) >= 10 else "unknown"


def _new_session(session_id, provider):
    return {
        "session": str(session_id),
        "provider": provider,
        "tokens": _tokens(),
        "byDay": {},
        "byModel": {},
        "cost": {"usd": None, "byModel": {}, "reported": False, "reportedMessages": 0},
    }


def _add_counts(parsed, model, timestamp, values):
    key = _model_key(*model) if isinstance(model, tuple) else model
    inc = {name: _int(values.get(name)) for name in TOKEN_KEYS}
    buckets = (
        parsed["tokens"],
        parsed["byDay"].setdefault(_day(timestamp), _tokens()),
        parsed["byModel"].setdefault(key, _tokens()),
    )
    for bucket in buckets:
        for name in TOKEN_KEYS:
            bucket[name] += inc[name]


def _normalized_usage(usage, source):
    usage = usage if isinstance(usage, dict) else {}
    if source == "claude":
        input_tokens = _int(usage.get("input_tokens"))
        cache_read = _int(usage.get("cache_read_input_tokens"))
        return {
            "input": input_tokens,
            "output": _int(usage.get("output_tokens")),
            "cacheRead": cache_read,
            "cacheWrite": _int(usage.get("cache_creation_input_tokens")),
        }
    if source == "codex":
        # Codex's input_tokens includes the cached subset; expose it separately.
        total_input = _int(usage.get("input_tokens"))
        cache_read = _int(usage.get("cached_input_tokens"))
        return {
            "input": max(0, total_input - cache_read),
            "output": _int(usage.get("output_tokens")),
            "cacheRead": cache_read,
            "cacheWrite": _int(usage.get("cache_write_input_tokens")),
        }
    return {
        "input": _int(usage.get("input", usage.get("input_tokens"))),
        "output": _int(usage.get("output", usage.get("output_tokens"))),
        "cacheRead": _int(usage.get("cacheRead", usage.get("cache_read_input_tokens"))),
        "cacheWrite": _int(usage.get("cacheWrite", usage.get("cache_creation_input_tokens"))),
    }


def _add_usage(parsed, provider, model, timestamp, usage, source, message=False):
    values = _normalized_usage(usage, source)
    values["messages"] = int(message)
    _add_counts(parsed, (provider, model), timestamp, values)


def _record_pi_cost(parsed, model_key, usage):
    raw = usage.get("costUSD", usage.get("cost_usd", usage.get("cost")))
    if isinstance(raw, dict):
        total = None
        for key in ("total", "totalUSD", "total_usd"):
            if key in raw:
                total = _number(raw.get(key))
                break
        if total is None:
            parts = [_number(raw.get(key)) for key in ("input", "output", "cacheRead", "cacheWrite")]
            parts = [value for value in parts if value is not None]
            total = sum(parts) if parts else None
    else:
        total = _number(raw)
    if total is None:
        return
    cost = parsed["cost"]
    cost["reported"] = True
    cost["usd"] = (cost["usd"] or 0.0) + total
    cost["reportedMessages"] += 1
    cost["byModel"][model_key] = cost["byModel"].get(model_key, 0.0) + total


def parse_claude_file(path):
    """Aggregate one Claude Code transcript, deduplicating assistant message ids."""
    parsed = _new_session(path.stem, "anthropic")
    seen = set()
    cost_state_messages = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as source:
            for line in source:
                if '"usage"' not in line and '"cost-state"' not in line and '"assistant"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") == "cost-state":
                    by_model = {}
                    for name, data in (entry.get("modelUsage") or {}).items():
                        usd = _number((data or {}).get("costUSD"))
                        if usd is not None:
                            by_model[_model_key("anthropic", name)] = usd
                    total = _number(entry.get("totalCostUSD"))
                    if total is None and ("totalCostUSD" in entry or by_model):
                        total = sum(by_model.values())
                    parsed["cost"] = {
                        "usd": total,
                        "byModel": by_model,
                        "reported": "totalCostUSD" in entry or "modelUsage" in entry,
                        "reportedMessages": parsed["tokens"]["messages"],
                    }
                    cost_state_messages = parsed["tokens"]["messages"]
                    continue
                if entry.get("type") != "assistant":
                    continue
                msg = entry.get("message") or {}
                if not isinstance(msg, dict):
                    continue
                mid = msg.get("id") or entry.get("uuid")
                if mid and mid in seen:
                    continue
                if mid:
                    seen.add(mid)
                _add_usage(parsed, "anthropic", msg.get("model"), entry.get("timestamp"),
                           msg.get("usage"), "claude", message=True)
    except OSError:
        return None
    if parsed["cost"]["reported"]:
        parsed["cost"]["reportedMessages"] = cost_state_messages
    return parsed


def parse_codex_file(path):
    """Aggregate one Codex rollout using per-response token_usage_record events."""
    parsed = _new_session(path.stem, "codex")
    model_by_turn = {}
    current_model = None
    response_ids = set()
    message_ids = set()
    fallback_usage = []
    usage_records = 0
    try:
        with path.open("rb") as source:
            for raw in source:
                try:
                    entry = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                kind = entry.get("type")
                payload = entry.get("payload") or {}
                if not isinstance(payload, dict):
                    payload = {}
                timestamp = entry.get("timestamp")
                if kind == "session_meta":
                    parsed["session"] = str(payload.get("id") or payload.get("session_id") or parsed["session"])
                elif kind == "turn_context":
                    model = payload.get("model")
                    if model:
                        current_model = model
                        if payload.get("turn_id"):
                            model_by_turn[payload["turn_id"]] = model
                elif kind == "token_usage_record":
                    usage = payload.get("usage")
                    if isinstance(usage, dict):
                        response_id = payload.get("response_id")
                        if response_id and response_id in response_ids:
                            continue
                        if response_id:
                            response_ids.add(response_id)
                        model = model_by_turn.get(payload.get("turn_id"), current_model)
                        _add_usage(parsed, "codex", model, timestamp, usage, "codex")
                        usage_records += 1
                elif kind == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    usage = info.get("last_token_usage")
                    if isinstance(usage, dict):
                        fallback_usage.append((current_model, timestamp, usage))
                elif kind == "response_item":
                    if payload.get("type") != "message" or payload.get("role") != "assistant":
                        continue
                    message_id = payload.get("id")
                    if message_id and message_id in message_ids:
                        continue
                    if message_id:
                        message_ids.add(message_id)
                    model = current_model
                    values = _tokens()
                    values["messages"] = 1
                    _add_counts(parsed, ("codex", model), timestamp, values)
    except OSError:
        return None
    # Older Codex rollouts predate token_usage_record and only have token_count snapshots.
    if not usage_records:
        for model, timestamp, usage in fallback_usage:
            _add_usage(parsed, "codex", model, timestamp, usage, "codex")
    return parsed


def parse_pi_file(path):
    """Aggregate Pi assistant messages, using each message's provider and model."""
    parsed = _new_session(path.stem, "pi")
    current_model = None
    current_provider = None
    seen = set()
    try:
        with path.open("rb") as source:
            for raw in source:
                try:
                    entry = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                kind = entry.get("type")
                if kind == "session":
                    parsed["session"] = str(entry.get("id") or parsed["session"])
                    continue
                if kind == "model_change":
                    current_model = entry.get("modelId") or current_model
                    current_provider = entry.get("provider") or current_provider
                    continue
                if kind != "message":
                    continue
                msg = entry.get("message") or {}
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                message_id = msg.get("id") or entry.get("id")
                if message_id and message_id in seen:
                    continue
                if message_id:
                    seen.add(message_id)
                provider = msg.get("provider") or entry.get("provider") or current_provider or "pi"
                model = msg.get("model") or current_model
                usage = msg.get("usage") or {}
                model_key = _model_key(provider, model)
                _add_usage(parsed, provider, model, entry.get("timestamp"), usage, "pi", message=True)
                if isinstance(usage, dict):
                    _record_pi_cost(parsed, model_key, usage)
    except OSError:
        return None
    return parsed


class Metrics:
    def __init__(self):
        self._files = {}  # path -> ((mtime, size), parsed source metrics)
        self._lock = threading.Lock()
        self._snapshot = None
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                self.refresh()
            except Exception:
                pass  # preserve the last good snapshot and retry next cycle
            time.sleep(REFRESH_SECONDS)

    def refresh(self):
        sources = (
            (PROJECTS_DIR, "*/*.jsonl", parse_claude_file),
            (CODEX_SESSIONS_DIR, "**/rollout-*.jsonl", parse_codex_file),
            (PI_SESSIONS_DIR, "**/*.jsonl", parse_pi_file),
        )
        present = set()
        for root, pattern, parser in sources:
            if not root.is_dir():
                continue
            for path in root.glob(pattern):
                present.add(path)
                try:
                    stat = path.stat()
                except OSError:
                    continue
                key = (stat.st_mtime, stat.st_size)
                cached = self._files.get(path)
                if cached and cached[0] == key:
                    continue
                parsed = parser(path)
                if parsed is not None:
                    self._files[path] = (key, parsed)
        for path in set(self._files) - present:
            self._files.pop(path, None)
        self._publish()

    def _publish(self):
        tokens = _tokens()
        by_day = defaultdict(_tokens)
        by_model = defaultdict(_tokens)
        model_cost = defaultdict(float)
        sessions = {}
        total_usd = 0.0
        priced_sessions = 0
        cost_unreported_sessions = 0
        cost_unreported_messages = 0
        session_count = 0

        for _, parsed in self._files.values():
            if not parsed["tokens"]["messages"] and not parsed["cost"]["reported"] and not any(
                parsed["tokens"][key] for key in TOKEN_KEYS if key != "messages"
            ):
                continue
            provider = parsed["provider"]
            session_key = f"{provider}:{parsed['session']}"
            _merge(tokens, parsed["tokens"])
            for day, bucket in parsed["byDay"].items():
                _merge(by_day[day], bucket)
            for model, bucket in parsed["byModel"].items():
                _merge(by_model[model], bucket)
            cost = parsed["cost"]
            if cost["reported"] and cost["usd"] is not None:
                priced_sessions += 1
                total_usd += cost["usd"]
                cost_unreported_messages += max(0, parsed["tokens"]["messages"] - cost["reportedMessages"])
                for model, usd in cost["byModel"].items():
                    model_cost[model] += usd
                model_cost_total = sum(cost["byModel"].values())
                remainder = cost["usd"] - model_cost_total
                if provider == "anthropic" and remainder > 0.000001:
                    model_cost["anthropic/unattributed"] += remainder
            else:
                cost_unreported_sessions += 1
                cost_unreported_messages += parsed["tokens"]["messages"]
            session_count += 1
            session_usage = {
                "provider": provider,
                "usd": cost["usd"] if cost["reported"] else None,
                "costReported": bool(cost["reported"] and cost["usd"] is not None),
                **parsed["tokens"],
            }
            sessions[session_key] = session_usage
            # Preserve the original lookup key used by the Claude job detail drawer.
            if provider == "anthropic":
                sessions[parsed["session"]] = session_usage

        for model, usd in model_cost.items():
            by_model[model]["usd"] = usd
        for bucket in by_model.values():
            bucket.setdefault("usd", None)

        by_model_sorted = dict(sorted(by_model.items(), key=lambda item: (
            item[1]["usd"] is None,
            -(item[1]["usd"] or 0.0),
            -item[1]["output"],
            item[0],
        )))
        snapshot = {
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "total": {**tokens, "usd": total_usd if priced_sessions else None},
            "costCoverage": {
                "reportedSessions": priced_sessions,
                "pricedSessions": priced_sessions,
                "unreportedSessions": cost_unreported_sessions,
                "unreportedMessages": cost_unreported_messages,
            },
            "sessionCount": session_count,
            "byDay": dict(sorted(by_day.items())),
            "byModel": by_model_sorted,
            "sessions": sessions,
        }
        with self._lock:
            self._snapshot = snapshot

    def snapshot(self):
        with self._lock:
            return self._snapshot


def _merge(dst, src):
    for key in TOKEN_KEYS:
        dst[key] += src[key]
