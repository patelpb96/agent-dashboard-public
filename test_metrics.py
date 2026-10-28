"""Coverage for cross-provider token and recorded-cost aggregation."""
import json
import threading
import tempfile
import unittest
from pathlib import Path

import metrics


class ProviderMetrics(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_jsonl(self, name, entries):
        path = self.root / name
        path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
        return path

    def test_aggregates_claude_codex_and_pi_records(self):
        claude_message = {
            "type": "assistant", "uuid": "claude-uuid", "timestamp": "2026-10-19T10:00:00Z",
            "message": {"id": "claude-message", "model": "claude-sonnet-4",
                        "usage": {"input_tokens": 10, "output_tokens": 2,
                                  "cache_read_input_tokens": 3, "cache_creation_input_tokens": 1}},
        }
        claude_path = self.write_jsonl("claude.jsonl", [
            claude_message,
            claude_message,  # the transcript can repeat the same assistant message id
            {"type": "cost-state", "totalCostUSD": 0.2,
             "modelUsage": {"claude-sonnet-4": {"costUSD": 0.15}}},
        ])
        codex_path = self.write_jsonl("codex.jsonl", [
            {"type": "session_meta", "payload": {"id": "codex-session"}},
            {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "gpt-6-luna"}},
            {"type": "token_usage_record", "timestamp": "2026-10-19T10:01:00Z",
             "payload": {"turn_id": "turn-1", "response_id": "response-1",
                         "usage": {"input_tokens": 12, "cached_input_tokens": 5,
                                   "cache_write_input_tokens": 1, "output_tokens": 6}}},
            {"type": "token_usage_record", "timestamp": "2026-10-19T10:01:00Z",
             "payload": {"turn_id": "turn-1", "response_id": "response-1",
                         "usage": {"input_tokens": 12, "cached_input_tokens": 5,
                                   "cache_write_input_tokens": 1, "output_tokens": 6}}},
            {"type": "response_item", "timestamp": "2026-10-19T10:01:00Z",
             "payload": {"type": "message", "role": "assistant", "id": "codex-message"}},
        ])
        pi_path = self.write_jsonl("pi.jsonl", [
            {"type": "session", "id": "pi-session"},
            {"type": "message", "timestamp": "2026-10-19T10:02:00Z",
             "message": {"role": "assistant", "id": "pi-message", "provider": "google",
                         "model": "gemini-4-argon",
                         "usage": {"input": 10, "output": 4, "cacheRead": 2, "cacheWrite": 3,
                                   "cost": {"input": 0.01, "output": 0.02, "total": 0.03}}}},
        ])

        parsed = [metrics.parse_claude_file(claude_path), metrics.parse_codex_file(codex_path),
                  metrics.parse_pi_file(pi_path)]
        self.assertTrue(all(parsed))
        collector = metrics.Metrics.__new__(metrics.Metrics)
        collector._files = {path: ((0, 0), value) for path, value in zip(
            (claude_path, codex_path, pi_path), parsed)}
        collector._lock = threading.Lock()
        collector._snapshot = None
        collector._publish()
        snapshot = collector.snapshot()

        self.assertEqual(snapshot["sessionCount"], 3)
        self.assertEqual({key: value for key, value in snapshot["total"].items() if key != "usd"}, {
            "input": 27, "output": 12, "cacheRead": 10, "cacheWrite": 5, "messages": 3,
        })
        self.assertAlmostEqual(snapshot["total"]["usd"], 0.23)
        self.assertEqual(snapshot["costCoverage"]["pricedSessions"], 2)
        self.assertEqual(snapshot["costCoverage"]["unreportedSessions"], 1)
        self.assertEqual(snapshot["costCoverage"]["unreportedMessages"], 1)
        self.assertEqual(snapshot["byModel"]["codex/gpt-6-luna"]["usd"], None)
        self.assertEqual(snapshot["byModel"]["google/gemini-4-argon"]["usd"], 0.03)
        self.assertAlmostEqual(snapshot["byModel"]["anthropic/unattributed"]["usd"], 0.05)


if __name__ == "__main__":
    unittest.main()
