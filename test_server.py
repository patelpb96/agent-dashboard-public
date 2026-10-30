"""Run with: python -m unittest test_server -v"""
import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import server


class LocalOnly(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Stop the background scan from replacing the snapshot while a test has a node injected.
        cls.no_publish = mock.patch.object(server.GRAPH, "_publish", lambda *args, **kwargs: None)
        cls.no_publish.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.no_publish.stop()

    def request(self, method, path, headers, body=None):
        """Send exactly these headers (no automatic Host), return (status, headers, body)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers.items():
                conn.putheader(name, value)
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            resp = conn.getresponse()
            return resp.status, resp.headers, resp.read()
        finally:
            conn.close()

    def local(self, **extra):
        return {"Host": f"127.0.0.1:{self.port}", **extra}

    def inject(self, node_id):
        with server.GRAPH._lock:
            server.GRAPH._snapshot["nodes"][node_id] = {"kind": "doc"}
        self.addCleanup(self.remove, node_id)

    @staticmethod
    def remove(node_id):
        with server.GRAPH._lock:
            server.GRAPH._snapshot["nodes"].pop(node_id, None)

    def open_folder(self, node_id, headers):
        body = json.dumps({"id": node_id}).encode("utf-8")
        with mock.patch.object(server.os, "startfile", create=True) as startfile, \
                mock.patch.object(server.subprocess, "Popen") as popen:
            status, _, data = self.request("POST", "/api/open-folder", headers, body)
        return status, json.loads(data), startfile, popen

    def temp_doc(self):
        """A doc: node id for a file in a fresh temporary folder, and that folder."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        (folder / "notes.md").write_text("x", encoding="utf-8")
        return "doc:" + str(folder / "notes.md").replace("\\", "/"), str(folder)

    def test_loopback_host_is_served(self):
        status, headers, body = self.request("GET", "/api/waits", self.local())
        self.assertEqual(status, 200)
        self.assertIn("jobs", json.loads(body))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_other_or_missing_host_is_refused(self):
        for headers in ({"Host": "evil.example"}, {"Host": f"evil.example:{self.port}"}, {}):
            status, _, body = self.request("GET", "/api/waits", headers)
            self.assertEqual(status, 421, headers)
            self.assertEqual(json.loads(body), {"error": "local requests only"})

    def test_bracketed_ipv6_host_is_served(self):
        status, _, _ = self.request("GET", "/api/waits", {"Host": f"[::1]:{self.port}"})
        self.assertEqual(status, 200)

    def test_cross_site_post_is_refused(self):
        node_id, _ = self.temp_doc()
        self.inject(node_id)   # a real folder, so only the Origin check stands in the way
        status, data, startfile, popen = self.open_folder(node_id, self.local(Origin="http://evil.example"))
        self.assertEqual(status, 403)
        self.assertEqual(data, {"error": "cross-site request refused"})
        startfile.assert_not_called()
        popen.assert_not_called()

    def test_network_path_is_not_opened(self):
        node_id = "doc://evil/share/x.md"
        self.inject(node_id)
        status, data, startfile, popen = self.open_folder(node_id, self.local())
        self.assertEqual(status, 400)
        self.assertEqual(data, {"error": "only local folders can be opened"})
        startfile.assert_not_called()
        popen.assert_not_called()

    def test_local_folder_is_opened(self):
        node_id, folder = self.temp_doc()
        self.inject(node_id)
        status, data, startfile, popen = self.open_folder(
            node_id, self.local(Origin=f"http://127.0.0.1:{self.port}"))
        self.assertEqual(status, 200)
        self.assertEqual(data["folder"], folder)
        if os.name == "nt":
            startfile.assert_called_once_with(folder)
            popen.assert_not_called()
        else:
            popen.assert_called_once()
            self.assertEqual(popen.call_args[0][0][1], folder)
            startfile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
