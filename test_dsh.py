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


PUSHED = "To github.com:acme/widget.git\n   1a2b3c4..5d6e7f8  main -> main\n"


def zstd_compress(*texts):
    """Each text as its own zstd frame, appended, the way DSH writes its transcript. None without Node zstd."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        return None
    script = ("const z=require('zlib');let s='';process.stdin.on('data',d=>s+=d).on('end',()=>{"
              "for(const t of JSON.parse(s))process.stdout.write(z.zstdCompressSync(Buffer.from(t)))})")
    try:
        done = subprocess.run([node, "-e", script], input=json.dumps(texts).encode(), capture_output=True,
                              timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout


def event(kind, at, **data):
    return json.dumps({"type": kind, "seq": 1, "time": int(at * 1000), "data": data}) + "\n"


def call(at, cid, name, **args):
    return event("tool/call", at, turn=1, step=1, callId=cid, name=name, arguments=json.dumps(args))


def result(at, cid, text, error=False):
    return event("tool/result", at, turn=1, step=1,
                 message={"role": "tool", "toolCallId": cid, "isError": error, "content": [{"type": "text", "text": text}]})


class ZstdSplitTest(unittest.TestCase):
    def test_false_magic_is_merged_and_partial_tail_is_left(self):
        magic = graph.ZSTD_MAGIC
        frame_a = magic + b"AAA" + magic + b"aaa"   # a magic number inside the compressed bytes
        frame_b = magic + b"BBB"
        whole = {frame_a: b"one\n", frame_b: b"two\n"}

        def decode_one(chunk):
            if chunk not in whole:
                raise ValueError("not a frame")
            return whole[chunk]

        text, used = graph._zstd_split(frame_a + frame_b + magic + b"CC", decode_one)   # last frame still writing
        self.assertEqual(text, b"one\ntwo\n")
        self.assertEqual(used, len(frame_a + frame_b))


class DshCallsTest(unittest.TestCase):
    def records(self, now):
        return [("call", now - 30, "c1", "read", {"file_path": "C:\\work\\proj\\app.py"}),
                ("result", now - 29, "c1", False, "ok"),
                ("call", now - 20, "c2", "pwsh", {"command": "git push origin main"}),
                ("result", now - 18, "c2", False, PUSHED),
                ("call", now - 5, "c3", "grep", {"pattern": "TODO", "path": "C:\\work\\proj"})]

    def test_files_shells_and_pushes_are_drawn(self):
        now, nodes, edges = time.time(), {}, {}
        events, open_calls, ships = graph._dsh_calls("deepseek:s", "C:\\work\\proj", self.records(now), True,
                                                     nodes, edges)
        docs = [n for n in nodes.values() if n["kind"] == "doc"]
        self.assertTrue(any(n["label"] == "app.py" for n in docs))
        self.assertIn(("deepseek:s", "tool:pwsh", "call"), edges)
        self.assertEqual(nodes["tool:pwsh"]["groupLabel"], "shell")
        read = next(e for e in events if e["tool"] == "read")
        self.assertEqual(read["ms"], 1000)
        self.assertEqual(list(open_calls), ["c3"])          # no result yet: in flight
        self.assertEqual([(s["step"], s["ok"]) for s in ships], [("push", True)])

    def test_a_quiet_session_has_nothing_in_flight(self):
        _, open_calls, _ = graph._dsh_calls("deepseek:s", "C:\\work\\proj", self.records(time.time()), False, {}, {})
        self.assertEqual(open_calls, {})


class DshTranscriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = time.time()
        graph._DSH_TAILS.clear()

    def tearDown(self):
        graph._DSH_TAILS.clear()
        self.tmp.cleanup()

    def test_transcript_is_decoded_incrementally(self):
        first = zstd_compress(event("session", self.now - 60),
                              call(self.now - 50, "c1", "edit", file_path="C:\\work\\proj\\main.py",
                                   old_string="a", new_string="b"),
                              result(self.now - 49, "c1", "edited"))
        if not first or graph.zstd_frames(first)[1] != len(first):
            self.skipTest("no zstd decoder (needs Python 3.14 or Node 22.15+)")
        write_session(self.root, "session-z", open_turn=True, now=self.now)
        transcript = self.root / "sessions" / "--C-work-proj--" / "session-z" / "session.v4.jsonl.zstd"
        transcript.parent.mkdir(parents=True)
        transcript.write_bytes(first)
        nodes = graph.parse_dsh(self.root, self.now)["nodes"]
        self.assertTrue(any(n.get("label") == "main.py" for n in nodes.values()))

        offset = graph._DSH_TAILS[transcript]["offset"]
        self.assertEqual(offset, len(first))
        with transcript.open("ab") as f:   # DSH appends a frame per write
            f.write(zstd_compress(call(self.now - 10, "c2", "pwsh", command="git push origin main"),
                                  result(self.now - 9, "c2", PUSHED)))
        parsed = graph.parse_dsh(self.root, self.now)
        self.assertGreater(graph._DSH_TAILS[transcript]["offset"], offset)
        self.assertEqual(len(graph._DSH_TAILS[transcript]["records"]), 4)   # nothing decoded twice
        self.assertEqual([s["step"] for s in parsed["ships"]], ["push"])


if __name__ == "__main__":
    unittest.main()
