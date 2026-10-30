"""Ship events end to end: Codex and Pi shell calls, the store's newest-first cap, the folder target,
and the /api/graph payload. Run with: python -m unittest test_ships -v"""
import importlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import graph
import metrics
from test_graph import line, result, use

PUSHED = "To github.com:acme/widget.git\n   1a2b3c4..5d6e7f8  main -> main\n"


def iso(at):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def bare_store(*paths):
    """A GraphStore without its refresh thread, holding these Claude transcripts."""
    store = graph.GraphStore.__new__(graph.GraphStore)
    store._files, store._lock, store._snapshot, store._events = {}, threading.Lock(), None, []
    for path in paths:
        store._files[path] = (None, graph.parse_file(path))
    return store


class ShipEvents(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.now = time.time()

    def write_rows(self, name, rows):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def test_store_keeps_ships_newest_first_capped(self):
        total = graph.SHIP_EVENTS_TOTAL + 5
        text = "".join(line(iso(self.now - 3000 + i * 10), use(f"p{i}", "Bash", {"command": "git push origin main"})) +
                       line(iso(self.now - 3000 + i * 10 + 1), result(f"p{i}", PUSHED)) for i in range(total))
        path = self.dir / "proj" / "pusher.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
        store = bare_store(path)
        store._publish(self.now)
        ships = store.snapshot()["ships"]
        self.assertEqual(len(ships), graph.SHIP_EVENTS_TOTAL)
        self.assertEqual(ships[0]["id"], f"pusher:p{total - 1}:push")          # newest first
        self.assertEqual([s["at"] for s in ships], sorted((s["at"] for s in ships), reverse=True))
        self.assertIs(store._ships, ships)

    def test_target_is_the_folder_group_of_files_under_the_cwd(self):
        repo = self.dir / "repo"
        rows = [
            {"timestamp": iso(self.now - 9), "cwd": str(repo), "message": {"content": [
                use("w", "Write", {"file_path": str(repo / "src" / "app.py")})]}},
            {"timestamp": iso(self.now - 5), "cwd": str(repo), "message": {"content": [
                use("p", "Bash", {"command": "git push origin main"})]}},
            {"timestamp": iso(self.now - 4), "message": {"content": [result("p", PUSHED)]}},
        ]
        store = bare_store(self.write_rows("proj/sess.jsonl", rows))
        store._publish(self.now)
        ship = store.snapshot()["ships"][0]
        self.assertEqual(ship["cwd"], str(repo))
        self.assertEqual(ship["folder"], "doc:" + graph._path_key(repo).replace("\\", "/"))
        self.assertEqual(ship["target"], graph.document_target(repo / "src" / "app.py")["group"])

    def test_codex_shell_call_is_read(self):
        rows = [
            {"timestamp": iso(self.now - 9), "type": "session_meta", "payload": {"id": "cx1", "cwd": str(self.dir)}},
            {"timestamp": iso(self.now - 8), "type": "response_item", "payload": {
                "type": "function_call", "name": "shell", "call_id": "c1",
                "arguments": json.dumps({"command": ["bash", "-lc", "git push origin main"]})}},
            {"timestamp": iso(self.now - 7), "type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "c1",
                "output": json.dumps({"output": PUSHED, "metadata": {"exit_code": 0}})}},
        ]
        ships = graph.parse_codex_file(self.write_rows("rollout-cx1.jsonl", rows), self.now)["ships"]
        self.assertEqual([(s["id"], s["step"], s["ok"], s["branch"]) for s in ships],
                         [("codex:cx1:c1:push", "push", True, "main")])

    def test_pi_bash_call_is_read(self):
        rows = [
            {"type": "session", "id": "pi1", "cwd": str(self.dir)},
            {"type": "message", "timestamp": iso(self.now - 8), "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "b1", "name": "bash", "arguments": {"command": "git commit -m 'ship it'"}}]}},
            {"type": "message", "timestamp": iso(self.now - 7), "message": {"role": "toolResult", "toolCallId": "b1",
                "content": [{"type": "text", "text": "[main 1a2b3c4] ship it\n 1 file changed\n"}]}},
        ]
        ships = graph.parse_pi_file(self.write_rows("pi1.jsonl", rows), self.now)["ships"]
        self.assertEqual([(s["id"], s["step"], s["ok"], s["branch"]) for s in ships],
                         [("pi:pi1:b1:commit", "commit", True, "main")])

    def test_graph_payload_carries_ships(self):
        # Importing server starts its refresh threads; keep them from reading this machine's sessions.
        with mock.patch.object(graph.GraphStore, "_loop", lambda self: None), \
                mock.patch.object(metrics.Metrics, "_loop", lambda self: None):
            server = importlib.import_module("server")
        ships = [{"id": "a:t2:push", "at": 2.0}, {"id": "a:t1:push", "at": 1.0}]
        store = mock.Mock()
        store.snapshot.return_value = {"nodes": {}, "edges": [], "generatedAt": 3.0, "ships": ships}
        with mock.patch.object(server, "GRAPH", store), mock.patch.object(server, "JOBS_DIR", self.dir / "none"), \
                mock.patch.object(server, "open_session_ids", return_value=set()):
            payload = server.graph_payload()
        self.assertEqual(payload["ships"], ships)


if __name__ == "__main__":
    unittest.main()
