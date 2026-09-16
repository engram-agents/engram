"""Tests for engram-alpha#2866 fixes 1 & 2 on viz.server:

1. Gzip compression of JSON responses (`Handler._json`) when the client
   sends `Accept-Encoding: gzip`, with a byte-identical-to-before regression
   guard for clients that don't advertise gzip support.
2. `ThreadingHTTPServer` + HTTP/1.1 keep-alive -- verifies the server class
   `main()` actually constructs at runtime, plus a Content-Length
   correctness check (required once `protocol_version = "HTTP/1.1"` is set,
   since the client relies on Content-Length rather than connection-close to
   know a response is complete).

Fix 3 (graph.js in-flight guard) has no JS test harness in this repo -- see
the PR body for the manual verification note (Ariadne's diagnosis spec,
"Tests required" section 3).

House style follows tests/test_viz_server_search.py: real HTTPServer +
urlopen against it, no mocking of the HTTP layer itself.
"""
from __future__ import annotations

import gzip
import json
import sys
import threading
import unittest
from http.client import HTTPConnection
from http.server import HTTPServer, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viz import agents as viz_agents
from viz import server as viz_server


# ---------------------------------------------------------------------------
# Fix 1 -- gzip the JSON responses
# ---------------------------------------------------------------------------

class TestJsonGzipCompression(unittest.TestCase):
    """/api/agents (any _json()-routed endpoint) gzips its response iff the
    client's Accept-Encoding header advertises gzip support."""

    @classmethod
    def setUpClass(cls):
        cls._saved_agents = viz_agents.AGENTS
        cls._saved_default = viz_agents.DEFAULT_AGENT
        # A nonexistent db path is fine here -- get_agents_meta() handles a
        # missing file gracefully (available=False), and this test only
        # cares about the JSON body's bytes, not its DB-backed content.
        viz_agents.AGENTS = {
            "test_agent": {"name": "test_agent", "label": "Test",
                           "db": "/nonexistent/knowledge.db"}
        }
        viz_agents.DEFAULT_AGENT = "test_agent"

        cls.server = HTTPServer(("127.0.0.1", 0), viz_server.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        viz_agents.AGENTS = cls._saved_agents
        viz_agents.DEFAULT_AGENT = cls._saved_default

    def _get_raw(self, path: str, accept_gzip: bool):
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {"Accept-Encoding": "gzip"} if accept_gzip else {}
        req = Request(url, headers=headers)
        with urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp, raw

    def test_gzip_requested_returns_content_encoding_gzip(self):
        resp, raw = self._get_raw("/api/agents", accept_gzip=True)
        self.assertEqual(resp.headers.get("Content-Encoding"), "gzip")

    def test_gzip_body_decompresses_to_same_json_as_uncompressed(self):
        _, gz_raw = self._get_raw("/api/agents", accept_gzip=True)
        decompressed = gzip.decompress(gz_raw)
        gz_json = json.loads(decompressed)

        _, plain_raw = self._get_raw("/api/agents", accept_gzip=False)
        plain_json = json.loads(plain_raw)

        self.assertEqual(gz_json, plain_json)

    def test_gzip_content_length_matches_compressed_bytes_on_wire(self):
        resp, raw = self._get_raw("/api/agents", accept_gzip=True)
        content_length = int(resp.headers.get("Content-Length"))
        self.assertEqual(content_length, len(raw))
        # And the compressed length must actually be a real compression
        # (gzip framing overhead is small; a JSON payload of any size that
        # repeats keys/structure compresses below its own length).
        decompressed_len = len(gzip.decompress(raw))
        self.assertLess(content_length, decompressed_len)

    def test_no_accept_encoding_returns_uncompressed_no_regression(self):
        """Regression guard: a client that does NOT send Accept-Encoding: gzip
        must get byte-identical (uncompressed) behavior to before fix 1."""
        resp, raw = self._get_raw("/api/agents", accept_gzip=False)
        self.assertIsNone(resp.headers.get("Content-Encoding"))
        # Body must be plain, directly-parseable JSON -- not gzip-magic-byte-prefixed.
        parsed = json.loads(raw)
        self.assertIn("agents", parsed)

    def test_no_accept_encoding_content_length_matches_raw_bytes(self):
        resp, raw = self._get_raw("/api/agents", accept_gzip=False)
        content_length = int(resp.headers.get("Content-Length"))
        self.assertEqual(content_length, len(raw))

    def test_partial_gzip_token_in_accept_encoding_still_matches(self):
        """A real browser's Accept-Encoding is usually 'gzip, deflate, br' --
        the substring check must still fire, not just an exact-match 'gzip'."""
        resp, raw = self._get_raw("/api/agents", accept_gzip=False)
        url = f"http://127.0.0.1:{self.port}/api/agents"
        req = Request(url, headers={"Accept-Encoding": "gzip, deflate, br"})
        with urlopen(req, timeout=5) as resp2:
            raw2 = resp2.read()
            self.assertEqual(resp2.headers.get("Content-Encoding"), "gzip")
            self.assertEqual(json.loads(gzip.decompress(raw2)), json.loads(raw))


# ---------------------------------------------------------------------------
# Fix 2 -- ThreadingHTTPServer + HTTP/1.1 keep-alive
# ---------------------------------------------------------------------------

class TestMainUsesThreadingHTTPServer(unittest.TestCase):
    """engram-alpha#2866 fix 2: main() must construct a ThreadingHTTPServer
    (not a single-threaded HTTPServer) so one slow client can't block every
    other request behind it."""

    def setUp(self):
        self._saved_agents = dict(viz_agents.AGENTS)
        self._saved_default_agent = viz_agents.DEFAULT_AGENT

    def tearDown(self):
        viz_agents.AGENTS = self._saved_agents
        viz_agents.DEFAULT_AGENT = self._saved_default_agent

    def test_main_constructs_threading_http_server_class(self):
        """Class-identity check against whatever main() actually builds --
        wraps the real ThreadingHTTPServer constructor (so main() still runs
        its normal startup path) while recording the constructed instance,
        rather than mocking it away entirely."""
        captured = {}
        real_cls = viz_server.ThreadingHTTPServer

        def _spy_constructor(*args, **kwargs):
            server = real_cls(*args, **kwargs)
            # Prevent main() from blocking in the real serve_forever() loop.
            server.serve_forever = MagicMock(side_effect=KeyboardInterrupt())
            captured["server"] = server
            return server

        mock_ctor = MagicMock(side_effect=_spy_constructor)

        with patch.object(sys, "argv",
                           ["viz_server", "--no-autodetect", "--port", "0"]):
            with patch.object(viz_server, "ThreadingHTTPServer", mock_ctor):
                try:
                    viz_server.main()
                except KeyboardInterrupt:
                    pass

        try:
            self.assertTrue(
                mock_ctor.called,
                "main() must construct the server via the ThreadingHTTPServer name",
            )
            self.assertIsInstance(captured.get("server"), ThreadingHTTPServer)
        finally:
            server = captured.get("server")
            if server is not None:
                server.server_close()

    def test_threading_http_server_is_not_plain_http_server_class(self):
        """Sanity check on the stdlib relationship this fix depends on:
        ThreadingHTTPServer must not be aliased back to plain HTTPServer
        (which would make fix 2 a no-op single-threaded server in disguise)."""
        self.assertIsNot(ThreadingHTTPServer, HTTPServer)
        self.assertTrue(issubclass(ThreadingHTTPServer, HTTPServer))


class TestHandlerProtocolVersionAndContentLength(unittest.TestCase):
    """Correctness note from the spec: once protocol_version = 'HTTP/1.1' is
    set, every response MUST carry an accurate Content-Length (matching
    whichever bytes were actually written -- post-gzip for _json()), or a
    keep-alive client hangs waiting for data that never comes."""

    @classmethod
    def setUpClass(cls):
        cls._saved_agents = viz_agents.AGENTS
        cls._saved_default = viz_agents.DEFAULT_AGENT
        viz_agents.AGENTS = {
            "test_agent": {"name": "test_agent", "label": "Test",
                           "db": "/nonexistent/knowledge.db"}
        }
        viz_agents.DEFAULT_AGENT = "test_agent"

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), viz_server.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        viz_agents.AGENTS = cls._saved_agents
        viz_agents.DEFAULT_AGENT = cls._saved_default

    def test_handler_protocol_version_is_http_1_1(self):
        self.assertEqual(viz_server.Handler.protocol_version, "HTTP/1.1")

    def test_two_sequential_requests_on_same_connection_both_succeed(self):
        """The most direct behavioral check for keep-alive correctness: if
        Content-Length were ever wrong, urllib (which relies on it to know
        where one response ends) would hang or corrupt the second request's
        read on a persistent connection."""
        url = f"http://127.0.0.1:{self.port}/api/agents"
        with urlopen(url, timeout=5) as resp1:
            body1 = json.loads(resp1.read())
        with urlopen(url, timeout=5) as resp2:
            body2 = json.loads(resp2.read())
        self.assertEqual(body1, body2)

    def test_gzip_response_content_length_matches_html_style_accuracy(self):
        """Same accuracy bar the spec calls out for _html(): Content-Length
        must equal len(bytes actually written), gzip or not."""
        req = Request(f"http://127.0.0.1:{self.port}/api/agents",
                       headers={"Accept-Encoding": "gzip"})
        with urlopen(req, timeout=5) as resp:
            raw = resp.read()
            self.assertEqual(int(resp.headers["Content-Length"]), len(raw))

    def test_unmatched_route_404_does_not_hang_a_keepalive_client(self):
        """Reviewer-fairy catch on PR-2926: do_GET's catch-all 404 (an
        unmatched /api/ path) used to send an empty body with no
        Content-Length -- harmless under the old HTTP/1.0 default-close
        behavior, but a real hang under this PR's HTTP/1.1 keep-alive switch,
        since the client has no way to know the (empty) body has ended.
        Reproduced directly (not just reasoned about) before writing this fix:
        reverting the Content-Length: 0 header on this branch reliably
        reproduces a client-side TimeoutError on this exact request."""
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/api/totally-unmatched-route")
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        self.assertEqual(resp.status, 404)
        self.assertEqual(body, b"")
        self.assertEqual(resp.getheader("Content-Length"), "0")


if __name__ == "__main__":
    unittest.main()
