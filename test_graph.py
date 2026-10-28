"""Run with: python -m unittest test_graph -v"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import graph


def line(at, *parts):
    return json.dumps({"timestamp": at, "message": {"content": list(parts)}}) + "\n"


def use(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def result(tid):
    return {"type": "tool_result", "tool_use_id": tid, "content": "ok"}


class ParseFile(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.now = time.time()

    def iso(self, offset):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now + offset))

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_main_session_edges(self):
        text = (
            line(self.iso(-30), use("t1", "WebSearch", {"query": "x"})) +
            line(self.iso(-29), result("t1")) +
            line(self.iso(-20), use("t2", "Write", {"file_path": "C:/work/plan.md"})) +
            line(self.iso(-19), result("t2")) +
            line(self.iso(-5), use("t3", "mcp__example__get_my_profile", {})) +
            line(self.iso(-4), use("t4", "SendMessage", {"to": "researcher"}))  # t4 never answered
        )
        path = self.write(self.dir / "proj" / "sess-1.jsonl", text)
        parsed = graph.parse_file(path)
        self.assertEqual(parsed["nodes"]["sess-1"]["kind"], "agent")
        self.assertEqual(parsed["nodes"]["web:WebSearch"]["kind"], "web")
        self.assertEqual(parsed["nodes"]["doc:c:/work/plan.md"]["label"], "plan.md")
        self.assertEqual(parsed["nodes"]["doc:c:/work/plan.md"]["group"], "doc:c:/work")
        self.assertEqual(parsed["nodes"]["mcp:example:get_my_profile"]["kind"], "tool")
        self.assertEqual(parsed["nodes"]["mcp:example:get_my_profile"]["group"], "mcp:example")
        self.assertEqual(parsed["nodes"]["msg:researcher"]["kind"], "msg")
        self.assertEqual(parsed["edges"][("sess-1", "web:WebSearch", "call")]["count"], 1)
        self.assertEqual(parsed["edges"][("sess-1", "msg:researcher", "message")]["count"], 1)
        # t1 and t2 were answered, so only t3 and t4 are open
        self.assertEqual(sorted(parsed["open"]), ["t3", "t4"])

    def test_subagent_spawn_edge(self):
        proj = self.dir / "proj"
        sub = self.write(proj / "sess-1" / "subagents" / "agent-abc.jsonl",
                         line(self.iso(-10), use("s1", "WebFetch", {"url": "https://example.com"})))
        parsed = graph.parse_file(sub)
        self.assertEqual(parsed["nodes"]["sub:agent-abc"]["kind"], "subagent")
        self.assertEqual(parsed["edges"][("sub:agent-abc", "web:WebFetch", "call")]["count"], 1)
        # The spawn edge is added on the parent side when the store merges files
        self.assertEqual(parsed["edges"][("sess-1", "sub:agent-abc", "spawn")]["count"], 1)

    def test_subagent_busy_until_end_turn(self):
        proj = self.dir / "proj"
        text = line(self.iso(-300), use("s1", "Bash", {"command": "cargo test"}))
        sub = self.write(proj / "sess-1" / "subagents" / "agent-abc.jsonl", text)
        self.assertTrue(graph.parse_file(sub)["nodes"]["sub:agent-abc"]["busy"])
        done = json.dumps({"timestamp": self.iso(-1), "message": {
            "stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}}) + "\n"
        self.write(sub, text + line(self.iso(-2), result("s1")) + done)
        self.assertFalse(graph.parse_file(sub)["nodes"]["sub:agent-abc"]["busy"])

    def test_pi_parallel_subagents_each_get_a_pi_child_node(self):
        path = self.dir / "pi-session.jsonl"
        header = {"type": "session", "id": "pi-parent", "cwd": str(self.dir)}
        call = {"type": "message", "timestamp": self.iso(-10), "message": {
            "role": "assistant", "stopReason": "toolUse", "content": [{
                "type": "toolCall", "id": "call-parallel", "name": "subagent", "arguments": {
                    "tasks": [
                        {"agent": "scout", "task": "Find the API entry points"},
                        {"agent": "planner", "task": "Map the workflow"},
                        {"agent": "reviewer", "task": "Check the changes"},
                    ]
                }
            }]
        }}
        path.write_text("\n".join(json.dumps(row) for row in (header, call)) + "\n", encoding="utf-8")

        active = graph.parse_pi_file(path, self.now)
        children = [node for node in active["nodes"].values() if node.get("kind") == "subagent"]
        self.assertEqual(len(children), 3)
        self.assertTrue(all(node["open"] and node["state"] == "working" for node in children))
        self.assertEqual({node["name"].split(" · ", 1)[0] for node in children}, {"scout", "planner", "reviewer"})
        self.assertTrue(all(node["provider"] == node["domain"] == node["space"] == "pi" for node in children))
        self.assertTrue(all(node["parent"] == "pi:pi-parent" for node in children))
        self.assertEqual(sum(1 for src, dst, kind in active["edges"] if src == "pi:pi-parent" and kind == "spawn"), 3)

        result = {"type": "message", "timestamp": self.iso(-1), "message": {
            "role": "toolResult", "toolCallId": "call-parallel", "durationMs": 9000
        }}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(result) + "\n")
        finished = graph.parse_pi_file(path, self.now)
        children = [node for node in finished["nodes"].values() if node.get("kind") == "subagent"]
        self.assertEqual(len(children), 3)
        self.assertTrue(all(node["state"] == "done" and not node["open"] for node in children))

    def test_pi_agent_named_dispatch_with_task_list_is_detected(self):
        path = self.dir / "pi-agent-dispatch.jsonl"
        header = {"type": "session", "id": "pi-agent-parent", "cwd": str(self.dir)}
        call = {"type": "message", "timestamp": self.iso(-5), "message": {
            "role": "assistant", "content": [{
                "type": "toolCall", "id": "agent-call", "name": "mcp__agents__run", "arguments": {
                    "tasks": [{"agent": "researcher", "task": "Find the implementation"}]
                }
            }]
        }}
        path.write_text("\n".join(json.dumps(row) for row in (header, call)) + "\n", encoding="utf-8")

        parsed = graph.parse_pi_file(path, self.now)
        children = [(node_id, node) for node_id, node in parsed["nodes"].items()
                    if node.get("kind") == "subagent"]
        self.assertEqual(len(children), 1)
        child_id, child = children[0]
        self.assertTrue(child["name"].startswith("researcher"))
        self.assertEqual(parsed["edges"][("pi:pi-agent-parent", child_id, "spawn")]["count"], 1)

    def test_call_events_carry_duration_and_detail(self):
        text = (
            line(self.iso(-30), use("t1", "Bash", {"command": "ls -la\nsecond line"})) +
            line(self.iso(-28), result("t1")) +           # answered 2 s later
            line(self.iso(-10), use("t2", "WebSearch", {"query": "plane view"}))  # still open
        )
        parsed = graph.parse_file(self.write(self.dir / "proj" / "sess-3.jsonl", text))
        first, second = parsed["events"]
        self.assertEqual(first["detail"], "ls -la")          # first line only
        self.assertEqual(first["ms"], 2000)
        self.assertIsNone(second["ms"])                      # never answered
        self.assertEqual(second["dst"], "web:WebSearch")
        self.assertIn("shared:tool:shell", first["targets"])
        self.assertIn("shared:tool:web", second["targets"])

    def test_flow_returns_only_that_edge_oldest_first(self):
        text = (
            line(self.iso(-30), use("a", "Bash", {"command": "one"})) +
            line(self.iso(-20), use("b", "WebFetch", {"url": "https://x"})) +
            line(self.iso(-10), use("c", "Bash", {"command": "two"}))
        )
        path = self.write(self.dir / "proj" / "sess-4.jsonl", text)
        store = graph.GraphStore.__new__(graph.GraphStore)
        store._files, store._lock = {path: (None, graph.parse_file(path))}, __import__("threading").Lock()
        store._snapshot, store._events = None, []
        store._publish(self.now)
        flow = store.flow("sess-4", "tool:Bash", "call")
        self.assertEqual([c["detail"] for c in flow], ["one", "two"])
        shared_flow = store.flow("sess-4", "shared:tool:shell", "call")
        self.assertEqual([c["detail"] for c in shared_flow], ["one", "two"])
        self.assertEqual(store.flow("sess-4", "web:WebFetch", "call")[0]["tool"], "WebFetch")

    def test_agent_events_cover_every_target_oldest_first(self):
        text = (
            line(self.iso(-30), use("a", "Bash", {"command": "one"})) +
            line(self.iso(-20), use("b", "WebFetch", {"url": "https://x"})) +
            line(self.iso(-10), use("c", "Bash", {"command": "two"}))
        )
        path = self.write(self.dir / "proj" / "sess-5.jsonl", text)
        store = graph.GraphStore.__new__(graph.GraphStore)
        store._files, store._lock = {path: (None, graph.parse_file(path))}, __import__("threading").Lock()
        store._snapshot, store._events = None, []
        store._publish(self.now)
        events = store.agent_events("sess-5")
        self.assertEqual([c["dst"] for c in events], ["tool:Bash", "web:WebFetch", "tool:Bash"])
        self.assertIn("shared:tool:shell", events[0]["targets"])
        self.assertEqual(store.agent_events("nobody"), [])

    def test_store_marks_active_and_pulse(self):
        proj = self.dir / "proj"
        self.write(proj / "sess-2.jsonl", line(self.iso(-3), use("t9", "Bash", {"command": "ls"})))
        store = graph.GraphStore.__new__(graph.GraphStore)  # no background thread in tests
        store._files, store._lock = {}, __import__("threading").Lock()
        store._snapshot = None
        path = proj / "sess-2.jsonl"
        store._files[path] = (None, graph.parse_file(path))
        store._publish(self.now)
        snap = store.snapshot()
        edge = snap["edges"][0]
        self.assertEqual((edge["src"], edge["dst"]), ("sess-2", "tool:Bash"))
        self.assertTrue(edge["active"])   # unanswered and recent
        self.assertTrue(edge["pulse"])    # within PULSE_S

    def store_with(self, *paths):
        store = graph.GraphStore.__new__(graph.GraphStore)
        store._files, store._lock, store._snapshot = {}, __import__("threading").Lock(), None
        for path in paths:
            store._files[path] = (None, graph.parse_file(path))
        return store

    def test_tool_groups(self):
        self.assertEqual(graph.target_of("Glob", {})["group"], "tool:files")
        self.assertEqual(graph.target_of("PowerShell", {})["group"], "tool:shell")
        self.assertEqual(graph.target_of("mcp__claude_ai_Gmail__send_message", {})["groupLabel"], "Gmail")
        self.assertEqual(graph.target_of("Write", {"file_path": r"C:\x\drafts\a.md"})["id"], "doc:c:/x/drafts/a.md")

    def test_file_paths_used_by_tools_are_linked(self):
        proj = self.dir / "proj"
        a = self.write(proj / "writer.jsonl", line(self.iso(-9), use("w", "Write", {"file_path": "C:/n/story.md"})))
        b = self.write(proj / "reader.jsonl",
                       line(self.iso(-5), use("r1", "Read", {"file_path": "C:/n/story.md"})) +
                       line(self.iso(-4), use("r2", "Read", {"file_path": "C:/n/unwritten.md"})))
        store = self.store_with(a, b)
        store._publish(self.now)
        pairs = {(e["src"], e["dst"]) for e in store.snapshot()["edges"]}
        self.assertIn(("reader", "doc:c:/n/story.md"), pairs)
        self.assertIn(("reader", "doc:c:/n/unwritten.md"), pairs)
        self.assertIn("doc:c:/n/unwritten.md", store.snapshot()["nodes"])
        self.assertIn(("reader", "tool:Read"), pairs)

    def test_roster_names_and_reports(self):
        proj = self.dir / "proj"
        a = self.write(proj / "s-boss.jsonl", line(self.iso(-9), use("w", "Write", {"file_path": "C:/n/front.md"})))
        store = self.store_with(a)
        roster = {"agents": [
            {"sessionId": "s-boss", "name": "Margaret Hale", "role": "Editor-in-Chief", "model": "opus", "tier": 0},
            {"sessionId": "s-rep", "name": "Leo Park", "role": "Reporter", "model": "haiku", "tier": 2,
             "reportsTo": "s-boss"}],
            "events": [{"from": "s-rep", "to": "s-boss", "kind": "files", "at": self.now - 1},
                       {"from": "s-rep", "to": "s-peer", "kind": "collab", "at": self.now - 1}]}
        store._publish(self.now, [roster])
        snap = store.snapshot()
        self.assertEqual(snap["nodes"]["s-boss"]["label"], "Margaret Hale")
        self.assertEqual(snap["nodes"]["s-rep"]["tier"], 2)   # roster agent shown before its transcript exists
        rep = [e for e in snap["edges"] if e["kind"] == "reports"]
        self.assertEqual((rep[0]["src"], rep[0]["dst"]), ("s-boss", "s-rep"))
        self.assertTrue(rep[0]["pulse"])
        # an event to an agent that is not on the plane draws nothing
        self.assertFalse([e for e in snap["edges"] if e["dst"] == "s-peer"])

    def test_load_rosters_skips_bad_files(self):
        d = self.dir / "roster"
        self.write(d / "ok.json", json.dumps({"agents": []}))
        self.write(d / "bad.json", "{not json")
        self.assertEqual(len(graph.load_rosters(time.time(), d)), 1)

    def test_shared_dir_is_one_area_node_and_off_by_default(self):
        shared = Path(tempfile.mkdtemp()) / "inbox"
        with mock.patch.object(graph, "SHARED_DIR", None):
            self.assertEqual(graph.document_target(shared / "a.md")["kind"], "doc")
        with mock.patch.object(graph, "SHARED_DIR", shared):
            node = graph.document_target(shared / "notes" / "a.md")
            self.assertEqual((node["id"], node["label"], node["detail"]), ("area:shared", "inbox", "inbox/notes/a.md"))
            self.assertEqual(graph.shared_target_from_input({"command": f"cat {shared}/x.md"})["id"], "area:shared")


if __name__ == "__main__":
    unittest.main()
