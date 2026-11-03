"""Antigravity conversations: SQLite steps holding protobuf, read into the agent graph.
The fixtures are built here with a tiny protobuf encoder, laid out like real Antigravity CLI files.
Run with: python -m unittest test_antigravity -v"""
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import graph

PUSHED = "Command exited with code 0.\n\nCommand output:\nTo github.com:acme/widget.git\n   1a2b3c4..5d6e7f8  main -> main\n"


def varint(n):
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def field(number, value):
    """One protobuf field: an int as a varint, bytes/str as length-delimited."""
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode()
    return varint(number << 3 | 2) + varint(len(value)) + value


def stamp(at):
    return field(1, int(at)) + field(2, int((at % 1) * 1e9))


def tool_step(at, call_id, name, args, result="", end=None):
    meta = (field(1, stamp(at)) + field(4, field(1, call_id) + field(2, name) + field(3, json.dumps(args))) +
            field(6, stamp(at)) + (field(8, stamp(end)) if end else b""))
    return field(1, graph.AG_STEP_TOOL) + field(5, meta) + (field(140, field(2, field(1, result))) if result else b"")


def text_step(kind, at):
    return field(1, kind) + field(5, field(1, stamp(at)))


def make_db(path, steps, model="gemini-4-argon-trial"):
    """steps: [(step_type, status, payload bytes)]"""
    con = sqlite3.connect(path)
    con.execute("create table steps (idx integer primary key, step_type integer, status integer, step_payload blob)")
    con.execute("create table gen_metadata (idx integer primary key, data blob)")
    con.executemany("insert into steps values (?, ?, ?, ?)", [(i, *s) for i, s in enumerate(steps)])
    con.execute("insert into gen_metadata values (0, ?)", (field(1, field(19, model)),))
    con.commit()
    con.close()


def make_summaries(root, rows):
    con = sqlite3.connect(root / "conversation_summaries.db")
    con.execute("create table conversation_summaries (conversation_id text primary key, title text, "
                "workspace_uris text, status text, parent_conversation_id text, agent_name text, "
                "not_fully_idle numeric, killed numeric)")
    for row in rows:
        con.execute("insert into conversation_summaries values (?, ?, ?, ?, ?, ?, ?, ?)",
                    (row["id"], row.get("title", ""), json.dumps(row.get("workspaces", ["file:///C:/work/widget"])),
                     row.get("status", "CASCADE_RUN_STATUS_IDLE"), row.get("parent", ""), "", 1, 0))
    con.commit()
    con.close()


class Antigravity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "conversations").mkdir()
        self.now = time.time()

    def db(self, cid, steps):
        path = self.root / "conversations" / f"{cid}.db"
        make_db(path, steps)
        return path

    def test_protobuf_reader_rejects_garbage(self):
        self.assertEqual(graph._pb_fields(b"\xff\xff\xff"), [])
        self.assertEqual(graph._pb_fields(field(1, "x")[:-1]), [])          # truncated
        self.assertEqual(graph._pb_get(field(5, field(4, field(2, "read_file"))), 5, 4, 2), b"read_file")
        self.assertEqual(graph._pb_get(b"\x00garbage", 5, 4), None)

    def test_tool_calls_become_edges_with_model_and_workspace(self):
        t = self.now - 60
        path = self.db("conv-aaaa11", [
            (graph.AG_STEP_USER, 3, text_step(graph.AG_STEP_USER, t)),
            (graph.AG_STEP_TOOL, 3, tool_step(t + 1, "c1", "read_file", {"Path": "C:\\work\\widget\\README.md"},
                                              "Showing lines 1-3", t + 1.5)),
            (graph.AG_STEP_TOOL, 3, tool_step(t + 2, "c2", "search_web", {"query": "widgets"}, "ok", t + 3)),
            (graph.AG_STEP_TOOL, 3, tool_step(t + 4, "c3", "run_command", {"CommandLine": "dir"},
                                              "Command exited with code 0.", t + 5)),
            (graph.AG_STEP_MODEL, 3, b"not a protobuf \xff"),                  # a malformed row is skipped
        ])
        summary = {"workspace_uris": json.dumps(["file:///C:/work/widget"]), "title": "Fix widgets"}
        parsed = graph.parse_antigravity_db(path, self.now, summary, present=True)
        agent = parsed["nodes"]["antigravity:conv-aaaa11"]
        self.assertEqual((agent["domain"], agent["model"], agent["cwd"], agent["title"]),
                         ("antigravity", "gemini-4-argon-trial", "C:/work/widget", "Fix widgets"))
        self.assertEqual(agent["label"], "Google · widget · aaaa11")
        dsts = {dst for (_, dst, _) in parsed["edges"]}
        self.assertIn("shared:tool:shell", dsts)
        self.assertIn("web:search_web", dsts)
        self.assertTrue(any(d.startswith("doc:") and d.endswith("readme.md") for d in dsts), dsts)
        shell = next(e for e in parsed["events"] if e["tool"] == "run_command")
        self.assertEqual((shell["detail"], shell["ms"]), ("dir", 1000))

    def test_open_session_with_a_running_step_is_working(self):
        t = self.now - 20
        path = self.db("conv-run", [(graph.AG_STEP_TOOL, graph.AG_STATUS_RUNNING,
                                     tool_step(t, "c1", "run_command", {"CommandLine": "npm test"}))])
        parsed = graph.parse_antigravity_db(path, self.now, {}, present=True)
        self.assertEqual(parsed["nodes"]["antigravity:conv-run"]["state"], "working")
        self.assertEqual(len(parsed["open"]), 1)

    def test_free_lock_with_recent_writes_is_still_open(self):
        # the real CLI keeps writing a conversation while its presence lock is free (seen Nov 2026)
        t = self.now - 5
        path = self.db("conv-live", [(graph.AG_STEP_TOOL, 3, tool_step(t, "c1", "read_file", {"Path": "C:\\a.txt"},
                                                                       "x", t + 1))])
        node = graph.parse_antigravity_db(path, self.now + 10, {"status": "CASCADE_RUN_STATUS_RUNNING"},
                                          present=False)["nodes"]["antigravity:conv-live"]
        self.assertEqual((node["open"], node["state"]), (True, "working"))

    def test_quiet_conversation_with_free_lock_is_done_and_nothing_in_flight(self):
        t = self.now - 20
        path = self.db("conv-gone", [(graph.AG_STEP_TOOL, graph.AG_STATUS_RUNNING,
                                      tool_step(t, "c1", "read_file", {"Path": "C:\\a.txt"}))])
        later = self.now + graph.ANTIGRAVITY_ACTIVE_S + 60
        os.utime(path, (t, t))   # nothing written since the step
        parsed = graph.parse_antigravity_db(path, later, {}, present=False)
        node = parsed["nodes"]["antigravity:conv-gone"]
        self.assertEqual((node["state"], node["open"], parsed["open"]), ("done", False, {}))

    def test_push_is_a_ship_event_and_a_failed_step_marks_it_failed(self):
        t = self.now - 30
        path = self.db("conv-ship", [
            (graph.AG_STEP_TOOL, 3, tool_step(t, "p1", "run_command", {"CommandLine": "git push origin main"},
                                              PUSHED, t + 2)),
            (graph.AG_STEP_TOOL, 9, tool_step(t + 5, "p2", "run_command", {"CommandLine": "git push origin main"},
                                              "error: failed to push", t + 6)),
        ])
        ships = graph.parse_antigravity_db(path, self.now, {}, present=True)["ships"]
        self.assertEqual([(s["id"], s["step"], s["ok"]) for s in ships],
                         [("antigravity:conv-ship:p1:push", "push", True),
                          ("antigravity:conv-ship:p2:push", "push", False)])

    def test_subagent_hangs_off_its_parent(self):
        path = self.db("child-cid", [(graph.AG_STEP_USER, 3, text_step(graph.AG_STEP_USER, self.now - 10))])
        parsed = graph.parse_antigravity_db(path, self.now, {"parent_conversation_id": "parent-cid"}, present=True)
        self.assertEqual(parsed["nodes"]["antigravity:child-cid"]["kind"], "subagent")
        self.assertEqual(parsed["nodes"]["antigravity:child-cid"]["parent"], "antigravity:parent-cid")
        self.assertIn(("antigravity:parent-cid", "antigravity:child-cid", "spawn"), parsed["edges"])

    def test_summaries_and_workspace_path(self):
        make_summaries(self.root, [{"id": "c1", "title": "T", "workspaces": ["file:///C:/Users/x%20y"]}])
        rows = graph.antigravity_summaries(self.root)
        self.assertEqual(rows["c1"]["title"], "T")
        self.assertEqual(graph._workspace_path(rows["c1"]["workspace_uris"]), "C:/Users/x y")
        self.assertEqual(graph._workspace_path("not json"), "")
        self.assertEqual(graph.antigravity_summaries(self.root / "missing"), {})

    def test_store_reads_the_antigravity_folder(self):
        self.db("conv-store", [(graph.AG_STEP_TOOL, 3, tool_step(self.now - 5, "c1", "read_file",
                                                                  {"Path": "C:\\a.txt"}, "x", self.now - 4))])
        make_summaries(self.root, [{"id": "conv-store", "title": "Stored"}])
        empty = self.root / "none"
        with mock.patch.multiple(graph, ANTIGRAVITY_DIR=self.root, PROJECTS_DIR=empty,
                                 CODEX_SESSIONS_DIR=empty, PI_SESSIONS_DIR=empty, ROSTER_DIR=empty):
            store = graph.GraphStore.__new__(graph.GraphStore)
            store._files, store._lock, store._snapshot, store._events = {}, threading.Lock(), None, []
            store._ships = []
            store.refresh()
            snap = store.snapshot()
        agent = snap["nodes"]["antigravity:conv-store"]
        self.assertEqual((agent["title"], agent["domain"]), ("Stored", "antigravity"))

    def test_server_keeps_the_parsers_open_flag(self):
        import server
        nodes = [{"id": "antigravity:a", "kind": "agent", "provider": "antigravity", "open": True},
                 {"id": "antigravity:b", "kind": "subagent", "provider": "antigravity", "open": True,
                  "parent": "antigravity:a", "mtime": self.now},
                 {"id": "antigravity:c", "kind": "agent", "provider": "antigravity", "open": False}]
        server.mark_open(nodes, set(), self.now)
        self.assertEqual([n["open"] for n in nodes], [True, True, False])


if __name__ == "__main__":
    unittest.main()
