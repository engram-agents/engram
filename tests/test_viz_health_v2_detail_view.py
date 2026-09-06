"""Tests for the health-v2 DETAIL VIEW's history strip + distribution slice
(engram-alpha#2518, detail-view slice over /api/health/v2/series).

Scope: this is the frontend slice that CONSUMES `/api/health/v2` and
`/api/health/v2/series` -- both endpoints and their backend loaders
(`viz/data/health_v2.py`, `health/series.py`) are already built and covered
by `tests/test_viz_health_v2.py`; nothing here re-tests them.

Same `_LiveServerBase` convention as `tests/test_viz_health_view_toggle.py`
and `tests/test_viz_health_v2.py` (real `HTTPServer` thread + `urlopen`, no
headless browser available on this seat) -- so the JS render logic itself is
covered two ways:

  1. STATIC assertions on the served `/health` page's inline `<script>` text
     (function names, endpoint strings) -- the only thing a plain HTTP fetch
     can observe about client-side JS without executing it.
  2. A genuine PURE-MATH unit test for `computeDistributionStats` /
     `computeHistogramBins` / `percentile`, run directly under `node` via
     `subprocess` against the standalone `viz/static/health-detail-math.js`
     file those functions live in (Node.js IS available on this seat --
     `node --version` succeeds -- so this is real executed-JS coverage, not
     a proxy).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import unittest
from collections import namedtuple
from http.server import HTTPServer
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viz import server as viz_server  # noqa: E402

_MATH_JS_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "engram"
    / "viz"
    / "static"
    / "health-detail-math.js"
)

_NODE_AVAILABLE = shutil.which("node") is not None

# `.status`/`.headers` on an `http.client.HTTPResponse` are populated
# up-front and remain readable after close; `.read()` needs the still-open
# socket -- so the body must be drained INSIDE the `with` block, before the
# context manager's `__exit__` closes it. This tuple carries all three back
# out already-materialized, so callers can't accidentally call `.read()`
# on an already-closed response (which silently returns b"", not an error).
_Response = namedtuple("_Response", ["status", "headers", "body"])


class _LiveServerBase(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), viz_server.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()

    def _get(self, path: str) -> _Response:
        with urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
            return _Response(resp.status, resp.headers, resp.read())

    def _get_text(self, path: str) -> str:
        return self._get(path).body.decode()


def _extract_inline_script(html_text: str) -> str:
    m = re.search(r"<script>(.*?)</script>", html_text, re.DOTALL)
    assert m is not None, "no inline <script> block found"
    return m.group(1)


class TestDetailViewFunctionNamesPresent(_LiveServerBase):
    """Static assertions: the new function names actually written into the
    inline script, and that the detail-view section fetches both endpoints."""

    def test_new_function_names_present_in_inline_script(self):
        body = self._get_text("/health")
        script = _extract_inline_script(body)
        for name in (
            "loadDetailView",
            "renderDetailView",
            "renderHistoryStrip",
            "renderDistributionPanel",
            "computeDistributionStats",
            "computeHistogramBins",
        ):
            self.assertIn(name, script, f"{name} not found in inline script")

    def test_fetches_both_health_v2_endpoints(self):
        body = self._get_text("/health")
        script = _extract_inline_script(body)
        self.assertIn("_withAgent('/api/health/v2')", script)
        self.assertIn("_withAgent('/api/health/v2/series')", script)

    def test_series_fetch_has_non_fatal_catch(self):
        """Per the spec: the series fetch (unlike the /api/health/v2 fetch)
        matches loadHealth()'s existing `.catch(() => null)` pattern for a
        non-fatal fetch."""
        body = self._get_text("/health")
        script = _extract_inline_script(body)
        series_idx = script.find("_withAgent('/api/health/v2/series')")
        self.assertGreater(series_idx, -1)
        # The catch clause should appear on the same statement, shortly after.
        tail = script[series_idx:series_idx + 200]
        self.assertIn(".catch(() => null)", tail)

    def test_fetch_only_once_per_page_load_guard_present(self):
        """loadDetailView() must guard on a single-fetch flag (e.g.
        `_detailLoaded`) so Normal<->Detail toggling doesn't refetch."""
        body = self._get_text("/health")
        script = _extract_inline_script(body)
        fn_match = re.search(
            r"function loadDetailView\(\)\s*\{(.*?)\n  \}", script, re.DOTALL
        )
        self.assertIsNotNone(fn_match, "couldn't find loadDetailView()")
        fn_body = fn_match.group(1)
        self.assertIn("if (_detailLoaded) return;", fn_body)
        self.assertIn("_detailLoaded = true;", fn_body)

    def test_apply_view_hooks_load_detail_view(self):
        """applyView()'s detail branch must call loadDetailView() -- the
        hook that covers both the initial-page-load-in-detail case and a
        later Normal->Detail toggle click."""
        body = self._get_text("/health")
        script = _extract_inline_script(body)
        fn_match = re.search(
            r"function applyView\(view, persist\)\s*\{(.*?)\n  \}", script, re.DOTALL
        )
        self.assertIsNotNone(fn_match, "couldn't find applyView()")
        fn_body = fn_match.group(1)
        self.assertIn("loadDetailView()", fn_body)


class TestMathFileServedAndReferenced(_LiveServerBase):
    def test_health_page_references_math_static_file(self):
        body = self._get_text("/health")
        self.assertIn('<script src="/static/health-detail-math.js"></script>', body)

    def test_math_static_file_served_with_js_content_type(self):
        resp = self._get("/static/health-detail-math.js")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("Content-Type"), "application/javascript")
        body = resp.body.decode()
        self.assertIn("function computeDistributionStats", body)
        self.assertIn("function computeHistogramBins", body)


class TestDetailViewMarkup(_LiveServerBase):
    def test_detail_content_containers_present(self):
        body = self._get_text("/health")
        for elem_id in (
            "detail-empty-state",
            "detail-content",
            "detail-scores-note",
            "detail-composed-block",
            "detail-signal-rows",
            "detail-aspect-tiles",
        ):
            self.assertIn(f'id="{elem_id}"', body)

    def test_detail_content_hidden_by_default(self):
        body = self._get_text("/health")
        m = re.search(r'<div id="detail-content"([^>]*)>', body)
        self.assertIsNotNone(m)
        self.assertIn("display:none", m.group(1).replace(" ", ""))

    def test_empty_state_message_still_present(self):
        """The pre-existing slice-1 placeholder text must survive as the
        served (pre-JS) markup -- test_viz_health_view_toggle.py already
        asserts this too; re-asserted here since this slice touches the
        same div."""
        body = self._get_text("/health")
        self.assertIn("No detail-view snapshot data yet", body)


@unittest.skipUnless(_NODE_AVAILABLE, "node not available on this seat")
class TestDistributionMathViaNode(unittest.TestCase):
    """Genuine executed-JS coverage for the pure math: requires the actual
    `health-detail-math.js` file under `node` and asserts on known
    inputs -> known outputs (percentile nearest-rank, population sd,
    Freedman-Diaconis with Sturges fallback)."""

    def _run_node(self, script: str):
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(
            result.returncode, 0,
            f"node script failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}",
        )
        return json.loads(result.stdout)

    def test_math_js_file_exists(self):
        self.assertTrue(_MATH_JS_PATH.is_file(), f"missing {_MATH_JS_PATH}")

    def test_distribution_stats_known_inputs(self):
        script = f"""
        const m = require({json.dumps(str(_MATH_JS_PATH))});
        console.log(JSON.stringify(m.computeDistributionStats([1,2,3,4,5])));
        """
        stats = self._run_node(script)
        self.assertEqual(stats["min"], 1)
        self.assertEqual(stats["p10"], 1)
        self.assertEqual(stats["p25"], 2)
        self.assertEqual(stats["p50"], 3)
        self.assertEqual(stats["p75"], 4)
        self.assertEqual(stats["p90"], 5)
        self.assertEqual(stats["max"], 5)
        self.assertEqual(stats["mean"], 3)
        self.assertAlmostEqual(stats["sd"], 1.4142135623730951, places=9)

    def test_distribution_stats_empty_returns_null(self):
        script = f"""
        const m = require({json.dumps(str(_MATH_JS_PATH))});
        console.log(JSON.stringify(m.computeDistributionStats([]) === null));
        """
        self.assertTrue(self._run_node(script))

    def test_histogram_bins_known_inputs(self):
        script = f"""
        const m = require({json.dumps(str(_MATH_JS_PATH))});
        console.log(JSON.stringify(m.computeHistogramBins([1,2,3,4,5])));
        """
        hist = self._run_node(script)
        self.assertEqual(len(hist["counts"]), 5)
        self.assertEqual(sum(hist["counts"]), 5)
        self.assertEqual(len(hist["edges"]), len(hist["counts"]) + 1)
        self.assertAlmostEqual(hist["edges"][0], 1)
        self.assertAlmostEqual(hist["edges"][-1], 5)

    def test_histogram_bins_degenerate_single_value_repeated(self):
        """max === min (a constant series) -- must not divide by zero, and
        must clamp to a single visible bin holding every point."""
        script = f"""
        const m = require({json.dumps(str(_MATH_JS_PATH))});
        console.log(JSON.stringify(m.computeHistogramBins([7,7,7])));
        """
        hist = self._run_node(script)
        self.assertEqual(hist["counts"], [3])
        self.assertEqual(len(hist["edges"]), 2)

    def test_histogram_bins_clamped_to_5_and_20(self):
        script = f"""
        const m = require({json.dumps(str(_MATH_JS_PATH))});
        const wide = Array.from({{length: 500}}, (_, i) => i);
        const b = m.computeHistogramBins(wide);
        console.log(JSON.stringify({{n: b.counts.length}}));
        """
        result = self._run_node(script)
        self.assertGreaterEqual(result["n"], 5)
        self.assertLessEqual(result["n"], 20)

    def test_percentile_nearest_rank_indices(self):
        script = f"""
        const m = require({json.dumps(str(_MATH_JS_PATH))});
        const sorted = [10, 20, 30, 40, 50];
        console.log(JSON.stringify({{
          p10: m.percentile(sorted, 10),
          p50: m.percentile(sorted, 50),
          p90: m.percentile(sorted, 90),
        }}));
        """
        result = self._run_node(script)
        self.assertEqual(result["p10"], 10)
        self.assertEqual(result["p50"], 30)
        self.assertEqual(result["p90"], 50)


if __name__ == "__main__":
    unittest.main()
