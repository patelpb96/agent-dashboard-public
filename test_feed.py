"""Open feeds: <FEED_DIR>/<runtime>/<session>.jsonl drawn like any other runtime, read as untrusted input, and
written by feed.py. Run with: python -m unittest test_feed -v"""
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import feed
import graph
import metrics

HERE = Path(__file__).resolve().parent
SECRET = "SECRET-MESSAGE-TEXT-7f3a"   # never allowed anywhere in the payload


def bare_store():
    store = graph.GraphStore.__new__(graph.GraphStore)   # no refresh thread in tests
    store._files, store._lock, store._snapshot, store._events, store._ships = {}, threading.Lock(), None, [], []
    return store


class FeedTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.root = self.dir / "feeds"
        self.root.mkdir()
        self.now = time.time()
        empty = self.dir / "none"
        patches = [mock.patch.multiple(graph, PROJECTS_DIR=empty, CODEX_SESSIONS_DIR=empty, PI_SESSIONS_DIR=empty,
                                       ANTIGRAVITY_DIR=empty, DSH_DIR=empty, ROSTER_DIR=empty, FEED_DIR=self.root,
                                       SHARED_DIR=None),
                   mock.patch.object(graph, "RUNTIMES", list(graph.RUNTIMES))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def write(self, runtime, session, rows, raw=()):
        folder = self.root / runtime
        folder.mkdir(exist_ok=True)
        path = folder / f"{session}.jsonl"
        lines = [json.dumps({"v": 1, "t": self.now - 5, **row}) for row in rows] + list(raw)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def snap(self, store=None):
        store = store or bare_store()
        store.refresh()
        return store.snapshot()

    def payload(self, snap):
        # Importing server starts its refresh threads; keep them from reading this machine's sessions.
        with mock.patch.object(graph.GraphStore, "_loop", lambda self: None), \
                mock.patch.object(metrics.Metrics, "_loop", lambda self: None):
            server = importlib.import_module("server")
        store = mock.Mock()
        store.snapshot.return_value = snap
        with mock.patch.object(server, "GRAPH", store), mock.patch.object(server, "JOBS_DIR", self.dir / "none"), \
                mock.patch.object(server, "open_session_ids", return_value=set()):
            return server.graph_payload()

    # ---- a third-party runtime, end to end

    def test_third_party_runtime_is_named_coloured_and_drawn(self):
        (self.root / "opencode").mkdir()
        (self.root / "opencode" / "runtime.json").write_text(json.dumps(
            {"label": "OpenCode", "color": "#7BD88F", "aliases": ["oc", "anthropic", 7]}), encoding="utf-8")
        cwd = str(self.dir / "app")
        self.write("opencode", "s1", [
            {"type": "session", "name": "Fix login", "model": "gpt-5", "cwd": cwd, "role": "lead"},
            {"type": "tool", "name": "Bash", "target": "src/app.py"},
            {"type": "tool", "name": "WebFetch", "target": "https://example.com/x"},
            {"type": "file", "path": "src/app.py", "op": "edit"},
            {"type": "file", "path": "README.md", "op": "read"},
            {"type": "ship", "kind": "push", "ref": "main"},
            {"type": "ship", "kind": "pr", "ref": "https://github.com/acme/app/pull/7"},
            {"type": "ship", "kind": "commit", "ref": "main", "ok": False},
        ])
        snap = self.snap()
        meta = {r["id"]: r for r in snap["runtimes"]}["opencode"]
        self.assertEqual((meta["label"], meta["color"], meta["aliases"]), ("OpenCode", "#7bd88f", ["oc"]))
        agent = snap["nodes"]["feed:opencode:s1"]
        self.assertEqual((agent["kind"], agent["domain"], agent["label"], agent["model"], agent["open"], agent["state"]),
                         ("agent", "opencode", "Fix login", "gpt-5", True, "working"))
        app = graph._feed_path_key("src/app.py", cwd).replace("\\", "/")
        dsts = {e["dst"] for e in snap["edges"] if e["src"] == "feed:opencode:s1"}
        self.assertTrue({"tool:Bash", "shared:tool:shell", "doc:" + app, "web:WebFetch", "tool:Read",
                         "shared:tool:files"} <= dsts, dsts)
        self.assertEqual(snap["nodes"]["doc:" + app]["domains"], ["opencode"])
        ships = sorted((s["step"], s["ok"], s["branch"], s["url"]) for s in snap["ships"])
        self.assertEqual(ships, [("commit", False, "main", None), ("pr", True, None, "https://github.com/acme/app/pull/7"),
                                 ("push", True, "main", None)])
        self.assertTrue(all(s["folder"] == "doc:" + graph._feed_path_key(cwd).replace("\\", "/") for s in snap["ships"]))
        payload = self.payload(snap)
        self.assertTrue(next(n for n in payload["nodes"] if n["id"] == "feed:opencode:s1")["open"])   # self-tracked

    def test_runtime_without_runtime_json_gets_a_default_label_and_no_colour(self):
        self.write("my-tool", "s1", [{"type": "tool", "name": "Grep"}])
        meta = {r["id"]: r for r in self.snap()["runtimes"]}["my-tool"]
        self.assertEqual((meta["label"], meta["color"]), ("My Tool", None))   # the page hashes a colour

    def test_bad_runtime_json_fields_fall_back(self):
        (self.root / "rt").mkdir()
        (self.root / "rt" / "runtime.json").write_text(json.dumps({"label": "x" * 500, "color": "red"}), encoding="utf-8")
        self.write("rt", "s1", [{"type": "tool", "name": "Grep"}])
        meta = {r["id"]: r for r in self.snap()["runtimes"]}["rt"]
        self.assertEqual((len(meta["label"]), meta["color"]), (40, None))
        (self.root / "rt" / "runtime.json").write_text("{" * 5000, encoding="utf-8")   # oversized and broken
        meta = {r["id"]: r for r in self.snap()["runtimes"]}["rt"]
        self.assertEqual(meta["label"], "Rt")

    def test_folder_named_after_a_builtin_adds_sessions_without_replacing_it(self):
        claude = next(r for r in graph.RUNTIMES if r.id == "claude")
        self.write("claude", "hook-1", [{"type": "tool", "name": "Grep"}])
        self.write("openai", "hook-2", [{"type": "tool", "name": "Grep"}])   # an alias goes to its runtime
        snap = self.snap()
        self.assertIs(next(r for r in graph.RUNTIMES if r.id == "claude"), claude)
        self.assertEqual([r["id"] for r in snap["runtimes"]], ["claude", "codex", "pi", "antigravity", "deepseek"])
        self.assertEqual(snap["runtimes"][0]["label"], "Claude")
        self.assertEqual(snap["nodes"]["feed:claude:hook-1"]["domain"], "claude")
        self.assertEqual(snap["nodes"]["feed:codex:hook-2"]["domain"], "codex")

    def test_builtin_readers_keep_running_beside_a_feed_folder_of_the_same_id(self):
        projects = self.dir / "projects" / "proj"
        projects.mkdir(parents=True)
        (projects / "abc.jsonl").write_text(json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:00Z",
                                                        "message": {"role": "assistant", "content": []}}) + "\n")
        self.write("claude", "hook-1", [{"type": "tool", "name": "Grep"}])
        store = bare_store()
        with mock.patch.object(graph, "PROJECTS_DIR", self.dir / "projects"):
            store.refresh()
        self.assertIn(("claude", projects / "abc.jsonl"), store._files)
        self.assertIn(("feed:claude", self.root / "claude" / "hook-1.jsonl"), store._files)

    # ---- open / closed, subagents

    def test_open_until_the_window_passes_or_an_end_line(self):
        self.write("rt", "live", [{"type": "tool", "name": "Grep"}])
        self.write("rt", "ended", [{"type": "tool", "name": "Grep"}, {"type": "end"}])
        self.write("rt", "resumed", [{"type": "end"}, {"type": "tool", "name": "Grep"}])
        quiet = self.write("rt", "quiet", [{"type": "tool", "name": "Grep", "t": self.now - 3600}])
        os.utime(quiet, (self.now - 3600, self.now - 3600))
        nodes = self.snap()["nodes"]
        self.assertEqual({s: nodes[f"feed:rt:{s}"]["open"] for s in ("live", "ended", "resumed", "quiet")},
                         {"live": True, "ended": False, "resumed": True, "quiet": False})
        self.assertEqual(nodes["feed:rt:quiet"]["state"], "done")

    def test_a_line_from_the_future_does_not_keep_a_session_open(self):
        path = self.write("rt", "s1", [{"type": "tool", "name": "Grep", "t": self.now + 10 ** 6}])
        os.utime(path, (self.now - 3600, self.now - 3600))
        self.assertFalse(self.snap()["nodes"]["feed:rt:s1"]["open"])

    def test_parent_session_draws_a_spawn_edge_to_its_subagent(self):
        self.write("rt", "lead", [{"type": "session", "name": "Lead"}])
        self.write("rt", "child_1", [{"type": "session", "parent": "lead", "name": "Scout"},
                                     {"type": "tool", "name": "Grep"}])
        self.write("rt", "orphan", [{"type": "session", "parent": "gone"}])
        snap = self.snap()
        self.assertEqual(snap["nodes"]["feed:rt:child_1"]["kind"], "subagent")
        self.assertEqual(snap["nodes"]["feed:rt:child_1"]["parent"], "feed:rt:lead")
        self.assertEqual(snap["nodes"]["feed:rt:lead"]["label"], "Lead")   # the real parent wins over its stub
        spawns = {(e["src"], e["dst"]) for e in snap["edges"] if e["kind"] == "spawn"}
        self.assertEqual(spawns, {("feed:rt:lead", "feed:rt:child_1"), ("feed:rt:gone", "feed:rt:orphan")})

    # ---- untrusted input

    def test_malformed_and_oversized_lines_are_skipped(self):
        self.write("rt", "s1", [{"type": "tool", "name": "Good"}, {"type": "tool", "name": "x" * 5000},
                                {"type": "tool", "name": 5}, {"type": "mystery", "name": "Nope"},
                                {"type": "file", "path": "a.txt", "op": "delete"},
                                {"type": "ship", "kind": "deploy", "ref": "main"}],
                   raw=["not json", "[1,2]", '{"t": 1, "type": "tool", "name": "NoVersion"}',
                        '{"v": true, "t": 1, "type": "tool", "name": "BoolVersion"}',
                        '{"v": 1, "t": "yesterday", "type": "tool", "name": "BadTime"}',
                        '{"v": 1, "t": 1e999, "type": "tool", "name": "Inf"}', "[" * 3000, "\xff\xfe"])
        snap = self.snap()
        tools = {n["label"] for n in snap["nodes"].values() if n["kind"] == "tool" and not n.get("space") == "shared"}
        self.assertEqual(tools, {"Good"})
        self.assertEqual(snap["ships"], [])

    def test_only_the_tail_of_a_big_file_is_read(self):
        rows = [{"type": "tool", "name": f"T{i}"} for i in range(200)]
        self.write("rt", "big", rows)
        with mock.patch.object(graph, "FEED_TAIL_BYTES", 2000), mock.patch.object(graph, "FEED_LINES_MAX", 10):
            nodes = self.snap()["nodes"]
        names = {n["label"] for n in nodes.values() if n["kind"] == "tool" and n["label"].startswith("T")}
        self.assertEqual(names, {f"T{i}" for i in range(190, 200)})

    def test_long_strings_are_capped_and_control_characters_dropped(self):
        self.write("rt", "s1", [{"type": "session", "name": "a\u0000b c\n" + "n" * 900, "model": "m" * 900,
                                 "cwd": "/" + "c" * 1500}])
        agent = self.snap()["nodes"]["feed:rt:s1"]
        self.assertEqual(len(agent["name"]), graph.FEED_TEXT_MAX)
        self.assertTrue(agent["name"].startswith("a b c n"))
        self.assertEqual(len(agent["model"]), graph.FEED_TEXT_MAX)
        self.assertLessEqual(len(agent["cwd"]), graph.FEED_PATH_MAX)

    def test_bad_ids_are_skipped(self):
        for runtime, session in (("Bad_Runtime", "s1"), ("rt", "UPPER"), ("rt", "a--b"), ("rt", "x" * 129),
                                 ("-rt", "s1"), ("rt", "dots.in.name")):
            self.write(runtime, session, [{"type": "tool", "name": "Grep"}])
        (self.root / "rt" / "notes.txt").write_text("{}", encoding="utf-8")
        snap = self.snap()
        self.assertEqual([n for n in snap["nodes"] if n.startswith("feed:")], [])
        self.assertNotIn("bad-runtime", [r["id"] for r in snap["runtimes"]])

    def test_too_many_runtime_folders_and_sessions_are_capped(self):
        for i in range(5):
            self.write(f"rt{i}", "s1", [{"type": "tool", "name": "Grep"}])
        for i in range(5):
            path = self.write("rt0", f"s{i}x", [{"type": "tool", "name": "Grep"}])
            os.utime(path, (self.now - 100 + i, self.now - 100 + i))
        with mock.patch.object(graph, "FEED_RUNTIMES_MAX", 2), mock.patch.object(graph, "FEED_SESSIONS_MAX", 2):
            snap = self.snap()
        self.assertEqual([r["id"] for r in snap["runtimes"]][5:], ["rt0", "rt1"])
        self.assertEqual(sorted(n for n in snap["nodes"] if n.startswith("feed:rt0:")),
                         ["feed:rt0:s1", "feed:rt0:s4x"])   # the two newest

    def link_or_skip(self, make):
        try:
            make()
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"cannot make a link here: {exc}")

    def test_symlinked_folder_and_file_are_skipped(self):
        outside = self.dir / "outside"
        outside.mkdir()
        (outside / "s1.jsonl").write_text(json.dumps({"v": 1, "t": self.now, "type": "tool", "name": "Grep"}) + "\n")
        self.write("rt", "real", [{"type": "tool", "name": "Grep"}])
        self.link_or_skip(lambda: os.symlink(outside, self.root / "linked", target_is_directory=True))
        self.link_or_skip(lambda: os.symlink(outside / "s1.jsonl", self.root / "rt" / "s2.jsonl"))
        nodes = self.snap()["nodes"]
        self.assertIn("feed:rt:real", nodes)
        self.assertNotIn("feed:linked:s1", nodes)
        self.assertNotIn("feed:rt:s2", nodes)

    def test_link_checks_without_making_links(self):
        # Making a symlink needs a privilege some machines lack, so the checks are also run on made-up stats.
        import stat as st_mod

        def fake(mode, attrs=0, ino=1, nlink=1):
            return mock.Mock(st_mode=mode, st_file_attributes=attrs, st_ino=ino, st_dev=1, st_nlink=nlink,
                             st_size=10, st_mtime=self.now)
        self.assertFalse(graph._plain(fake(st_mod.S_IFLNK | 0o777)))
        self.assertFalse(graph._plain(fake(st_mod.S_IFDIR, attrs=0x400)))   # a junction is a reparse point
        self.assertTrue(graph._plain(fake(st_mod.S_IFREG)))
        path = self.write("rt", "s1", [{"type": "tool", "name": "Grep"}])
        with mock.patch("os.lstat", return_value=fake(st_mod.S_IFLNK | 0o777)):
            self.assertIsNone(graph._feed_open(path))
            self.assertIs(graph._feed_key(path, self.now), graph.DROP)
        real = os.lstat(path)

        def opened_as(ino, nlink):
            seen = fake(st_mod.S_IFREG, ino=ino, nlink=nlink)
            seen.st_dev = real.st_dev
            return mock.patch("os.fstat", lambda fd: seen)
        with opened_as(real.st_ino + 1, 1):
            self.assertIsNone(graph._feed_open(path))   # swapped for another file after the check
        with opened_as(real.st_ino, 2):
            self.assertIsNone(graph._feed_open(path))   # hard-linked
        with opened_as(real.st_ino, 1):
            f, _ = graph._feed_open(path)
            f.close()

    @unittest.skipUnless(os.name == "nt", "junctions are Windows-only")
    def test_junction_folder_is_skipped(self):
        import _winapi
        outside = self.dir / "outside"
        outside.mkdir()
        (outside / "s1.jsonl").write_text(json.dumps({"v": 1, "t": self.now, "type": "tool", "name": "Grep"}) + "\n")
        self.link_or_skip(lambda: _winapi.CreateJunction(str(outside), str(self.root / "junction")))
        snap = self.snap()
        self.assertNotIn("feed:junction:s1", snap["nodes"])
        self.assertNotIn("junction", [r["id"] for r in snap["runtimes"]])

    def test_hard_linked_file_is_skipped(self):
        outside = self.dir / "elsewhere.jsonl"
        outside.write_text(json.dumps({"v": 1, "t": self.now, "type": "tool", "name": "Grep"}) + "\n")
        (self.root / "rt").mkdir()
        self.link_or_skip(lambda: os.link(outside, self.root / "rt" / "s1.jsonl"))
        self.assertNotIn("feed:rt:s1", self.snap()["nodes"])

    def test_unc_root_is_never_read(self):
        with mock.patch.object(graph, "FEED_DIR", Path("\\\\server\\share\\feeds")), \
                mock.patch("os.scandir", side_effect=AssertionError("scandir was called")), \
                mock.patch("os.lstat", side_effect=AssertionError("lstat was called")):
            self.assertEqual(graph.sync_feeds(), [])

    def test_paths_named_in_events_are_never_touched(self):
        touched = []
        real_stat, real_lstat, real_resolve = os.stat, os.lstat, Path.resolve

        def guard(real):
            def call(path, *args, **kwargs):
                if "never-touch" in str(path):
                    touched.append(str(path))
                return real(path, *args, **kwargs)
            return call
        self.write("rt", "s1", [{"type": "session", "cwd": str(self.dir / "never-touch-cwd")},
                                {"type": "tool", "name": "Write", "target": "never-touch/a.py"},
                                {"type": "file", "path": "\\\\evil-host\\never-touch\\x", "op": "read"},
                                {"type": "ship", "kind": "push", "ref": "main"}])
        with mock.patch("os.stat", guard(real_stat)), mock.patch("os.lstat", guard(real_lstat)), \
                mock.patch.object(Path, "resolve", guard(real_resolve)), \
                mock.patch("os.path.realpath", side_effect=AssertionError("realpath was called")):
            nodes = self.snap()["nodes"]
        self.assertEqual(touched, [])
        self.assertIn("doc:" + graph._feed_path_key("never-touch/a.py", str(self.dir / "never-touch-cwd"))
                      .replace("\\", "/"), nodes)

    def test_feed_file_paths_land_on_the_same_node_as_other_runtimes(self):
        real = self.dir / "proj" / "a.py"
        real.parent.mkdir()
        real.write_text("x")
        self.assertEqual(graph._feed_path_key(str(real)), graph._path_key(str(real)))
        self.assertEqual(graph._feed_path_key("a.py", str(real.parent)), graph._path_key("a.py", str(real.parent)))

    def test_message_text_never_reaches_the_payload(self):
        self.write("rt", "s1", [
            {"type": "session", "name": "ok", "prompt": SECRET, "text": SECRET, "content": [SECRET]},
            {"type": "tool", "name": "Bash", "target": "a.py", "output": SECRET, "input": {"command": SECRET}},
            {"type": "file", "path": "a.py", "op": "edit", "content": SECRET, "diff": SECRET},
            {"type": "ship", "kind": "commit", "ref": "main", "message": SECRET},
            {"type": "message", "text": SECRET}, {"type": "end", "summary": SECRET},
        ])
        snap = self.snap()
        self.assertIn("feed:rt:s1", snap["nodes"])
        self.assertNotIn(SECRET, json.dumps(self.payload(snap), default=str))
        self.assertNotIn(SECRET, json.dumps(snap, default=str))

    def test_one_bad_feed_does_not_break_the_refresh(self):
        self.write("rt", "good", [{"type": "tool", "name": "Grep"}])
        self.write("rt", "bad", [{"type": "tool", "name": "Grep"}])
        real = graph.parse_feed_file

        def flaky(path, *args, **kwargs):
            if Path(path).stem == "bad":
                raise RuntimeError("boom")
            return real(path, *args, **kwargs)
        with mock.patch.object(graph, "parse_feed_file", flaky):
            nodes = self.snap()["nodes"]
        self.assertIn("feed:rt:good", nodes)
        self.assertNotIn("feed:rt:bad", nodes)
        with mock.patch.object(graph, "sync_feeds", side_effect=RuntimeError("boom")):
            self.assertIsNotNone(self.snap()["generatedAt"])   # the other runtimes still publish

    def test_removed_runtime_folder_is_unregistered(self):
        import shutil
        self.write("rt", "s1", [{"type": "tool", "name": "Grep"}])
        store = bare_store()
        self.assertIn("rt", [r["id"] for r in self.snap(store)["runtimes"]])
        shutil.rmtree(self.root / "rt")
        snap = self.snap(store)
        self.assertNotIn("rt", [r["id"] for r in snap["runtimes"]])
        self.assertNotIn("feed:rt:s1", snap["nodes"])
        self.assertEqual(store._files, {})

    def test_feed_and_code_share_the_id_rules(self):
        self.assertEqual(feed.RUNTIME_ID.pattern, graph.RUNTIME_ID.pattern)
        self.assertEqual(feed.SESSION_ID.pattern, graph.FEED_SESSION_ID.pattern)
        self.assertEqual((feed.TEXT_MAX, feed.PATH_MAX, feed.ROLE_MAX, feed.LINE_MAX, feed.SESSION_ID_MAX),
                         (graph.FEED_TEXT_MAX, graph.FEED_PATH_MAX, graph.FEED_ROLE_MAX, graph.FEED_LINE_MAX,
                          graph.FEED_SESSION_MAX))

    # ---- feed.py

    def test_writer_round_trips_through_the_reader(self):
        feed.describe_runtime("opencode", label="OpenCode", color="#7bd88f", aliases=["oc"], root=self.root)
        lead = feed.FeedWriter("opencode", "lead", root=self.root)
        lead.session(name="Lead", model="gpt-5", cwd=str(self.dir))
        lead.tool("Bash", target="src/app.py")
        lead.file("src/app.py", "edit")
        lead.ship("pr", ref="https://github.com/acme/app/pull/9")
        child = feed.FeedWriter("opencode", "lead_sub-1", root=self.root)
        child.session(parent="lead", name="Scout")
        child.tool("Grep")
        child.end()
        snap = self.snap()
        meta = {r["id"]: r for r in snap["runtimes"]}["opencode"]
        self.assertEqual((meta["label"], meta["color"], meta["aliases"]), ("OpenCode", "#7bd88f", ["oc"]))
        self.assertEqual(snap["nodes"]["feed:opencode:lead"]["model"], "gpt-5")
        self.assertTrue(snap["nodes"]["feed:opencode:lead"]["open"])
        self.assertFalse(snap["nodes"]["feed:opencode:lead_sub-1"]["open"])
        self.assertEqual(snap["nodes"]["feed:opencode:lead_sub-1"]["parent"], "feed:opencode:lead")
        self.assertEqual([s["url"] for s in snap["ships"]], ["https://github.com/acme/app/pull/9"])

    def test_writer_refuses_bad_input(self):
        for call in (lambda: feed.FeedWriter("Bad", "s1", root=self.root),
                     lambda: feed.FeedWriter("rt", "s 1", root=self.root),
                     lambda: feed.FeedWriter("rt", "s1", root="\\\\host\\share"),
                     lambda: feed.FeedWriter("rt", "s1", root=self.root).file("a", "delete"),
                     lambda: feed.FeedWriter("rt", "s1", root=self.root).ship("deploy"),
                     lambda: feed.FeedWriter("rt", "s1", root=self.root).session(parent="Not Valid"),
                     lambda: feed.FeedWriter("rt", "s1", root=self.root).tool(""),
                     lambda: feed.describe_runtime("rt", color="red", root=self.root)):
            with self.assertRaises(ValueError):
                call()
        long = feed.FeedWriter("rt", "s1", root=self.root).tool("x" * 900, target="y" * 5000)
        self.assertEqual((len(long["name"]), len(long["target"])), (feed.TEXT_MAX, feed.PATH_MAX))

    def test_two_processes_appending_at_once_never_tear_a_line(self):
        script = ("import sys; sys.path.insert(0, sys.argv[1]); import feed\n"
                  "w = feed.FeedWriter('rt', 'shared', root=sys.argv[2])\n"
                  "for i in range(200): w.tool('T' + sys.argv[3], target='p' * 900)\n")
        procs = [subprocess.Popen([sys.executable, "-c", script, str(HERE), str(self.root), str(n)]) for n in range(3)]
        for p in procs:
            self.assertEqual(p.wait(timeout=60), 0)
        lines = (self.root / "rt" / "shared.jsonl").read_bytes().split(b"\n")
        self.assertEqual(lines[-1], b"")
        self.assertEqual(len(lines) - 1, 600)
        self.assertTrue(all(json.loads(line)["type"] == "tool" for line in lines[:-1]))

    def test_cli_writes_what_the_reader_draws(self):
        base = ["--runtime", "my-cli", "--session", "abc", "--root", str(self.root)]
        self.assertEqual(feed.main(["--runtime", "my-cli", "--root", str(self.root), "describe", "--label", "My CLI",
                                    "--color", "#123456", "--alias", "mc"]), 0)
        self.assertEqual(feed.main(base + ["session", "--name", "Hook run", "--cwd", str(self.dir)]), 0)
        self.assertEqual(feed.main(base + ["tool", "--name", "Bash", "--target", "src/app.py"]), 0)
        self.assertEqual(feed.main(base + ["file", "--path", "src/app.py", "--op", "write"]), 0)
        self.assertEqual(feed.main(base + ["ship", "--kind", "push", "--ref", "main", "--failed"]), 0)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(feed.main(["--runtime", "BAD", "--session", "abc", "--root", str(self.root), "end"]), 1)
        out = subprocess.run([sys.executable, str(HERE / "feed.py"), *base, "tool", "--name", "Grep"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        snap = self.snap()
        self.assertEqual({r["id"]: r for r in snap["runtimes"]}["my-cli"]["label"], "My CLI")
        self.assertEqual(snap["nodes"]["feed:my-cli:abc"]["label"], "Hook run")
        dsts = {e["dst"] for e in snap["edges"] if e["src"] == "feed:my-cli:abc"}
        self.assertIn("tool:Grep", dsts)
        self.assertEqual([(s["step"], s["ok"]) for s in snap["ships"]], [("push", False)])


if __name__ == "__main__":
    unittest.main()
