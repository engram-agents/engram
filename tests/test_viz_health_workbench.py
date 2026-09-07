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
import shutil
import sys
import tempfile
import threading
import unittest
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

    def tearDown(self):
        viz_server._workbench_available = self._saved_available
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


if __name__ == "__main__":
    unittest.main()
