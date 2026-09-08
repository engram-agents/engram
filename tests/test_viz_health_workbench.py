"""Tests for the Curve Workbench route + tier gate (engram-alpha#2727).

Scope: the SERVER-side route/gate behavior only --
``/health/workbench`` and ``/api/health/v2/model`` in ``viz/server.py``,
plus ``viz.data.health_workbench.get_workbench_model``. Not covered here:
the page's client-side JS render logic (covered separately by
``tests/test_health_workbench_curve_parity.py`` for the pure math, and by
static string assertions the way ``tests/test_viz_health_v2_detail_view.py``
covers the detail view -- out of scope for THIS file per the dispatching
brief's test list).

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
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), viz_server.Handler)
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


class TestWorkbenchTierGate(_LiveServerBase):
    """Both directions of the tier gate -- flipped via monkeypatching
    ``viz_server._workbench_available`` directly (the function is
    deliberately a plain function, not a cached constant, exactly so tests
    can do this without faking the filesystem)."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="engram_workbench_http_test_")
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

    def setUp(self):
        super().setUp()
        self._saved_available = viz_server._workbench_available
        self._saved_access_log_path = viz_server._ACCESS_LOG_PATH

    def tearDown(self):
        viz_server._workbench_available = self._saved_available
        viz_server._ACCESS_LOG_PATH = self._saved_access_log_path
        super().tearDown()

    def test_real_gate_renders_200_for_real_seat_fixture(self):
        # No monkeypatching -- this exercises the REAL _workbench_available()
        # against the actual files on disk in this checkout (which do ship
        # health_workbench.html/.py, since this is the source tree, not a
        # built convenience/essential plugin bundle).
        status, headers, body = self._get("/health/workbench?agent=test_agent")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"Curve Workbench", body)

    def test_gate_closed_page_404(self):
        viz_server._workbench_available = lambda: False
        status, body = self._get_json("/health/workbench?agent=test_agent")
        self.assertEqual(status, 404)
        self.assertIn("error", body)
        self.assertEqual(body.get("reason"), "not_developer_tier")

    def test_gate_closed_model_endpoint_404(self):
        viz_server._workbench_available = lambda: False
        status, body = self._get_json("/api/health/v2/model")
        self.assertEqual(status, 404)
        self.assertEqual(body.get("reason"), "not_developer_tier")

    def test_gate_open_page_renders_200(self):
        # MUTATION per the dispatching brief: flip the gate, assert it
        # opens -- this is the discriminating direction (a gate that is
        # ALWAYS closed would also pass a "closed" assertion).
        viz_server._workbench_available = lambda: True
        status, headers, body = self._get("/health/workbench?agent=test_agent")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"Curve Workbench", body)

    def test_gate_open_model_endpoint_200(self):
        viz_server._workbench_available = lambda: True
        status, body = self._get_json("/api/health/v2/model")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("available"))
        self.assertIn("model_defs", body)

    def test_unknown_agent_returns_404_same_shape_as_sibling_endpoints(self):
        viz_server._workbench_available = lambda: True
        status, body = self._get_json("/health/workbench?agent=nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)
        self.assertEqual(body["agent"], "nope")

    def test_health_page_header_link_present_when_gate_open(self):
        viz_server._workbench_available = lambda: True
        status, _headers, body = self._get("/health?agent=test_agent")
        self.assertEqual(status, 200)
        self.assertIn(b"/health/workbench", body)

    def test_health_page_header_link_absent_when_gate_closed(self):
        viz_server._workbench_available = lambda: False
        status, _headers, body = self._get("/health?agent=test_agent")
        self.assertEqual(status, 200)
        self.assertNotIn(b"/health/workbench", body)

    # -- R5.2: cache-busting -------------------------------------------

    def test_workbench_page_script_tags_carry_nonempty_version_stamp(self):
        viz_server._workbench_available = lambda: True
        status, _headers, body = self._get("/health/workbench?agent=test_agent")
        self.assertEqual(status, 200)
        m_curve = re.search(rb'src="/static/health-workbench-curve\.js\?v=([^"]+)"', body)
        m_main = re.search(rb'src="/static/health-workbench\.js\?v=([^"]+)"', body)
        self.assertIsNotNone(m_curve, body)
        self.assertIsNotNone(m_main, body)
        self.assertTrue(len(m_curve.group(1)) > 0)
        self.assertTrue(len(m_main.group(1)) > 0)
        # Both script tags carry the SAME stamp -- computed once at import
        # time (see server.py's _compute_static_version_stamp), not
        # per-tag or per-request.
        self.assertEqual(m_curve.group(1), m_main.group(1))

    def test_static_request_still_returns_cache_control_header(self):
        # R5.2 keeps max-age=3600 for static files -- the version-stamp
        # query string is what busts the cache, not a shortened TTL.
        status, headers, body = self._get(
            f"/static/health-workbench.js?v={viz_server._STATIC_VERSION_STAMP}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), viz_server._STATIC_CACHE_CONTROL)
        self.assertTrue(len(body) > 0)

    # -- R5.5: opt-in access log -----------------------------------------

    def test_access_log_off_by_default_no_file_written(self):
        # Unset (the class default via setUp/tearDown save-restore) --
        # requesting a route must not create a log file anywhere; there's
        # no path configured to write to, so there's nothing to check
        # beyond "the request still succeeds".
        self.assertIsNone(viz_server._ACCESS_LOG_PATH)
        status, _body = self._get_json("/api/agents")
        self.assertEqual(status, 200)

    def test_access_log_appends_well_formed_line(self):
        tmpdir = tempfile.mkdtemp(prefix="engram_workbench_access_log_test_")
        log_path = str(Path(tmpdir) / "access.log")
        viz_server._ACCESS_LOG_PATH = log_path
        try:
            status, _body = self._get_json("/api/agents")
            self.assertEqual(status, 200)
            with open(log_path, encoding="utf-8") as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 1)
            line = lines[0].rstrip("\n")
            # ISO-UTC method path status duration_ms remote
            m = re.match(
                r"^(\S+) (\S+) (\S+) (\d+) ([\d.]+) (\S+)$", line
            )
            self.assertIsNotNone(m, line)
            ts, method, path, status_field, duration_ms, remote = m.groups()
            # ISO-UTC timestamp parses.
            datetime.fromisoformat(ts)
            self.assertEqual(method, "GET")
            self.assertEqual(path, "/api/agents")
            self.assertEqual(status_field, "200")
            self.assertGreaterEqual(float(duration_ms), 0.0)
            self.assertEqual(remote, "127.0.0.1")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_model_endpoint_jsonable_raise_is_fail_honest_not_500(self):
        """A future shape change making model_defs_jsonable/curve_defs_jsonable
        raise must still hit the endpoint's documented ``{"available": False,
        "reason": ...}`` fail-honest shape -- not a 500 -- end-to-end, not just
        on the import boundary (engram-alpha#2730, reviewer-fairy suggestion 2).
        Same HTTP status (200) as the sibling gate-open-but-fails path uses:
        this endpoint is fail-honest-via-200, not fail-honest-via-404 (that
        status is reserved for the tier gate itself -- see
        test_gate_closed_model_endpoint_404 above)."""
        from health import model as health_model

        viz_server._workbench_available = lambda: True
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
        self.assertNotIn("model_module_unavailable", body.get("reason", ""))


class TestComputeStaticVersionStamp(unittest.TestCase):
    """engram-alpha#2748 item 1: exercise both fallback branches of
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
        candidates = [
            viz_server._STATIC_DIR / "health-workbench-curve.js",
            viz_server._STATIC_DIR / "health-workbench.js",
        ]
        expected = str(int(max(p.stat().st_mtime for p in candidates if p.is_file())))

        def _raise_oserror(*_a, **_k):
            raise OSError("resolver binary missing")

        original_run = viz_server.subprocess.run
        viz_server.subprocess.run = _raise_oserror
        try:
            stamp = viz_server._compute_static_version_stamp()
        finally:
            viz_server.subprocess.run = original_run
        self.assertTrue(stamp)
        self.assertTrue(stamp.isdigit())
        self.assertEqual(stamp, expected)

    def test_run_nonzero_returncode_falls_back_to_mtime(self):
        """subprocess.run returning rc != 0 (e.g. not inside any
        resolvable tree) must also fall through to the mtime branch, same
        as the OSError case above."""
        candidates = [
            viz_server._STATIC_DIR / "health-workbench-curve.js",
            viz_server._STATIC_DIR / "health-workbench.js",
        ]
        expected = str(int(max(p.stat().st_mtime for p in candidates if p.is_file())))

        class _FakeResult:
            returncode = 128
            stdout = ""

        def _fake_run(*_a, **_k):
            return _FakeResult()

        original_run = viz_server.subprocess.run
        viz_server.subprocess.run = _fake_run
        try:
            stamp = viz_server._compute_static_version_stamp()
        finally:
            viz_server.subprocess.run = original_run
        self.assertTrue(stamp)
        self.assertTrue(stamp.isdigit())
        self.assertEqual(stamp, expected)

    def test_run_fails_and_mtime_unresolvable_returns_terminal_zero(self):
        """When the version-control resolver is unresolvable AND the two
        candidate JS files aren't found at _STATIC_DIR (mtime lookup finds
        nothing), the function must return the terminal fallback "0"."""

        def _raise_oserror(*_a, **_k):
            raise OSError("resolver binary missing")

        empty_dir = Path(tempfile.mkdtemp())
        original_run = viz_server.subprocess.run
        original_static_dir = viz_server._STATIC_DIR
        viz_server.subprocess.run = _raise_oserror
        viz_server._STATIC_DIR = empty_dir
        try:
            stamp = viz_server._compute_static_version_stamp()
        finally:
            viz_server.subprocess.run = original_run
            viz_server._STATIC_DIR = original_static_dir
            shutil.rmtree(empty_dir, ignore_errors=True)
        self.assertEqual(stamp, "0")


if __name__ == "__main__":
    unittest.main()
