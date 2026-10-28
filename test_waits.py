"""Run with: python -m unittest test_waits -v"""
import unittest

import waits


class Summarize(unittest.TestCase):
    def test_share_counts_blocked_and_idle_as_waiting(self):
        # 0-100 working, 100-400 blocked, 400-500 working, 500-520 idle, then done
        entries = [(0, "working"), (100, "blocked"), (400, "working"), (500, "idle"), (520, "done")]
        s = waits.summarize(entries, now=10_000)
        self.assertEqual(s["total"], 520)
        self.assertEqual(s["waiting"], 320)   # 300 blocked + 20 idle
        self.assertEqual(s["working"], 200)
        self.assertAlmostEqual(s["share"], 320 / 520, places=3)
        self.assertEqual(s["longest"], {"state": "blocked", "seconds": 300, "at": 100})

    def test_running_job_clock_runs_to_now(self):
        entries = [(0, "working"), (100, "blocked")]
        s = waits.summarize(entries, now=400)
        self.assertEqual(s["waiting"], 300)     # blocked from 100 to now
        self.assertEqual(s["total"], 400)

    def test_done_job_stops_its_clock(self):
        entries = [(0, "working"), (50, "done")]
        s = waits.summarize(entries, now=10_000)
        self.assertEqual(s["total"], 50)
        self.assertEqual(s["share"], 0.0)

    def test_suggestion_only_with_enough_history(self):
        short = waits.summarize([(0, "blocked"), (30, "done")], now=100)
        self.assertEqual(short["suggestion"], "")
        long = waits.summarize([(0, "blocked"), (300, "done")], now=1000)
        self.assertIn("waits on you", long["suggestion"])
        idle = waits.summarize([(0, "working"), (60, "idle"), (300, "done")], now=1000)
        self.assertIn("idle", idle["suggestion"])

    def test_empty_timeline(self):
        s = waits.summarize([], now=100)
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["share"], 0.0)
        self.assertIsNone(s["longest"])


if __name__ == "__main__":
    unittest.main()
