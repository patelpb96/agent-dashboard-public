"""Jev calls end to end: exact-name detection in Claude, Pi and Codex transcripts, the lifecycle (pending, done,
missing), the bounded outcome read from a Jev result, and the /api/graph `jevs` list.
Run with: python -m unittest test_jev -v"""
import hashlib
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

JEV = "mcp__jev__jev_decide"
CANARY = "SECRET"


def iso(at):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at)) if at else None


def answer(**over):
    """A Jev response with a canary in every field that must never be kept."""
    body = {
        "ok": True, "failOpen": False, "latencyMs": 412.4, "policy": "generic", "model": CANARY + "-model",
        "id": CANARY + "-id", "requestId": CANARY + "-req", "redactions": [CANARY + "-redaction"],
        "request": {"state": CANARY + "-state", "criteria": CANARY, "facts": [CANARY], "questions": [CANARY]},
        "answers": {CANARY + "-label": CANARY + "-answer"}, "probabilities": {CANARY + "-option": 0.9},
        "choice": CANARY + "-choice", "value": CANARY + "-value", "risks": [{"kind": CANARY, "p": 0.4}],
        "message": CANARY + "-message",
        "decision": {
            "applied": True, "reason": "all_applied", "choice": CANARY + "-choice", "value": CANARY + "-value",
            "message": CANARY,
            "audit": [
                {"kind": "choice", "applied": True, "confidence": 0.8234, "threshold": 0.7,
                 "choice": CANARY + "-choice", "probabilities": {CANARY: 0.8}, "question": CANARY},
                {"kind": "noul", "applied": False, "probability": 0.5551, "thresholds": {"yes": 0.8, "no": 0.2},
                 "value": CANARY + "-noul", "question": CANARY},
            ],
        },
    }
    body.update(over)
    return body


class Writer(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.now = time.time()

    def rows(self, name, rows):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    # ---- Claude Code
    def claude_use(self, at, tid, name=JEV, inp=None):
        return {"type": "assistant", "timestamp": iso(at), "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": name, "input": inp or {"state": CANARY + "-input"}}]}}

    def claude_result(self, at, tid, body):
        text = body if isinstance(body, str) else json.dumps(body)
        return {"type": "user", "timestamp": iso(at), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text", "text": text}]}]}}

    def claude_turn(self, at, text="next please"):
        return {"type": "user", "timestamp": iso(at), "message": {"role": "user", "content": text}}

    def claude(self, rows, name="proj/sess.jsonl"):
        return graph.parse_file(self.rows(name, rows))

    # ---- Pi
    def pi(self, rows, name="pi1.jsonl"):
        return graph.parse_pi_file(self.rows(name, [{"type": "session", "id": "pi1", "cwd": str(self.dir)}] + rows), self.now)

    def pi_call(self, at, cid, name=JEV):
        return {"type": "message", "timestamp": iso(at), "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": cid, "name": name, "arguments": {"state": CANARY}}]}}

    def pi_result(self, at, cid, body, tool=JEV):
        message = {"role": "toolResult", "toolCallId": cid,
                   "content": [{"type": "text", "text": body if isinstance(body, str) else json.dumps(body)}]}
        if tool is not None:
            message["toolName"] = tool
        return {"type": "message", "timestamp": iso(at), "message": message}

    def pi_turn(self, at):
        return {"type": "message", "timestamp": iso(at), "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}}

    # ---- Codex
    def codex(self, rows, name="rollout-cx1.jsonl"):
        meta = {"timestamp": iso(self.now - 100), "type": "session_meta", "payload": {"id": "cx1", "cwd": str(self.dir)}}
        return graph.parse_codex_file(self.rows(name, [meta] + rows), self.now)

    def codex_call(self, at, cid, namespace="mcp__jev", name="jev_decide"):
        payload = {"type": "function_call", "name": name, "call_id": cid, "arguments": json.dumps({"state": CANARY})}
        if namespace is not None:
            payload["namespace"] = namespace
        return {"timestamp": iso(at), "type": "response_item", "payload": payload}

    def codex_output(self, at, cid, body, wrap=True):
        text = body if isinstance(body, str) else json.dumps(body)
        output = json.dumps({"content": [{"type": "text", "text": text}], "isError": False}) if wrap else text
        return {"timestamp": iso(at), "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": cid, "output": output}}

    def codex_turn(self, at):
        return {"timestamp": iso(at), "type": "event_msg", "payload": {"type": "task_started", "started_at": iso(at)}}


class Detection(Writer):
    def test_claude_call_and_answer(self):
        jevs = self.claude([self.claude_use(self.now - 10, "toolu_A"),
                            self.claude_result(self.now - 9, "toolu_A", answer())])["jevs"]
        self.assertEqual(len(jevs), 1)
        call = jevs[0]
        self.assertEqual(call["agent"], "sess")
        self.assertEqual(call["phase"], "done")
        self.assertEqual(call["id"], hashlib.sha1("sess\0toolu_A".encode()).hexdigest()[:12])
        self.assertRegex(call["id"], r"^[0-9a-f]{12}$")
        self.assertAlmostEqual(call["at"], round(self.now - 10), delta=1)
        self.assertGreater(call["end"], call["at"])
        self.assertEqual(call["outcome"], {
            "ok": True, "fail_open": False, "latency_ms": 412, "policy": "generic", "applied": True,
            "reason": "all_applied", "error": None,
            "audit": [
                {"applied": True, "measure": "confidence", "value_pm": 823, "threshold_pm": 700, "yes_pm": None, "no_pm": None},
                {"applied": False, "measure": "yes_no", "value_pm": None, "threshold_pm": None, "yes_pm": 800, "no_pm": 200},
            ]})

    def test_exact_name_only(self):
        names = ["jev_decide", "mcp__jev__jev_decide_v2", "mcp__jevx__jev_decide", "mcp__other__jev_decide",
                 "MCP__JEV__JEV_DECIDE", "mcp__jev__jev_decide ", "mcp__jev__decide", "jev"]
        rows = []
        for i, name in enumerate(names):
            rows += [self.claude_use(self.now - 50 + i, f"t{i}", name), self.claude_result(self.now - 49 + i, f"t{i}", answer())]
        self.assertEqual(self.claude(rows)["jevs"], [])
        pi_rows = []
        for i, name in enumerate(names):
            pi_rows += [self.pi_call(self.now - 50 + i, f"p{i}", name), self.pi_result(self.now - 49 + i, f"p{i}", answer(), name)]
        self.assertEqual(self.pi(pi_rows)["jevs"], [])
        cx = [self.codex_call(self.now - 50, "c1", "mcp__jevx"), self.codex_call(self.now - 49, "c2", "jev"),
              self.codex_call(self.now - 48, "c3", "mcp__jev", "jev_decide_v2"), self.codex_call(self.now - 47, "c4", None)]
        self.assertEqual(self.codex(cx)["jevs"], [])

    def test_codex_namespace_join(self):
        self.assertEqual(graph.codex_tool_name({"namespace": "mcp__jev", "name": "jev_decide"}), JEV)
        self.assertEqual(graph.codex_tool_name({"namespace": "mcp__exa", "name": "web_search"}), "mcp__exa__web_search")
        # never prefixed twice, other namespaces left alone, a bare "mcp__" is not a server
        self.assertEqual(graph.codex_tool_name({"namespace": "mcp__jev", "name": JEV}), JEV)
        self.assertEqual(graph.codex_tool_name({"namespace": "collaboration", "name": "wait_agent"}), "wait_agent")
        self.assertEqual(graph.codex_tool_name({"namespace": "mcp__", "name": "x"}), "x")
        self.assertEqual(graph.codex_tool_name({"name": "shell"}), "shell")
        parsed = self.codex([self.codex_call(self.now - 10, "c1"), self.codex_output(self.now - 9, "c1", answer())])
        self.assertIn("mcp:jev:jev_decide", parsed["nodes"])
        self.assertEqual(parsed["nodes"]["mcp:jev:jev_decide"]["group"], "mcp:jev")
        [call] = parsed["jevs"]
        self.assertEqual((call["agent"], call["phase"], call["outcome"]["policy"]), ("codex:cx1", "done", "generic"))
        # the output string may also be Jev's JSON itself, not wrapped
        parsed = self.codex([self.codex_call(self.now - 10, "c1"), self.codex_output(self.now - 9, "c1", answer(), wrap=False)])
        self.assertEqual(parsed["jevs"][0]["outcome"]["reason"], "all_applied")

    def test_codex_double_prefix_and_other_namespaces(self):
        parsed = self.codex([self.codex_call(self.now - 10, "c1", "mcp__jev", JEV),
                             self.codex_call(self.now - 9, "c2", "collaboration", "wait_agent")])
        self.assertIn("mcp:jev:jev_decide", parsed["nodes"])
        self.assertIn("tool:wait_agent", parsed["nodes"])
        self.assertEqual([c["phase"] for c in parsed["jevs"]], ["pending"])

    def test_pi_call_and_answer(self):
        jevs = self.pi([self.pi_call(self.now - 10, "p1"), self.pi_result(self.now - 9, "p1", answer())])["jevs"]
        self.assertEqual([(c["agent"], c["phase"]) for c in jevs], [("pi:pi1", "done")])
        self.assertEqual(jevs[0]["outcome"]["latency_ms"], 412)

    def test_pi_result_naming_another_tool_is_never_read(self):
        jevs = self.pi([self.pi_call(self.now - 10, "p1"), self.pi_result(self.now - 9, "p1", answer(), "bash")])["jevs"]
        self.assertEqual([(c["phase"], c["outcome"]) for c in jevs], [("pending", None)])

    def test_pi_orphan_result_is_done_without_a_start(self):
        jevs = self.pi([self.pi_result(self.now - 9, "p9", answer())])["jevs"]
        self.assertEqual([(c["phase"], c["at"], c["outcome"]) for c in jevs], [("done", None, None)])
        self.assertIsNotNone(jevs[0]["end"])


class Lifecycle(Writer):
    def test_pending_then_missing_on_a_new_turn(self):
        jevs = self.claude([self.claude_use(self.now - 10, "t1")])["jevs"]
        self.assertEqual([(c["phase"], c["end"]) for c in jevs], [("pending", None)])
        jevs = self.claude([self.claude_use(self.now - 10, "t1"), self.claude_turn(self.now - 5)])["jevs"]
        self.assertEqual(jevs[0]["phase"], "missing")
        self.assertAlmostEqual(jevs[0]["end"], round(self.now - 5), delta=1)
        jevs = self.pi([self.pi_call(self.now - 10, "p1"), self.pi_turn(self.now - 5)])["jevs"]
        self.assertEqual(jevs[0]["phase"], "missing")
        jevs = self.codex([self.codex_turn(self.now - 20), self.codex_call(self.now - 10, "c1"), self.codex_turn(self.now - 5)])["jevs"]
        self.assertEqual(jevs[0]["phase"], "missing")

    def test_meta_lines_and_tool_results_are_not_a_new_turn(self):
        meta = {"type": "user", "isMeta": True, "timestamp": iso(self.now - 8), "message": {"role": "user", "content": "caveat"}}
        other = self.claude_result(self.now - 7, "zzz", "plain")
        jevs = self.claude([self.claude_use(self.now - 10, "t1"), meta, other])["jevs"]
        self.assertEqual(jevs[0]["phase"], "pending")

    def test_late_result_after_missing_is_ignored(self):
        rows = [self.claude_use(self.now - 10, "t1"), self.claude_turn(self.now - 5),
                self.claude_result(self.now - 4, "t1", answer()), self.claude_result(self.now - 3, "t1", answer())]
        jevs = self.claude(rows)["jevs"]
        self.assertEqual([(c["phase"], c["outcome"]) for c in jevs], [("missing", None)])
        jevs = self.pi([self.pi_call(self.now - 10, "p1"), self.pi_turn(self.now - 5), self.pi_result(self.now - 4, "p1", answer())])["jevs"]
        self.assertEqual([(c["phase"], c["outcome"]) for c in jevs], [("missing", None)])

    def test_repeated_result_never_makes_a_second_call(self):
        rows = [self.pi_call(self.now - 10, "p1"), self.pi_result(self.now - 9, "p1", answer()),
                self.pi_result(self.now - 8, "p1", answer(policy="none"))]
        jevs = self.pi(rows)["jevs"]
        self.assertEqual(len(jevs), 1)
        self.assertEqual(jevs[0]["outcome"]["policy"], "generic")
        rows = [self.claude_use(self.now - 10, "t1"), self.claude_result(self.now - 9, "t1", answer()),
                self.claude_result(self.now - 8, "t1", answer(policy="none"))]
        jevs = self.claude(rows)["jevs"]
        self.assertEqual([(c["phase"], c["outcome"]["policy"]) for c in jevs], [("done", "generic")])

    def test_reused_id_after_a_new_turn_is_not_read(self):
        # t1 was a Jev call, the turn moved on, and the provider later reused t1 for another tool: its result is
        # neither read nor taken as Jev's.
        rows = [self.claude_use(self.now - 10, "t1"), self.claude_turn(self.now - 8),
                self.claude_use(self.now - 6, "t1", "Bash", {"command": "ls"}), self.claude_result(self.now - 5, "t1", answer())]
        jevs = self.claude(rows)["jevs"]
        self.assertEqual([(c["phase"], c["outcome"]) for c in jevs], [("missing", None)])

    def test_reused_jev_id_gets_its_own_stable_id(self):
        rows = [self.claude_use(self.now - 10, "t1"), self.claude_result(self.now - 9, "t1", answer()),
                self.claude_turn(self.now - 8),
                self.claude_use(self.now - 6, "t1"), self.claude_result(self.now - 5, "t1", answer(policy="none"))]
        jevs = self.claude(rows)["jevs"]
        self.assertEqual([c["phase"] for c in jevs], ["done", "done"])
        self.assertNotEqual(jevs[0]["id"], jevs[1]["id"])
        self.assertEqual(jevs, self.claude(rows, "proj/sess.jsonl")["jevs"])   # same file, same ids

    def test_quiet_session_reports_pending_as_missing(self):
        parsed = self.claude([self.claude_use(self.now - 10, "t1")])
        [row] = graph.jev_rows([parsed], parsed["activity"] + 5)
        self.assertEqual(row["phase"], "pending")
        [row] = graph.jev_rows([parsed], parsed["activity"] + graph.ACTIVE_S + 1)
        self.assertEqual((row["phase"], row["end"]), ("missing", None))
        self.assertEqual(parsed["jevs"][0]["phase"], "pending")   # the cached parse is untouched
        pi = self.pi([self.pi_call(self.now - 10, "p1")])
        self.assertEqual(graph.jev_rows([pi], pi["activity"] + graph.PI_ACTIVE_S + 1)[0]["phase"], "missing")

    def test_bounds(self):
        rows = []
        for i in range(12):
            rows += [self.claude_use(self.now - 100 + i, f"t{i}"), self.claude_result(self.now - 99 + i, f"t{i}", answer())]
        parsed = [self.claude(rows, f"proj/s{n}.jsonl") for n in range(6)]
        out = graph.jev_rows(parsed, self.now)
        self.assertEqual(len(out), graph.JEV_TOTAL)
        per_agent = {}
        for row in out:
            per_agent[row["agent"]] = per_agent.get(row["agent"], 0) + 1
        self.assertTrue(all(n <= graph.JEV_PER_AGENT for n in per_agent.values()))
        self.assertEqual([r["at"] for r in out], sorted((r["at"] for r in out), reverse=True))   # newest first
        many = []
        for i in range(graph.JEV_PER_TRANSCRIPT + 10):
            many += [self.claude_use(self.now - 500 + i, f"m{i}")]
        self.assertEqual(len(self.claude(many)["jevs"]), graph.JEV_PER_TRANSCRIPT)


class Outcome(unittest.TestCase):
    def read(self, body):
        return graph.jev_outcome(json.dumps(body))

    def test_codes_go_through_allow_lists(self):
        o = self.read(answer(policy="madeUp" + CANARY, decision={"applied": False, "reason": CANARY}))
        self.assertEqual((o["policy"], o["reason"]), ("other", "other"))
        o = self.read(answer(ok=False, failOpen=True, error={"reason": "timeout", "message": CANARY + "-err"}))
        self.assertEqual((o["ok"], o["fail_open"], o["error"]), (False, True, "timeout"))
        o = self.read(answer(ok=False, error={"reason": CANARY, "message": CANARY}))
        self.assertEqual(o["error"], "other")
        self.assertNotIn(CANARY, json.dumps(o))

    def test_numbers_out_of_range_are_dropped(self):
        o = self.read(answer(latencyMs=3_600_001, decision={"applied": True, "reason": "all_applied", "audit": [
            {"kind": "choice", "applied": True, "confidence": 1.2, "threshold": -0.1},
            {"kind": "choice", "applied": True, "confidence": True, "threshold": "0.5"},
            {"kind": "noul", "applied": True, "thresholds": {"yes": 2, "no": 0.0005}}]}))
        self.assertIsNone(o["latency_ms"])
        self.assertEqual([(r["value_pm"], r["threshold_pm"]) for r in o["audit"][:2]], [(None, None), (None, None)])
        self.assertEqual((o["audit"][2]["yes_pm"], o["audit"][2]["no_pm"]), (None, 1))
        self.assertIsNone(self.read(answer(latencyMs=-1))["latency_ms"])
        self.assertEqual(self.read(answer(latencyMs=0.5))["latency_ms"], 1)
        self.assertIsNone(graph.jev_outcome('{"ok": true, "latencyMs": NaN}')["latency_ms"])
        self.assertIsNone(self.read(answer(ok="yes"))["ok"])

    def test_rows_per_policy(self):
        item = {"applied": True, "confidence": 0.9, "threshold": 0.5, "kind": "noul", "thresholds": {"yes": 0.7, "no": 0.3}}
        ck = self.read(answer(policy="contextKeep", decision={"applied": True, "reason": "dropped_unneeded_items",
                                                              "audit": [item, item]}))
        self.assertEqual(ck["audit"], [{"applied": None, "measure": "confidence", "value_pm": 900, "threshold_pm": 500,
                                        "yes_pm": None, "no_pm": None}] * 2)
        for policy in ("routeModel", "shouldContinue"):
            o = self.read(answer(policy=policy, decision={"applied": False, "reason": "low_confidence",
                                                          "audit": {"confidence": 0.41, "threshold": 0.6, "choice": CANARY}}))
            self.assertEqual(o["audit"], [{"applied": False, "measure": "confidence", "value_pm": 410, "threshold_pm": 600,
                                           "yes_pm": None, "no_pm": None}])
            self.assertEqual(self.read(answer(policy=policy, decision={"applied": True, "audit": [item]}))["audit"], [])
        sg = self.read(answer(policy="safetyGate", decision={"applied": True, "reason": "destructive",
                                                             "audit": {"confidence": 0.9, "threshold": 0.5, "risks": [CANARY]}}))
        self.assertEqual((sg["audit"], sg["reason"]), ([], "destructive"))
        none = self.read(answer(policy="none", decision={"audit": [item]}))
        self.assertEqual((none["audit"], none["applied"]), ([], None))
        many = self.read(answer(decision={"applied": True, "audit": [item] * 20}))
        self.assertEqual(len(many["audit"]), graph.JEV_AUDIT_ROWS)
        # generic noul: thresholds only, never the probability
        self.assertEqual(many["audit"][0], {"applied": True, "measure": "yes_no", "value_pm": None, "threshold_pm": None,
                                            "yes_pm": 700, "no_pm": 300})

    def test_shapes_and_bounds_of_the_text(self):
        body = json.dumps(answer())
        self.assertEqual(graph.jev_outcome(body)["policy"], "generic")
        self.assertEqual(graph.jev_outcome([{"type": "text", "text": body}])["policy"], "generic")
        wrapped = json.dumps({"content": [{"type": "text", "text": body}]})
        self.assertEqual(graph.jev_outcome(wrapped)["policy"], "generic")
        bare = json.dumps([{"type": "text", "text": body}])
        self.assertEqual(graph.jev_outcome(bare)["policy"], "generic")
        twice = json.dumps({"content": [{"type": "text", "text": wrapped}]})
        self.assertIsNone(graph.jev_outcome(twice))       # one level only
        self.assertIsNone(graph.jev_outcome("not json"))
        self.assertIsNone(graph.jev_outcome(json.dumps({"policy": "generic"})))   # not a Jev response
        self.assertIsNone(graph.jev_outcome({"text": body}))
        big = json.dumps(answer(pad="x" * graph.JEV_RESULT_MAX))
        self.assertIsNone(graph.jev_outcome(big))
        self.assertIsNone(graph.jev_outcome(json.dumps({"content": [{"type": "text", "text": big}]})))


class Payload(Writer):
    def test_api_graph_jevs_never_carry_excluded_fields(self):
        claude = self.rows("proj/sess.jsonl", [
            self.claude_use(self.now - 30, "toolu_" + CANARY), self.claude_result(self.now - 29, "toolu_" + CANARY, answer()),
            self.claude_use(self.now - 20, "toolu_B"),
            self.claude_result(self.now - 19, "toolu_B", answer(ok=False, error={"reason": "http_error", "message": CANARY}))])
        pi = self.rows("pi1.jsonl", [{"type": "session", "id": "pi1", "cwd": str(self.dir)},
                                     self.pi_call(self.now - 15, "p_" + CANARY), self.pi_result(self.now - 14, "p_" + CANARY, answer())])
        codex = self.rows("rollout-cx1.jsonl", [
            {"timestamp": iso(self.now - 100), "type": "session_meta", "payload": {"id": "cx1", "cwd": str(self.dir)}},
            self.codex_call(self.now - 12, "c_" + CANARY), self.codex_output(self.now - 11, "c_" + CANARY, answer())])
        store = graph.GraphStore.__new__(graph.GraphStore)
        store._files, store._lock, store._snapshot, store._events = {}, threading.Lock(), None, []
        store._files[claude] = (None, graph.parse_file(claude))
        store._files[pi] = (None, graph.parse_pi_file(pi, self.now))
        store._files[codex] = (None, graph.parse_codex_file(codex, self.now))
        store._publish(self.now)
        with mock.patch.object(graph.GraphStore, "_loop", lambda self: None), \
                mock.patch.object(metrics.Metrics, "_loop", lambda self: None):
            server = importlib.import_module("server")
        with mock.patch.object(server, "GRAPH", store), mock.patch.object(server, "JOBS_DIR", self.dir / "none"), \
                mock.patch.object(server, "open_session_ids", return_value=set()):
            payload = server.graph_payload()
        jevs = payload["jevs"]
        self.assertEqual([c["agent"] for c in jevs], ["codex:cx1", "pi:pi1", "sess", "sess"])   # newest first
        for call in jevs:
            self.assertEqual(set(call), {"id", "agent", "at", "end", "phase", "outcome"})
            self.assertEqual(set(call["outcome"]), {"ok", "fail_open", "latency_ms", "policy", "applied", "reason", "error", "audit"})
            for row in call["outcome"]["audit"]:
                self.assertEqual(set(row), {"applied", "measure", "value_pm", "threshold_pm", "yes_pm", "no_pm"})
        text = json.dumps(jevs)
        self.assertNotIn(CANARY, text)
        self.assertNotIn(CANARY.lower(), text.lower())
        self.assertNotIn("0.555", text)    # the noul probability, as a number or per-mille
        self.assertFalse(any(row["value_pm"] == 555 for c in jevs for row in c["outcome"]["audit"]))
        failed = next(c for c in jevs if c["outcome"]["ok"] is False)
        self.assertEqual((failed["outcome"]["error"], failed["outcome"]["policy"]), ("http_error", "generic"))


if __name__ == "__main__":
    unittest.main()
