"""Tests for the Curve Workbench (engram-alpha#2727, moved to its own
standalone entry point by engram-alpha#2754 PR B).

Scope, post-#2754 PR B:

* ``viz.data.health_workbench.get_workbench_model`` -- unchanged, tested
  directly (no HTTP layer).
* ``viz.workbench`` -- the NEW standalone ``:5021`` entry point that now
  owns the editable page + its own copies of the four read-only APIs it
  needs. This is where the OLD ``TestWorkbenchTierGate`` class's coverage
  moved to (adapted: there is no per-request tier gate on this process
  any more -- see ``viz.workbench``'s own module docstring, JUDGMENT
  CALL 1 -- so the "gate closed" test pairs are gone; what remains is the
  positive-path coverage plus the cache-busting / static-serving
  regression tests, which ARE still load-bearing).
* ``viz.server`` -- a small regression suite confirming the OLD
  ``/health/workbench`` route and the ``/health`` page's Curve Workbench
  header link are BOTH gone from ``:5001`` now that the page lives
  elsewhere (PR B's own explicit requirement).

Not covered here: the page's client-side JS render logic (covered
separately by ``tests/test_health_workbench_curve_parity.py`` for the
pure math, and by static string assertions the way
``tests/test_viz_health_v2_detail_view.py`` covers the detail view --
out of scope for THIS file per the dispatching brief's test list).

Style follows ``tests/test_viz_health_v2.py``'s ``_LiveServerBase``
convention (real ``HTTPServer`` thread + ``urlopen``, no headless browser
available on this seat).
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viz import agents as viz_agents  # noqa: E402
from viz import server as viz_server  # noqa: E402
from viz import workbench as viz_workbench  # noqa: E402
from viz.data import health_workbench as viz_health_workbench_data  # noqa: E402


def _make_fixture_snapshot(turn: int) -> dict:
    """A minimal but real-shaped health-v2 snapshot, one signal
    (``taint_rate``), enough to exercise the workbench route without
    depending on all nine real model signals being present."""
    return {
        "health_version": "0.4.0-pr5",
        "raw_schema_version": "1",
        "model_version": "1",
        "anchor": "deadbeef",
        "turn": turn,
        "checkpoint_mode": "nap",
        "measurements": {
            "claim_bearing_pending_retract_marker": 2,
            "claim_bearing_current": 40,
        },
        "signal_defs": {
            "taint_rate": {
                "numerator": "claim_bearing_pending_retract_marker",
                "denominator": "claim_bearing_current",
                "form": "rate",
                "gates": [],
                "formula": "claim_bearing_pending_retract_marker / claim_bearing_current",
            },
        },
        "signals": {"taint_rate": 0.05},
        "config": {
            "aspects": [
                {
                    "name": "epistemic_integrity",
                    "weight": 40.0,
                    "metrics": [{"name": "taint_rate", "scored": True}],
                },
            ]
        },
        "scores": {
            "aspects": {"epistemic_integrity": 0.95},
            "unscored_aspects": [],
            "aspect_coverage": {
                "epistemic_integrity": {"metrics_in_play": ["taint_rate"], "coverage": 1.0}
            },
            "composed": 38.0,
            "points_in_play": 40.0,
            "composed_coverage": 1.0,
        },
    }


class _LiveServerBase(unittest.TestCase):
    """Live-server harness parameterized by *HANDLER_CLASS* -- a real
    ``HTTPServer`` thread running the given Handler class + ``urlopen``,
    no headless browser available on this seat. Subclasses set
    ``HANDLER_CLASS`` to either ``viz_server.Handler`` (:5001) or
    ``viz_workbench.Handler`` (:5021, the new standalone entry point)."""

    HANDLER_CLASS = None  # set by subclasses

    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), self.HANDLER_CLASS)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()

    def _get(self, path: str):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urlopen(url, timeout=5) as resp:
                return resp.status, resp.headers, resp.read()
        except HTTPError as e:
            return e.code, e.headers, e.read()

    def _get_json(self, path: str):
        status, _headers, body = self._get(path)
        return status, json.loads(body)


class TestWorkbenchModelData(unittest.TestCase):
    """viz.data.health_workbench.get_workbench_model, called directly --
    no HTTP layer. Real health.engine.MODEL is importable in this test
    environment (conftest.py inserts src/engram onto sys.path), so this
    exercises the real, non-mocked model."""

    def test_available_and_shaped(self):
        result = viz_health_workbench_data.get_workbench_model()
        self.assertTrue(result["available"], result.get("reason"))
        self.assertIn("model_defs", result)
        self.assertIn("curve_defs", result)
        aspects = result["model_defs"]["aspects"]
        metrics = result["model_defs"]["metrics"]
        self.assertTrue(len(aspects) > 0)
        self.assertTrue(len(metrics) > 0)
        for m in metrics:
            self.assertIn("signal", m)
            self.assertIn("aspect", m)
            self.assertIn("curve", m)
            self.assertIn("importance", m)
            self.assertIn(m["curve"], result["curve_defs"])
        for a in aspects:
            self.assertIn("name", a)
            self.assertIn("points", a)


class TestWorkbenchServerNoLongerHosts(_LiveServerBase):
    """engram-alpha#2754 PR B: the OLD /health/workbench route and the
    /health page's Curve Workbench header link are BOTH removed from
    viz.server (:5001) now that the page moved to its own standalone
    entry point (viz.workbench, :5021). This is the direct regression
    coverage for that removal -- the OLD TestWorkbenchTierGate class's
    "gate open/closed" pairs tested THIS process's own
    _workbench_available(); that function no longer exists on
    viz.server at all (removed, not just gated closed), so there is no
    monkeypatch-based equivalent to carry forward here -- the route is
    unconditionally gone."""

    HANDLER_CLASS = viz_server.Handler

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="engram_workbench_removed_http_test_")
        cls.engram_home = Path(cls.tmpdir) / "agent_dir"
        cls.engram_home.mkdir()
        cls.db_path = cls.engram_home / "knowledge.db"
        cls.db_path.write_bytes(b"")

        cls._saved_agents = viz_agents.AGENTS
        cls._saved_default = viz_agents.DEFAULT_AGENT
        viz_agents.AGENTS = {
            "test_agent": {"name": "test_agent", "label": "Test", "db": str(cls.db_path)}
        }
        viz_agents.DEFAULT_AGENT = "test_agent"

    @classmethod
    def tearDownClass(cls):
        viz_agents.AGENTS = cls._saved_agents
        viz_agents.DEFAULT_AGENT = cls._saved_default
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_workbench_route_gone_from_5001(self):
        status, _headers, _body = self._get("/health/workbench?agent=test_agent")
        self.assertEqual(status, 404)

    def test_health_page_never_carries_workbench_link_or_placeholder(self):
        status, _headers, body = self._get("/health?agent=test_agent")
        self.assertEqual(status, 200)
        self.assertNotIn(b"/health/workbench", body)
        self.assertNotIn(b"WORKBENCH_LINK_PLACEHOLDER", body)

    def test_model_endpoint_still_served_on_5001(self):
        # Explicit requirement: the four read-only APIs (including this
        # one) stay on :5001 even though the PAGE moved.
        status, body = self._get_json("/api/health/v2/model")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("available"), body.get("reason"))
        self.assertIn("model_defs", body)


class TestWorkbenchStandaloneServer(_LiveServerBase):
    """viz.workbench (:5021 by default) -- the NEW standalone entry
    point. No per-request tier gate exists on this process (see that
    module's own docstring, JUDGMENT CALL 1): this process only runs at
    all because the dev-tier files are present, checked once at import
    time, not per-request -- so there is no "gate closed" pairing to
    test here, only the positive path."""

    HANDLER_CLASS = viz_workbench.Handler

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="engram_workbench_standalone_http_test_")
        cls.engram_home = Path(cls.tmpdir) / "agent_dir"
        cls.engram_home.mkdir()
        cls.db_path = cls.engram_home / "knowledge.db"
        cls.db_path.write_bytes(b"")
        cls.snap_dir = cls.engram_home / "health-snapshots"
        cls.snap_dir.mkdir()
        snapshot = _make_fixture_snapshot(turn=5)
        (cls.snap_dir / "5-nap-20260904T120000Z.json").write_text(json.dumps(snapshot))

        cls._saved_agents = viz_agents.AGENTS
        cls._saved_default = viz_agents.DEFAULT_AGENT
        viz_agents.AGENTS = {
            "test_agent": {"name": "test_agent", "label": "Test", "db": str(cls.db_path)}
        }
        viz_agents.DEFAULT_AGENT = "test_agent"

    @classmethod
    def tearDownClass(cls):
        viz_agents.AGENTS = cls._saved_agents
        viz_agents.DEFAULT_AGENT = cls._saved_default
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_page_renders_200_at_root(self):
        status, headers, body = self._get("/?agent=test_agent")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"Curve Workbench", body)

    def test_unknown_agent_returns_404_same_shape_as_sibling_endpoints(self):
        status, body = self._get_json("/?agent=nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)
        self.assertEqual(body["agent"], "nope")

    def test_model_endpoint_200_unconditionally(self):
        status, body = self._get_json("/api/health/v2/model")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("available"))
        self.assertIn("model_defs", body)

    def test_health_v2_endpoint_served(self):
        status, body = self._get_json("/api/health/v2?agent=test_agent")
        self.assertEqual(status, 200)
        self.assertEqual(body.get("agent"), "test_agent")

    def test_health_v2_series_endpoint_served(self):
        status, body = self._get_json("/api/health/v2/series?agent=test_agent")
        self.assertEqual(status, 200)
        self.assertEqual(body.get("agent"), "test_agent")

    def test_agents_endpoint_served(self):
        status, body = self._get_json("/api/agents")
        self.assertEqual(status, 200)
        self.assertIn("agents", body)
        self.assertEqual(body.get("default"), "test_agent")

    def test_unknown_route_404s(self):
        status, _headers, _body = self._get("/api/health")  # a :5001-only route
        self.assertEqual(status, 404)

    # -- cache-busting -----------------------------------------------

    def test_workbench_page_script_tags_carry_nonempty_version_stamp(self):
        status, _headers, body = self._get("/?agent=test_agent")
        self.assertEqual(status, 200)
        m_curve = re.search(rb'src="/static/health-workbench-curve\.js\?v=([^"]+)"', body)
        m_main = re.search(rb'src="/static/health-workbench\.js\?v=([^"]+)"', body)
        self.assertIsNotNone(m_curve, body)
        self.assertIsNotNone(m_main, body)
        self.assertTrue(len(m_curve.group(1)) > 0)
        self.assertTrue(len(m_main.group(1)) > 0)
        # Both script tags carry the SAME stamp -- computed once at import
        # time (see viz.workbench._compute_static_version_stamp), not
        # per-tag or per-request.
        self.assertEqual(m_curve.group(1), m_main.group(1))

    def test_static_request_still_returns_cache_control_header(self):
        # The version-stamp query string is what busts the cache, not a
        # shortened TTL -- static/ still caches for 1h.
        status, headers, body = self._get(
            f"/static/health-workbench.js?v={viz_workbench._STATIC_VERSION_STAMP}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            headers.get("Cache-Control"),
            viz_workbench.static_serve._STATIC_CACHE_CONTROL,
        )
        self.assertTrue(len(body) > 0)

    def test_model_endpoint_jsonable_raise_is_fail_honest_not_500(self):
        """A future shape change making model_defs_jsonable/curve_defs_jsonable
        raise must still hit the endpoint's documented ``{"available": False,
        "reason": ...}`` fail-honest shape -- not a 500 -- end-to-end, not just
        on the import boundary (engram-alpha#2730, reviewer-fairy suggestion 2).
        Same HTTP status (200) as the sibling gate-open-but-fails path uses."""
        from health import model as health_model

        original = health_model.model_defs_jsonable

        def _boom(*_a, **_k):
            raise RuntimeError("boom-from-test")

        health_model.model_defs_jsonable = _boom
        try:
            status, body = self._get_json("/api/health/v2/model")
        finally:
            health_model.model_defs_jsonable = original
        self.assertEqual(status, 200)
        self.assertFalse(body.get("available"))
        self.assertIn("boom-from-test", body.get("reason", ""))


class TestWorkbenchAccessLog(_LiveServerBase):
    """engram-alpha#2767: viz.workbench.Handler.log_message mirrors
    viz.server's own opt-in R5.5 access log -- off by default
    (_ACCESS_LOG_PATH is None, matching viz.server's own default), one
    line per request when a path is set. Toggled directly on the module
    attribute (the same monkeypatch shape viz.server's own tests would
    use, per that module's docstring: "so tests can set
    viz_server._ACCESS_LOG_PATH directly ... without going through
    argparse/main()")."""

    HANDLER_CLASS = viz_workbench.Handler

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="engram_workbench_access_log_test_")
        cls.engram_home = Path(cls.tmpdir) / "agent_dir"
        cls.engram_home.mkdir()
        cls.db_path = cls.engram_home / "knowledge.db"
        cls.db_path.write_bytes(b"")

        cls._saved_agents = viz_agents.AGENTS
        cls._saved_default = viz_agents.DEFAULT_AGENT
        viz_agents.AGENTS = {
            "test_agent": {"name": "test_agent", "label": "Test", "db": str(cls.db_path)}
        }
        viz_agents.DEFAULT_AGENT = "test_agent"

    @classmethod
    def tearDownClass(cls):
        viz_agents.AGENTS = cls._saved_agents
        viz_agents.DEFAULT_AGENT = cls._saved_default
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self._saved_access_log_path = viz_workbench._ACCESS_LOG_PATH

    def tearDown(self):
        viz_workbench._ACCESS_LOG_PATH = self._saved_access_log_path
        super().tearDown()

    def test_off_by_default_no_file_written(self):
        # Matches viz.server's own default (None, unchanged behavior) --
        # a request must not create anything at a path nobody set.
        log_path = Path(self.tmpdir) / "unset-access.log"
        viz_workbench._ACCESS_LOG_PATH = None
        status, _headers, _body = self._get("/api/agents")
        self.assertEqual(status, 200)
        self.assertFalse(log_path.exists())

    def test_appends_well_formed_line_when_set(self):
        log_path = Path(self.tmpdir) / "access.log"
        viz_workbench._ACCESS_LOG_PATH = str(log_path)
        status, _headers, _body = self._get("/api/agents")
        self.assertEqual(status, 200)
        lines = log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        # ISO-UTC method path status duration_ms remote -- same shape as
        # viz.server's own log line.
        ts, method, path, code, duration_ms, remote = lines[0].split(" ")
        self.assertEqual(method, "GET")
        self.assertEqual(path, "/api/agents")
        self.assertEqual(code, "200")
        self.assertGreaterEqual(float(duration_ms), 0.0)
        self.assertEqual(remote, "127.0.0.1")
        # Parses as a real ISO-8601 UTC timestamp.
        datetime.fromisoformat(ts)

    def test_appends_one_line_per_request(self):
        log_path = Path(self.tmpdir) / "multi.log"
        viz_workbench._ACCESS_LOG_PATH = str(log_path)
        for _ in range(3):
            status, _headers, _body = self._get("/api/agents")
            self.assertEqual(status, 200)
        lines = log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)


class TestAccessLogParityWithServer(unittest.TestCase):
    """engram-alpha#2769 (Borges's held-for-one-test request, forum #405
    #8313): #2767 mirrored viz.server's log_message onto viz.workbench BY
    HAND, which is exactly the mechanism that let the two drift apart in
    the first place -- two independently-maintained copies of the same
    logic, no shared implementation, no test tying them together. This
    test is that tie: it runs BOTH servers side by side, hits the SAME
    route (`/api/agents`, served identically on both) with identical
    inputs, and asserts the two resulting log lines have the IDENTICAL
    SHAPE -- same field count, same field types in the same order. A
    future edit to one Handler's log_message that isn't mirrored to the
    other's fails HERE, not silently."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="engram_access_log_parity_test_")
        cls.engram_home = Path(cls.tmpdir) / "agent_dir"
        cls.engram_home.mkdir()
        cls.db_path = cls.engram_home / "knowledge.db"
        cls.db_path.write_bytes(b"")

        cls._saved_agents = viz_agents.AGENTS
        cls._saved_default = viz_agents.DEFAULT_AGENT
        viz_agents.AGENTS = {
            "test_agent": {"name": "test_agent", "label": "Test", "db": str(cls.db_path)}
        }
        viz_agents.DEFAULT_AGENT = "test_agent"

        cls.server_srv = HTTPServer(("127.0.0.1", 0), viz_server.Handler)
        cls.server_port = cls.server_srv.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server_srv.serve_forever)
        cls.server_thread.daemon = True
        cls.server_thread.start()

        cls.workbench_srv = HTTPServer(("127.0.0.1", 0), viz_workbench.Handler)
        cls.workbench_port = cls.workbench_srv.server_address[1]
        cls.workbench_thread = threading.Thread(target=cls.workbench_srv.serve_forever)
        cls.workbench_thread.daemon = True
        cls.workbench_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server_srv.shutdown()
        cls.workbench_srv.shutdown()
        viz_agents.AGENTS = cls._saved_agents
        viz_agents.DEFAULT_AGENT = cls._saved_default
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        self._saved_server_path = viz_server._ACCESS_LOG_PATH
        self._saved_workbench_path = viz_workbench._ACCESS_LOG_PATH

    def tearDown(self):
        viz_server._ACCESS_LOG_PATH = self._saved_server_path
        viz_workbench._ACCESS_LOG_PATH = self._saved_workbench_path

    def _line_shape(self, line: str) -> tuple:
        """(field_count, method_type_ok, path_type_ok, status_type_ok,
        duration_type_ok, remote_type_ok) -- the SHAPE of a log line,
        independent of its specific values (timestamps/ports differ
        between the two servers by construction)."""
        fields = line.split(" ")
        if len(fields) != 6:
            return (len(fields),)
        ts, method, path, code, duration_ms, remote = fields
        return (
            len(fields),
            isinstance(method, str) and method.isalpha(),
            path.startswith("/"),
            code.isdigit(),
            _is_float(duration_ms),
            remote.count(".") == 3 or remote == "-",
        )

    def test_log_line_shape_is_identical_between_server_and_workbench(self):
        server_log = Path(self.tmpdir) / "server-access.log"
        workbench_log = Path(self.tmpdir) / "workbench-access.log"
        viz_server._ACCESS_LOG_PATH = str(server_log)
        viz_workbench._ACCESS_LOG_PATH = str(workbench_log)

        with urlopen(f"http://127.0.0.1:{self.server_port}/api/agents", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        with urlopen(f"http://127.0.0.1:{self.workbench_port}/api/agents", timeout=5) as resp:
            self.assertEqual(resp.status, 200)

        server_lines = server_log.read_text(encoding="utf-8").splitlines()
        workbench_lines = workbench_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(server_lines), 1)
        self.assertEqual(len(workbench_lines), 1)

        server_shape = self._line_shape(server_lines[0])
        workbench_shape = self._line_shape(workbench_lines[0])
        self.assertEqual(
            server_shape, workbench_shape,
            f"viz.server line: {server_lines[0]!r}\n"
            f"viz.workbench line: {workbench_lines[0]!r}\n"
            "Shapes diverged -- the two Handler.log_message implementations "
            "are no longer mirrored (engram-alpha#2769's own drift mechanism)."
        )
        # Both requested the SAME route with the SAME method -- those two
        # fields should be byte-identical, not merely same-shaped.
        self.assertEqual(server_lines[0].split(" ")[1], workbench_lines[0].split(" ")[1])
        self.assertEqual(server_lines[0].split(" ")[2], workbench_lines[0].split(" ")[2])
        self.assertEqual(server_lines[0].split(" ")[3], workbench_lines[0].split(" ")[3])


def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


class TestComputeStaticVersionStamp(unittest.TestCase):
    """engram-alpha#2748 item 1 (moved to viz.workbench by engram-alpha#2754
    PR B, since that's the only process serving a page that references
    these two script tags now): exercise both fallback branches of
    ``_compute_static_version_stamp`` -- the mtime branch (git resolver
    fails) and the terminal ``"0"`` branch (mtime also unresolvable). The
    stamp is computed ONCE at import time into the module constant
    ``_STATIC_VERSION_STAMP``, so these tests call the function directly
    rather than depending on re-import order."""

    def test_run_raises_oserror_falls_back_to_mtime(self):
        """subprocess.run raising OSError (the installed-plugin-tree case
        per the function's own docstring -- resolver binary missing) must
        fall through to the mtime branch: a non-empty all-digit string
        equal to str(int(max mtime of the two workbench JS files))."""
        import subprocess

        candidates = [
            viz_workbench.static_serve._STATIC_DIR / "health-workbench-curve.js",
            viz_workbench.static_serve._STATIC_DIR / "health-workbench.js",
        ]
        expected = str(int(max(p.stat().st_mtime for p in candidates if p.is_file())))

        def _raise_oserror(*_a, **_k):
            raise OSError("resolver binary missing")

        original_run = subprocess.run
        subprocess.run = _raise_oserror
        try:
            stamp = viz_workbench._compute_static_version_stamp()
        finally:
            subprocess.run = original_run
        self.assertTrue(stamp)
        self.assertTrue(stamp.isdigit())
        self.assertEqual(stamp, expected)

    def test_run_nonzero_returncode_falls_back_to_mtime(self):
        """subprocess.run returning rc != 0 (e.g. not inside any
        resolvable tree) must also fall through to the mtime branch, same
        as the OSError case above."""
        import subprocess

        candidates = [
            viz_workbench.static_serve._STATIC_DIR / "health-workbench-curve.js",
            viz_workbench.static_serve._STATIC_DIR / "health-workbench.js",
        ]
        expected = str(int(max(p.stat().st_mtime for p in candidates if p.is_file())))

        class _FakeResult:
            returncode = 128
            stdout = ""

        def _fake_run(*_a, **_k):
            return _FakeResult()

        original_run = subprocess.run
        subprocess.run = _fake_run
        try:
            stamp = viz_workbench._compute_static_version_stamp()
        finally:
            subprocess.run = original_run
        self.assertTrue(stamp)
        self.assertTrue(stamp.isdigit())
        self.assertEqual(stamp, expected)

    def test_run_fails_and_mtime_unresolvable_returns_terminal_zero(self):
        """When the version-control resolver is unresolvable AND the two
        candidate JS files aren't found at static_serve._STATIC_DIR (mtime
        lookup finds nothing), the function must return the terminal
        fallback "0"."""
        import subprocess

        def _raise_oserror(*_a, **_k):
            raise OSError("resolver binary missing")

        empty_dir = Path(tempfile.mkdtemp())
        original_run = subprocess.run
        original_static_dir = viz_workbench.static_serve._STATIC_DIR
        subprocess.run = _raise_oserror
        viz_workbench.static_serve._STATIC_DIR = empty_dir
        try:
            stamp = viz_workbench._compute_static_version_stamp()
        finally:
            subprocess.run = original_run
            viz_workbench.static_serve._STATIC_DIR = original_static_dir
            shutil.rmtree(empty_dir, ignore_errors=True)
        self.assertEqual(stamp, "0")


if __name__ == "__main__":
    unittest.main()
