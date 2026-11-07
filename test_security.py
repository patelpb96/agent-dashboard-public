"""Regressions for the security review of the runtime registry, open feeds and docs (16279a2..adapter-registry).
One class per finding. Run with: python -m unittest test_security -v"""
import importlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import feed
import graph
import metrics
from test_antigravity import make_db, make_summaries, tool_step
from test_dsh import PUSHED, call, result, write_session, zstd_compress


def bare_store():
    store = graph.GraphStore.__new__(graph.GraphStore)   # no refresh thread in tests
    store._files, store._lock, store._snapshot, store._events, store._ships = {}, threading.Lock(), None, [], []
    return store


def payload_of(snap, tmp):
    """server.graph_payload() for this snapshot, without starting the server's refresh threads."""
    with mock.patch.object(graph.GraphStore, "_loop", lambda self: None), \
            mock.patch.object(metrics.Metrics, "_loop", lambda self: None):
        server = importlib.import_module("server")
    store = mock.Mock()
    store.snapshot.return_value = snap
    with mock.patch.object(server, "GRAPH", store), mock.patch.object(server, "JOBS_DIR", tmp / "none"), \
            mock.patch.object(server, "open_session_ids", return_value=set()):
        return server.graph_payload()


class TempDirTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.now = time.time()

    def only(self, **dirs):
        """Point every runtime at a missing folder except the ones given."""
        empty = self.dir / "none"
        paths = dict(PROJECTS_DIR=empty, CODEX_SESSIONS_DIR=empty, PI_SESSIONS_DIR=empty, ANTIGRAVITY_DIR=empty,
                     DSH_DIR=empty, FEED_DIR=empty, ROSTER_DIR=empty)
        paths.update(dirs)
        for p in (mock.patch.multiple(graph, **paths), mock.patch.object(graph, "RUNTIMES", list(graph.RUNTIMES))):
            p.start()
            self.addCleanup(p.stop)


# ---- 1. Node is looked up on PATH only, never in the current folder

class NodeLookupTest(TempDirTest):
    def plant(self, folder):
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("node.cmd", "node.bat", "node.exe", "node"):
            (folder / name).write_text("@echo planted\n")
            os.chmod(folder / name, 0o755)

    def lookup(self, cwd, path_entries):
        old = os.getcwd()
        os.chdir(cwd)
        try:
            env = {"PATH": os.pathsep.join(path_entries)}
            with mock.patch.dict(os.environ, env):
                os.environ.pop("NoDefaultCurrentDirectoryInExePath", None)   # Claude Code's shell sets it
                return graph.node_executable()
        finally:
            os.chdir(old)

    def test_a_node_planted_in_the_current_folder_is_never_chosen(self):
        cwd = self.dir / "project"
        self.plant(cwd)
        found = self.lookup(cwd, ["", ".", "project", str(cwd), str(cwd) + os.sep, str(cwd).upper()])
        self.assertIsNone(found)

    def test_node_on_a_real_path_entry_is_found_as_an_absolute_path(self):
        cwd, bin_dir = self.dir / "project", self.dir / "bin"
        self.plant(cwd)
        self.plant(bin_dir)
        found = self.lookup(cwd, [".", str(bin_dir)])
        self.assertTrue(found and os.path.isabs(found), found)
        self.assertEqual(Path(found).parent, bin_dir)
        if os.name == "nt":
            self.assertEqual(Path(found).name, "node.exe")   # never a .cmd/.bat run through cmd.exe

    @unittest.skipUnless(os.name == "nt", "the current-folder search is Windows behaviour")
    def test_shutil_which_would_have_taken_the_planted_one(self):
        # Shows the test above guards a real hole: shutil.which searches the current folder first on Windows.
        cwd = self.dir / "project"
        self.plant(cwd)
        old = os.getcwd()
        os.chdir(cwd)
        try:
            with mock.patch.dict(os.environ, {"PATH": str(self.dir / "empty")}):
                os.environ.pop("NoDefaultCurrentDirectoryInExePath", None)
                planted = shutil.which("node")
                planted_dir = Path(planted).resolve().parent if planted else None
        finally:
            os.chdir(old)
        if planted is None:
            self.skipTest("this Python's shutil.which does not search the current folder")
        self.assertEqual(planted_dir, cwd.resolve())


# ---- 2. Ship parsing is linear, and reads a bounded slice of the output

class ShipRegexTest(unittest.TestCase):
    BOUND_S = 2.0   # generous: the fixed patterns take milliseconds here; the old one took minutes

    def timed(self, fn):
        start = time.perf_counter()
        fn()
        return time.perf_counter() - start

    def test_push_ref_is_linear_on_blank_lines(self):
        text = " \n" * 100_000   # ~200 KB
        self.assertLess(self.timed(lambda: list(graph.PUSH_REF.finditer(text))), self.BOUND_S)

    def test_other_ship_patterns_are_linear_on_hostile_text(self):
        for pattern, text in ((graph.TRACKING, "branch '" * 25_000), (graph.COMMIT_LINE, "[" + "a 1234567" * 20_000),
                              (graph.PR_URL, "https://github.com/" + "a" * 200_000),
                              (graph.EXIT_CODE, "exit code " * 20_000)):
            self.assertLess(self.timed(lambda: list(pattern.finditer(text))), self.BOUND_S, pattern.pattern)

    def test_the_three_push_line_shapes_still_match(self):
        for line, want in ((" * [new branch]      feature-x -> feature-x", ("feature-x", "feature-x")),
                           ("   1a2b3c4..5d6e7f8  main -> main", ("main", "main")),
                           (" + a1b2...c3d4 x -> x", ("x", "x"))):
            self.assertEqual(graph.PUSH_REF.search(line).groups(), want)

    PUSH = [{"step": "push", "branch": None, "remote": "origin", "cwd": None}]

    def judge(self, text):
        return [(e["branch"], e["ok"]) for e in graph.ship_events("a", "c", self.PUSH, text, 1.0)]

    def test_a_push_line_in_the_middle_of_large_output_is_judged(self):
        middle = "build line\n" * 3000 + "To github.com:o/r.git\n   1a2b..3c4d  main -> main\n" + "test line\n" * 3000
        self.assertEqual(self.judge(middle), [("main", True)])

    def test_an_error_line_in_the_middle_of_large_output_fails_the_push(self):
        text = "   1a2b..3c4d  main -> main\n" + "x\n" * 10000 + "error: failed to push some refs\n" + "y\n" * 10000
        self.assertEqual(self.judge(text), [("main", False)])

    def test_a_push_line_at_16_kb_keeps_its_branch(self):
        # the old 16 KB slice cut this line in two and lost the branch
        text = "x" * (16 * 1024 - 31) + "\n   1a2b..3c4d  feature-x -> feature-x\n" + "y\n" * 20000
        self.assertEqual(self.judge(text), [("feature-x", True)])

    def test_the_whole_output_is_scanned_fast(self):
        for pattern, text in ((graph.PUSH_REF, " \n" * 2_000_000), (graph.COMMIT_LINE, ("[" + "a " * 150 + "\n") * 13000),
                              (graph.TRACKING, "branch '" + "a" * 4_000_000),
                              (graph.PR_URL, "https://github.com/" + "a" * 4_000_000)):
            self.assertLess(self.timed(lambda: list(pattern.finditer(text))), self.BOUND_S * 2, pattern.pattern)
        text = "x" * 10 + " \n" * 2_000_000 + "To github.com:acme/app.git\n   1a2b3c4..5d6e7f8  main -> main\n"   # ~4 MB
        took = self.timed(lambda: self.assertEqual(self.judge(text), [("main", True)]))
        self.assertLess(took, self.BOUND_S * 2)

    def test_claude_transcript_with_blank_line_output_parses_fast(self):
        # repro_redos.py: a `git commit` whose hook printed thousands of blank lines
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            t = "2026-11-06T12:00:00Z"
            lines = [{"type": "assistant", "timestamp": t, "cwd": "C:\\work", "message": {"role": "assistant", "content": [
                         {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "git commit -m wip"}}]}},
                     {"type": "user", "timestamp": t, "message": {"role": "user", "content": [
                         {"type": "tool_result", "tool_use_id": "tu1", "content": "husky > pre-commit\n" + " \n" * 15000}]}}]
            path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
            self.assertLess(self.timed(lambda: graph.parse_file(path)), self.BOUND_S)


# ---- 3. One failing unit never stops the refresh

class FailingUnitTest(TempDirTest):
    def stall_db(self, folder):
        """repro_stall.py: an Antigravity conversation whose tool arguments nest 5000 deep."""
        folder.mkdir(parents=True, exist_ok=True)
        make_db(folder / "c1.db", [(graph.AG_STEP_TOOL, 3, tool_step(self.now - 5, "call1", "run_command", {}))])
        con = sqlite3.connect(folder / "c1.db")
        args = ("[" * 5000 + "]" * 5000).encode()
        from test_antigravity import field
        meta = field(1, b"") + field(4, field(1, b"call1") + field(2, b"run_command") + field(3, args))
        con.execute("update steps set step_payload = ?", (field(1, graph.AG_STEP_TOOL) + field(5, meta),))
        con.commit()
        con.close()

    def test_deeply_nested_antigravity_args_do_not_stop_the_refresh(self):
        agy, feeds = self.dir / "agy", self.dir / "feeds"
        self.stall_db(agy / "conversations")
        (feeds / "demo").mkdir(parents=True)
        (feeds / "demo" / "s1.jsonl").write_text(json.dumps({"v": 1, "t": self.now, "type": "tool", "name": "Grep"}) + "\n")
        self.only(ANTIGRAVITY_DIR=agy, FEED_DIR=feeds)
        store = bare_store()
        store.refresh()
        store.refresh()
        snap = store.snapshot()
        self.assertIsNotNone(snap["generatedAt"])
        self.assertIn("feed:demo:s1", snap["nodes"])
        self.assertIn("antigravity:c1", snap["nodes"])   # the parser itself now survives it too

    def test_a_raising_parser_is_isolated_and_not_retried_until_its_file_changes(self):
        folder = self.dir / "rt"
        folder.mkdir()
        (folder / "bad.jsonl").write_text("x")
        (folder / "good.jsonl").write_text("x")
        self.only()
        calls = []

        def parse(path, key, now, ctx):
            if threading.current_thread() is threading.main_thread():   # not test_server's background GraphStore
                calls.append(path.name)
            if path.stem == "bad":
                raise RecursionError("boom")
            agent = "rt:" + path.stem
            return {"agent": agent, "nodes": {agent: {"kind": "agent", "label": path.stem, "domain": "rt"}},
                    "edges": {}, "open": {}}
        graph.register_runtime(graph.Runtime("rt", "RT", None, roots=lambda: [folder],
                                             find=lambda root: root.glob("*.jsonl"), parse=parse))
        store = bare_store()
        store.refresh()
        store.refresh()
        snap = store.snapshot()
        self.assertIn("rt:good", snap["nodes"])
        self.assertNotIn("rt:bad", snap["nodes"])
        self.assertEqual(sorted(calls), ["bad.jsonl", "good.jsonl"])   # neither parsed again: keys unchanged
        os.utime(folder / "bad.jsonl", (self.now + 5, self.now + 5))
        store.refresh()
        self.assertEqual(calls.count("bad.jsonl"), 2)

    def test_a_raising_key_or_find_does_not_stop_other_runtimes(self):
        feeds = self.dir / "feeds"
        (feeds / "demo").mkdir(parents=True)
        (feeds / "demo" / "s1.jsonl").write_text(json.dumps({"v": 1, "t": self.now, "type": "tool", "name": "Grep"}) + "\n")
        self.only(FEED_DIR=feeds)

        def boom(*args):
            raise ValueError("boom")
        graph.register_runtime(graph.Runtime("rt-key", "K", None, roots=lambda: [self.dir], find=lambda r: [r],
                                             key=boom, parse=lambda *a: None))
        graph.register_runtime(graph.Runtime("rt-find", "F", None, roots=lambda: [self.dir], find=boom,
                                             parse=lambda *a: None))
        store = bare_store()
        store.refresh()
        self.assertIn("feed:demo:s1", store.snapshot()["nodes"])

    def test_deep_dsh_arguments_and_nul_paths_are_harmless(self):
        deep = current = {}
        for _ in range(5000):
            current["a"] = {}
            current = current["a"]
        clipped = graph._dsh_clip(deep)
        depth = 0
        while isinstance(clipped, dict) and clipped:
            clipped, depth = clipped.get("a"), depth + 1
        self.assertLessEqual(depth, graph.DSH_CLIP_DEPTH + 1)
        self.assertTrue(graph._path_key("C:\\work\\a\x00b.py"))


# ---- 4. feed.py never writes through a link

class FeedWriterLinkTest(TempDirTest):
    def link_or_skip(self, make):
        try:
            make()
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"cannot make a link here: {exc}")

    def test_hard_link_at_the_session_file_is_refused(self):
        root = self.dir / "feeds"
        (root / "rt").mkdir(parents=True)
        victim = self.dir / "victim.txt"
        victim.write_text("IMPORTANT USER DATA\n")
        self.link_or_skip(lambda: os.link(victim, root / "rt" / "s1.jsonl"))
        with self.assertRaises(ValueError):
            feed.FeedWriter("rt", "s1", root=root).tool("Bash", target="x")
        self.assertEqual(victim.read_text(), "IMPORTANT USER DATA\n")

    def test_hard_link_at_the_temp_name_or_runtime_json_is_never_written_through(self):
        root = self.dir / "feeds"
        (root / "rt").mkdir(parents=True)
        victim = self.dir / "victim.txt"
        victim.write_text("original config\n")
        self.link_or_skip(lambda: os.link(victim, root / "rt" / f".runtime.{os.getpid()}.tmp"))
        os.link(victim, root / "rt" / "runtime.json")
        feed.describe_runtime("rt", label="X", root=root)
        self.assertEqual(victim.read_text(), "original config\n")
        self.assertEqual(json.loads((root / "rt" / "runtime.json").read_text()), {"label": "X"})
        self.assertEqual([p.name for p in (root / "rt").glob(".runtime.*.tmp") if p.stat().st_nlink == 1], [])

    @unittest.skipUnless(os.name == "nt", "junctions are Windows-only")
    def test_junction_as_runtime_folder_is_refused(self):
        import _winapi
        root, elsewhere = self.dir / "feeds", self.dir / "elsewhere"
        root.mkdir()
        elsewhere.mkdir()
        self.link_or_skip(lambda: _winapi.CreateJunction(str(elsewhere), str(root / "rt")))
        with self.assertRaises(ValueError):
            feed.FeedWriter("rt", "s1", root=root).tool("Bash")
        with self.assertRaises(ValueError):
            feed.describe_runtime("rt", label="X", root=root)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_link_swapped_in_while_opening_is_refused(self):
        root = self.dir / "feeds"
        w = feed.FeedWriter("rt", "s1", root=root)
        w.tool("Grep")
        real_lstat = os.lstat
        calls = []

        def lstat(path, *args, **kwargs):   # the second look at the path (after opening) sees another file
            st = real_lstat(path, *args, **kwargs)
            calls.append(path)
            if str(path).endswith("s1.jsonl") and len([c for c in calls if str(c).endswith("s1.jsonl")]) == 2:
                return os.stat_result((st.st_mode, st.st_ino + 1, *tuple(st)[2:]))
            return st
        with mock.patch("os.lstat", lstat), self.assertRaises(ValueError):
            w.tool("Bash")


# ---- 5. Antigravity reads only real tables, under a deadline

class AntigravityQueryTest(TempDirTest):
    def test_a_view_named_steps_is_refused(self):
        # repro_view.py: a recursive view that would run forever
        conv = self.dir / "conversations"
        conv.mkdir()
        db = conv / "c2.db"
        con = sqlite3.connect(db)
        con.execute("create view steps as with recursive r(i) as (select 1 union all select i+1 from r) "
                    "select i as idx, 132 as step_type, 3 as status, x'' as step_payload from r")
        con.execute("create table gen_metadata(idx integer, data blob)")
        con.commit()
        con.close()
        done = []
        thread = threading.Thread(target=lambda: done.append(graph.parse_antigravity_db(db)), daemon=True)
        thread.start()
        thread.join(15)
        self.assertEqual(done, [None])

    def test_a_view_named_conversation_summaries_is_refused(self):
        con = sqlite3.connect(self.dir / "conversation_summaries.db")
        con.execute("create view conversation_summaries as select 'c' as conversation_id, 't' as title, '' as "
                    "workspace_uris, '' as status, '' as parent_conversation_id, '' as agent_name, 1 as "
                    "not_fully_idle, 0 as killed")
        con.commit()
        con.close()
        self.assertEqual(graph.antigravity_summaries(self.dir), {})

    def test_queries_stop_at_the_deadline(self):
        db = self.dir / "slow.db"
        sqlite3.connect(db).close()
        con = graph._sqlite_ro(db, deadline_s=0.2)
        start = time.monotonic()
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("with recursive r(i) as (select 1 union all select i+1 from r) select count(*) from r").fetchall()
        con.close()
        self.assertLess(time.monotonic() - start, 5)


# ---- 6. Runtime ids that are JavaScript built-in names

class ReservedIdTest(TempDirTest):
    def test_object_prototype_names_are_refused_as_runtime_ids(self):
        for name in ("constructor", "tostring", "valueof", "hasownproperty"):
            self.assertFalse(graph.runtime_id_ok(name), name)
            with self.assertRaises(ValueError):
                graph.Runtime(name, "X", None, roots=lambda: [], find=lambda r: [], parse=lambda *a: None)
            with self.assertRaises(ValueError):
                feed.FeedWriter(name, "s1", root=self.dir)
        rt = graph.Runtime("ok", "X", None, aliases=("constructor", "fine"), roots=lambda: [], find=lambda r: [],
                           parse=lambda *a: None)
        self.assertEqual(rt.aliases, ("fine",))
        self.assertEqual(feed.JS_RESERVED, graph.JS_RESERVED)

    def test_a_feed_folder_named_constructor_is_skipped(self):
        feeds = self.dir / "feeds"
        (feeds / "constructor").mkdir(parents=True)
        (feeds / "constructor" / "s1.jsonl").write_text(
            json.dumps({"v": 1, "t": self.now, "type": "tool", "name": "Grep"}) + "\n")
        self.only(FEED_DIR=feeds)
        store = bare_store()
        store.refresh()
        snap = store.snapshot()
        self.assertNotIn("constructor", [r["id"] for r in snap["runtimes"]])
        self.assertNotIn("feed:constructor:s1", snap["nodes"])


# ---- 7. Privacy: credentials in remotes and details, title caps, DeepSeek Harness result text

TOKEN = "ghp_SECRETTOKEN123"


class PrivacyTest(TempDirTest):
    def test_a_token_in_a_push_remote_never_reaches_the_payload(self):
        proj = self.dir / "projects" / "p"
        proj.mkdir(parents=True)
        t = "2026-11-06T12:00:00Z"
        command = f"git push https://me:{TOKEN}@github.com/acme/app.git main"
        lines = [{"type": "assistant", "timestamp": t, "cwd": "C:\\work", "message": {"role": "assistant", "content": [
                     {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": command}}]}},
                 {"type": "user", "timestamp": t, "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": "tu1",
                      "content": "To github.com:acme/app.git\n   1a2b3c4..5d6e7f8  main -> main\n"}]}}]
        (proj / "s1.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        self.only(PROJECTS_DIR=self.dir / "projects")
        store = bare_store()
        store.refresh()
        snap = store.snapshot()
        self.assertEqual([(s["step"], s["remote"]) for s in snap["ships"]], [("push", "https://github.com/acme/app.git")])
        self.assertNotIn(TOKEN, json.dumps(payload_of(snap, self.dir), default=str))
        self.assertNotIn(TOKEN, json.dumps(store._events, default=str))   # the flow view's details too

    def test_feed_targets_lose_url_credentials(self):
        feeds = self.dir / "feeds"
        (feeds / "rt").mkdir(parents=True)
        (feeds / "rt" / "s1.jsonl").write_text(json.dumps(
            {"v": 1, "t": self.now, "type": "tool", "name": "WebFetch", "target": f"https://u:{TOKEN}@example.com/x"}) + "\n")
        self.only(FEED_DIR=feeds)
        store = bare_store()
        store.refresh()
        self.assertNotIn(TOKEN, json.dumps([store.snapshot(), store._events], default=str))

    def test_titles_are_capped(self):
        root = self.dir / "agy"
        (root / "conversations").mkdir(parents=True)
        make_db(root / "conversations" / "c1.db", [])
        make_summaries(root, [{"id": "c1", "title": "T" * 5000}])
        parsed = graph.parse_antigravity_db(root / "conversations" / "c1.db", self.now,
                                            graph.antigravity_summaries(root)["c1"])
        self.assertEqual(len(parsed["nodes"]["antigravity:c1"]["title"]), graph.TITLE_MAX)
        dsh = self.dir / "dsh"
        write_session(dsh, "session-long", title="D" * 5000, now=self.now)
        self.assertEqual(len(graph.parse_dsh(dsh, self.now)["nodes"]["deepseek:session-long"]["title"]),
                         graph.TITLE_MAX)

    def test_dsh_keeps_result_text_only_for_calls_that_can_ship(self):
        graph._DSH_TAILS.clear()
        self.addCleanup(graph._DSH_TAILS.clear)
        data = zstd_compress(call(self.now - 30, "c1", "grep", pattern="x"), result(self.now - 29, "c1", "SECRET OUTPUT"),
                             call(self.now - 20, "c2", "pwsh", command="git push origin main"),
                             result(self.now - 18, "c2", PUSHED))
        if not data or graph.zstd_frames(data)[1] != len(data):
            self.skipTest("no zstd decoder (needs Python 3.14 or Node 22.15+)")
        path = self.dir / "session.v4.jsonl.zstd"
        path.write_bytes(data)
        results = {r[2]: r[4] for r in graph.dsh_records(path) if r[0] == "result"}
        self.assertEqual(results["c1"], "")
        self.assertEqual(results["c2"], PUSHED)
        self.assertNotIn("SECRET OUTPUT", repr(graph._DSH_TAILS))

    def test_url_credentials_are_removed_in_every_url_form(self):
        for url, want in ((f"https://bob:{TOKEN}@github.com/o/r.git", "https://github.com/o/r.git"),
                          (f"https://{TOKEN}@github.com/o/r.git", "https://github.com/o/r.git"),
                          (f"ssh://git:{TOKEN}@host/o/r", "ssh://host/o/r"),
                          (f"HTTPS://u:{TOKEN}@h/r", "HTTPS://h/r"),
                          (f"https://u:p@{TOKEN}@host/r", "https://host/r"),   # an unencoded @ in the password
                          (f"git push https://x:{TOKEN}@github.com/o/r main", "git push https://github.com/o/r main"),
                          ("git@github.com:o/r.git", "git@github.com:o/r.git"),
                          ("see https://example.com/a@b", "see https://example.com/a@b")):
            self.assertEqual(graph.no_userinfo(url), want)

    def test_url_credential_stripping_is_linear_on_long_dotted_tokens(self):
        for sep in ".-+":
            text = "echo " + ("a" + sep) * 200000 + "@"   # 400 KB; was quadratic (40 KB took ~4 s)
            start = time.perf_counter()
            self.assertEqual(graph.no_userinfo(text), text)
            self.assertLess(time.perf_counter() - start, 2.0, sep)
        start = time.perf_counter()
        graph.detail_of({"command": "echo " + "a." * 200000 + "@"})
        self.assertLess(time.perf_counter() - start, 2.0)

    def test_feed_ship_ref_loses_url_credentials(self):
        feeds = self.dir / "feeds"
        (feeds / "rt").mkdir(parents=True)
        (feeds / "rt" / "s1.jsonl").write_text(
            json.dumps({"v": 1, "t": self.now, "type": "ship", "kind": "pr",
                        "ref": f"https://bob:{TOKEN}@github.com/o/r/pull/1"}) + "\n" +
            json.dumps({"v": 1, "t": self.now, "type": "ship", "kind": "push", "ref": f"https://{TOKEN}@host/x"}) + "\n")
        self.only(FEED_DIR=feeds)
        store = bare_store()
        store.refresh()
        snap = store.snapshot()
        self.assertEqual(sorted((s["step"], s["branch"], s["url"]) for s in snap["ships"]),
                         [("pr", None, "https://github.com/o/r/pull/1"), ("push", "https://host/x", None)])
        self.assertNotIn(TOKEN, json.dumps(payload_of(snap, self.dir), default=str))


# ---- Round two

class AntigravityDeadlineCacheTest(TempDirTest):
    def slow_db(self, path):
        """gen_col.py: a real table whose generated column makes every read of the steps very slow."""
        con = sqlite3.connect(path)
        con.execute("create table steps(n integer, step_type integer, status integer, step_payload blob, "
                    "idx integer generated always as (length(hex(zeroblob(5000000 + n)))) virtual)")
        con.execute("create table gen_metadata(idx integer, data blob)")
        con.executemany("insert into steps(n, step_type, status, step_payload) values (?, 132, 3, x'')",
                        [(i,) for i in range(200)])   # ~2 s to read in full; the deadline is set to 0.3 s
        con.commit()
        con.close()

    def test_a_query_stopped_at_the_deadline_is_cached_until_the_file_changes(self):
        agy = self.dir / "agy"
        (agy / "conversations").mkdir(parents=True)
        self.slow_db(agy / "conversations" / "g0.db")
        for k in (1, 2):
            shutil.copyfile(agy / "conversations" / "g0.db", agy / "conversations" / f"g{k}.db")
        self.only(ANTIGRAVITY_DIR=agy)
        calls = []
        real = graph.parse_antigravity_db

        def counted(*args, **kwargs):
            if threading.current_thread() is threading.main_thread():   # not test_server's background GraphStore
                calls.append(args[0])
            return real(*args, **kwargs)
        with mock.patch.object(graph, "ANTIGRAVITY_QUERY_S", 0.3), mock.patch.object(graph, "parse_antigravity_db", counted):
            self.assertEqual(real(agy / "conversations" / "g0.db")["nodes"], {})   # empty bundle, not None
            store = bare_store()
            store.refresh()
            first = len(calls)
            start = time.monotonic()
            store.refresh()
            second = time.monotonic() - start
        self.assertEqual(first, 3)              # one per database (the direct call above is not counted)
        self.assertEqual(len(calls), first)     # the second refresh ran no query
        self.assertLess(second, 5)                # generous; the call count above is the real check
        self.assertEqual(sum(1 for slot in store._files if slot[0] == "antigravity"), 3)

    def test_only_the_newest_databases_are_read(self):
        conv = self.dir / "agy" / "conversations"
        conv.mkdir(parents=True)
        for k in range(5):
            make_db(conv / f"c{k}.db", [])
            os.utime(conv / f"c{k}.db", (self.now - 100 + k, self.now - 100 + k))
        with mock.patch.object(graph, "ANTIGRAVITY_DBS_MAX", 2):
            self.assertEqual(sorted(p.name for p in graph._antigravity_find(conv)), ["c3.db", "c4.db"])
        self.assertEqual(len(graph._antigravity_find(conv)), 5)


class RosterIsolationTest(TempDirTest):
    BAD = [("sessions holds a list", {"agents": [{"sessionId": "x", "sessions": [["a"]]}]}),
           ("agents is a number", {"agents": 5}),
           ("event time is text", {"agents": [], "events": [{"from": "a", "to": "b", "at": "soon"}]}),
           ("an event is not an object", {"events": [7]}),
           ("sessionId is a number", {"agents": [{"sessionId": 5}]})]

    def test_a_malformed_roster_is_skipped_and_the_refresh_continues(self):
        # roster_stall.py
        feeds, rosters = self.dir / "feeds", self.dir / "roster"
        (feeds / "okrt").mkdir(parents=True)
        rosters.mkdir()
        (feeds / "okrt" / "s1.jsonl").write_text(json.dumps({"v": 1, "t": self.now, "type": "tool", "name": "Grep"}) + "\n")
        (rosters / "good.json").write_text(json.dumps({"agents": [{"sessionId": "lead", "name": "Lead"}]}))
        self.only(FEED_DIR=feeds, ROSTER_DIR=rosters)
        for label, roster in self.BAD:
            (rosters / "bad.json").write_text(json.dumps(roster))
            store = bare_store()
            store.refresh()
            snap = store.snapshot()
            self.assertIsNotNone(snap["generatedAt"], label)
            self.assertIn("feed:okrt:s1", snap["nodes"], label)
            self.assertEqual(snap["nodes"]["lead"]["label"], "Lead", label)   # the good roster still applies
            self.assertNotIn("x", snap["nodes"], label)

    def test_apply_rosters_skips_a_roster_that_raises(self):
        nodes, edges = {}, {}
        graph.apply_rosters(nodes, edges, [{"agents": [{"sessionId": "x", "sessions": [["a"]]}]},
                                           {"agents": [{"sessionId": "ok", "name": "OK"}]}], self.now)
        self.assertEqual(nodes["ok"]["label"], "OK")


class PiParentPathTest(TempDirTest):
    def pi_file(self, parent):
        path = self.dir / "sessions" / "2026-11-06T12-00-00-000Z_child-1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        t = "2026-11-06T12:00:00Z"
        path.write_text(json.dumps({"type": "session", "id": "child-1", "cwd": str(self.dir), "timestamp": t,
                                    "parentSession": parent}) + "\n", encoding="utf-8")
        return path

    def test_the_parent_session_path_is_never_opened_or_stated(self):
        big = self.dir / "elsewhere" / "big_bigparent.jsonl"
        big.parent.mkdir()
        with big.open("wb") as f:     # a huge file with no newline: reading its "first line" would load it all
            f.write(b"x" * (8 * 1024 * 1024))
        touched = []
        real_open, real_stat, real_lstat = Path.open, os.stat, os.lstat

        def watch(real):
            def call(target, *args, **kwargs):
                text = str(target)
                if "host" in text or text.endswith("CON") or "big_bigparent" in text:
                    touched.append(text)
                return real(target, *args, **kwargs)
            return call
        cases = [("\\\\host\\share\\2026_hostparent.jsonl", "pi:hostparent"), ("CON", "pi:CON"),
                 (str(big), "pi:bigparent")]
        for parent, want in cases:
            child = self.pi_file(parent)
            with mock.patch.object(Path, "open", watch(real_open)), mock.patch("os.stat", watch(real_stat)), \
                    mock.patch("os.lstat", watch(real_lstat)):
                parsed = graph.parse_pi_file(child, self.now)
            self.assertEqual(touched, [], parent)
            node = parsed["nodes"]["pi:child-1"]
            self.assertEqual((node["kind"], node["parent"]), ("subagent", want))
            stub = parsed["nodes"][want]
            self.assertEqual((stub["open"], stub.get("mtime"), stub["_parentStub"]), (False, None, True))


class FeedFolderSwapTest(TempDirTest):
    def test_folder_swapped_while_opening_is_refused(self):
        root = self.dir / "feeds"
        w = feed.FeedWriter("rt", "s1", root=root)
        w.tool("Grep")
        folder = os.path.abspath(root / "rt")
        real_lstat = os.lstat
        seen = []

        def lstat(path, *args, **kwargs):   # the folder's second look (after opening) is another folder
            st = real_lstat(path, *args, **kwargs)
            if os.path.abspath(path) == folder:
                seen.append(path)
                if len(seen) == 3:   # _plain_folder, then _append before and after opening
                    return os.stat_result((st.st_mode, st.st_ino + 1, *tuple(st)[2:]))
            return st
        with mock.patch("os.lstat", lstat), self.assertRaises(ValueError):
            w.tool("Bash")


if __name__ == "__main__":
    unittest.main()
