"""DeepSeek Harness Sessions: the plain-JSON projection cache read into the agent graph.
Fixtures are laid out like real ~/.dsh files (storages/session_projcache/sessions/<id>.json).
Run with: python -m unittest test_dsh -v"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import graph


def row(val):
    return {"ver": 1, "seq": 1, "val": val}


def write_session(root, sid, *, title="", cwd="C:\\work\\proj", open_turn=False, asking=False,
                  children=(), sub_label=None, model="deepseek-flash", age=0.0, now=None):
    rows = {
        "title": row(title),
        "workingDirectory": row(cwd),
        "turnBoundary": row({"openTurnStartSeq": 7 if open_turn else None, "lastTurn": 1}),
        "userQuestions": row({"questions": {"active": [{"id": "q"}] if asking else [], "settled": []}}),
        "modelSelection": row({"lastUsed": {"provider": "deepseek-account", "model": model}}),
        "tokenUsage": row({"totals": {"uncachedInputTokens": 10, "outputTokens": 5, "cacheReadTokens": 85}}),
        "sessionStats": row({"turns": 3}),
        "subagentCatalog": row({"head": {"values": [{"childId": c, "label": lbl} for c, lbl in children]}}),
        "subagent": row({"identity": {"label": sub_label}} if sub_label else {}),
    }
    path = Path(root) / "storages" / "session_projcache" / "sessions" / f"{sid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 7, "record": {"identity": {"cwd": cwd}, "rows": rows}}), encoding="utf-8")
    stamp = (now or time.time()) - age
    os.utime(path, (stamp, stamp))
    return path


class DshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    def parse(self):
        return graph.parse_dsh(self.root, self.now)["nodes"]

    def test_running_session_and_its_subagent(self):
        write_session(self.root, "session-aaa111", title="Plan work", open_turn=True,
                      children=[("bbb222", "Builds the checker")], now=self.now)
        write_session(self.root, "bbb222", title="You are a worker...", sub_label="x", age=60, now=self.now)
        nodes = self.parse()
        parent, child = nodes["deepseek:session-aaa111"], nodes["deepseek:bbb222"]
        self.assertEqual((parent["kind"], parent["state"], parent["open"]), ("agent", "working", True))
        self.assertEqual(parent["domain"], "deepseek")
        self.assertEqual(parent["model"], "deepseek-flash")
        self.assertEqual(parent["tokens"], 100)
        self.assertEqual((child["kind"], child["parent"]), ("subagent", "deepseek:session-aaa111"))
        self.assertEqual(child["title"], "Builds the checker")   # the parent's label, not the prompt title
        self.assertEqual(child["state"], "idle")
        self.assertIn(("deepseek:session-aaa111", "deepseek:bbb222", "spawn"),
                      graph.parse_dsh(self.root, self.now)["edges"])

    def test_pending_question_is_blocked(self):
        write_session(self.root, "session-q", asking=True, age=20 * 60, now=self.now)
        node = self.parse()["deepseek:session-q"]
        self.assertEqual((node["state"], node["open"]), ("blocked", True))

    def test_quiet_and_archived_sessions_are_done(self):
        write_session(self.root, "session-quiet", age=10 * 60, now=self.now)
        write_session(self.root, "session-stuck", open_turn=True, age=45 * 60, now=self.now)   # crashed mid-turn
        write_session(self.root, "session-arch", now=self.now)
        (self.root / "storages" / "workspace.json").write_text(json.dumps(
            {"global": {"archivedSessionIds": ["session-arch"]},
             "tables": {"workspaces": {"w": {"title": "Proj", "sessionIds": ["session-quiet"]}}}}), encoding="utf-8")
        nodes = self.parse()
        for sid in ("session-quiet", "session-stuck", "session-arch"):
            self.assertEqual((nodes["deepseek:" + sid]["state"], nodes["deepseek:" + sid]["open"]), ("done", False))
        self.assertIn("· Proj ·", nodes["deepseek:session-quiet"]["label"])   # workspace title names it

    def test_old_sessions_and_bad_files_are_skipped(self):
        write_session(self.root, "session-old", age=graph.GRAPH_WINDOW_S + 60, now=self.now)
        bad = self.root / "storages" / "session_projcache" / "sessions" / "session-bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.parse(), {})

    def test_cache_key_follows_writes(self):
        path = write_session(self.root, "session-k", now=self.now)
        before = graph.dsh_key(self.root, self.now)
        os.utime(path, (self.now + 5, self.now + 5))
        self.assertNotEqual(before, graph.dsh_key(self.root, self.now))


if __name__ == "__main__":
    unittest.main()
