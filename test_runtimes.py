"""The runtime registry: a runtime registered in a test shows up on the graph with no other change, its cached
units are dropped when they go, and /api/graph lists every runtime. Run with: python -m unittest test_runtimes -v"""
import importlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import graph
import metrics

# The runtimes registered in code (the five built-ins, plus any native adapter added since), in order.
BUILTIN = [r.id for r in graph.RUNTIMES]


def parse_fake(path):
    """A made-up runtime's transcript: one JSON object per line, {"tool": name}. Only tool names are kept."""
    agent = f"fake:{path.stem}"
    nodes = {agent: {"kind": "agent", "label": path.stem, "provider": "fakert", "domain": "fakert",
                     "space": "fakert", "environment": "local", "mtime": path.stat().st_mtime}}
    edges = {}
    for row in path.read_text(encoding="utf-8").splitlines():
        tool = json.loads(row).get("tool")
        if not tool:
            continue
        nodes[f"tool:{tool}"] = {"kind": "tool", "label": tool, "group": "tools", "groupLabel": "tools"}
        edge = edges.setdefault((agent, f"tool:{tool}", "call"), {"count": 0, "lastAt": 0.0})
        edge["count"] += 1
    return {"agent": agent, "provider": "fakert", "nodes": nodes, "edges": edges, "open": {}}


def bare_store():
    store = graph.GraphStore.__new__(graph.GraphStore)   # no refresh thread in tests
    store._files, store._lock, store._snapshot, store._events, store._ships = {}, threading.Lock(), None, [], []
    return store


class RuntimeRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.fake_root = self.dir / "fake-sessions"
        self.fake_root.mkdir()
        self.parsed = []
        empty = self.dir / "none"
        # Point every built-in runtime at a missing folder, so only the fake one has anything to read.
        self.paths = mock.patch.multiple(graph, PROJECTS_DIR=empty, CODEX_SESSIONS_DIR=empty, PI_SESSIONS_DIR=empty,
                                         ANTIGRAVITY_DIR=empty, DSH_DIR=empty, ROSTER_DIR=empty)
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.registry = mock.patch.object(graph, "RUNTIMES", list(graph.RUNTIMES))   # restored after each test
        self.registry.start()
        self.addCleanup(self.registry.stop)
        self.addCleanup(self.tmp.cleanup)

    def fake_runtime(self, **over):
        def parse(path, key, now, ctx):
            if threading.current_thread() is threading.main_thread():   # not test_server's background GraphStore
                self.parsed.append(path.name)
            return parse_fake(path)
        args = dict(aliases=("fake-rt", "Fake RT"), roots=lambda: [self.fake_root],
                    find=lambda root: root.glob("*.jsonl"), parse=parse)
        args.update(over)
        return graph.Runtime("fakert", "Fake <b>RT</b>", "#12AB9C", **args)

    def write(self, name, tools, age=0.0):
        path = self.fake_root / name
        path.write_text("\n".join(json.dumps({"tool": t}) for t in tools), encoding="utf-8")
        if age:
            at = time.time() - age
            os.utime(path, (at, at))
        return path

    def test_registered_runtime_shows_up_on_the_graph(self):
        graph.register_runtime(self.fake_runtime())
        self.write("s1.jsonl", ["grep", "grep", "edit"])
        store = bare_store()
        store.refresh()
        snap = store.snapshot()
        self.assertEqual(snap["nodes"]["fake:s1"]["domain"], "fakert")
        calls = {(e["src"], e["dst"]): e["count"] for e in snap["edges"]}
        self.assertEqual(calls, {("fake:s1", "tool:grep"): 2, ("fake:s1", "tool:edit"): 1})
        self.assertEqual(snap["nodes"]["tool:grep"]["domains"], ["fakert"])   # resources take the runtime's domain

    def test_units_are_cached_by_key_and_dropped_when_gone_or_old(self):
        graph.register_runtime(self.fake_runtime())
        path = self.write("s1.jsonl", ["grep"])
        self.write("s2.jsonl", ["edit"])
        store = bare_store()
        store.refresh()
        store.refresh()
        self.assertEqual(sorted(self.parsed), ["s1.jsonl", "s2.jsonl"])   # unchanged files are not parsed again
        path.unlink()
        self.write("s2.jsonl", ["edit"], age=graph.GRAPH_WINDOW_S + 60)
        store.refresh()
        self.assertNotIn("fake:s1", store.snapshot()["nodes"])   # no longer found
        self.assertNotIn("fake:s2", store.snapshot()["nodes"])   # outside the window
        self.assertEqual(store._files, {})

    def test_unregistered_runtime_leaves_the_graph(self):
        graph.register_runtime(self.fake_runtime())
        self.write("s1.jsonl", ["grep"])
        store = bare_store()
        store.refresh()
        self.assertIn("fake:s1", store.snapshot()["nodes"])
        graph.unregister_runtime("fakert")
        store.refresh()
        self.assertNotIn("fake:s1", store.snapshot()["nodes"])
        self.assertNotIn("fakert", [r["id"] for r in store.snapshot()["runtimes"]])

    def test_parse_none_keeps_the_older_parse_and_drop_forgets_it(self):
        answer = {"value": None}
        runtime = graph.register_runtime(self.fake_runtime(
            parse=lambda path, key, now, ctx: answer["value"]))
        path = self.write("s1.jsonl", ["grep"])
        store = bare_store()
        store._files[(runtime.id, path)] = ("old key", parse_fake(path))
        store.refresh()
        self.assertIn("fake:s1", store.snapshot()["nodes"])   # None: the older parse stays
        answer["value"] = graph.DROP
        store.refresh()
        self.assertNotIn("fake:s1", store.snapshot()["nodes"])

    def test_snapshot_lists_runtimes_in_order(self):
        graph.register_runtime(self.fake_runtime())
        store = bare_store()
        store.refresh()
        runtimes = store.snapshot()["runtimes"]
        self.assertEqual([r["id"] for r in runtimes], BUILTIN + ["fakert"])
        self.assertEqual(runtimes[-1], {"id": "fakert", "label": "Fake <b>RT</b>", "color": "#12ab9c",
                                        "aliases": ["fake-rt", "fake rt"], "order": len(BUILTIN)})
        self.assertEqual([(r["label"], r["color"]) for r in runtimes[:5]],
                         [("Claude", "#e9a960"), ("GPT", "#9a88ff"), ("Pi", "#62d1c8"), ("Google", "#ef7fae"),
                          ("DeepSeek", "#4d6bfe")])

    def test_replacing_a_runtime_keeps_its_place(self):
        graph.register_runtime(self.fake_runtime())
        graph.register_runtime(graph.Runtime("codex", "Codex", "#000000", roots=lambda: [], find=lambda r: [],
                                             parse=lambda *a: None))
        self.assertEqual([r["id"] for r in graph.runtime_meta()], BUILTIN + ["fakert"])
        self.assertEqual(graph.runtime_meta()[1]["label"], "Codex")

    def test_bad_runtime_definitions_are_refused(self):
        for args in (("Bad_Id", "X", "#000000"), ("ok", "", "#000000"), ("ok", "X", "red"),
                     ("ok", "X" * 41, "#000000"), ("ok", "X", "#00000g")):
            with self.assertRaises(ValueError, msg=args):
                graph.Runtime(*args, roots=lambda: [], find=lambda r: [], parse=lambda *a: None)

    def test_graph_payload_carries_runtimes(self):
        # Importing server starts its refresh threads; keep them from reading this machine's sessions.
        with mock.patch.object(graph.GraphStore, "_loop", lambda self: None), \
                mock.patch.object(metrics.Metrics, "_loop", lambda self: None):
            server = importlib.import_module("server")
        graph.register_runtime(self.fake_runtime())
        self.write("s1.jsonl", ["grep"])
        store = bare_store()
        store.refresh()
        with mock.patch.object(server, "GRAPH", store), mock.patch.object(server, "JOBS_DIR", self.dir / "none"), \
                mock.patch.object(server, "open_session_ids", return_value=set()):
            payload = server.graph_payload()
        self.assertEqual([r["id"] for r in payload["runtimes"]], BUILTIN + ["fakert"])
        self.assertIn("fake:s1", [n["id"] for n in payload["nodes"]])
        for field in ("generatedAt", "nodes", "edges", "ships", "jevs", "activeWindow"):
            self.assertIn(field, payload)   # the existing fields are all still there


if __name__ == "__main__":
    unittest.main()
