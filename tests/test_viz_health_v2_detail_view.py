"""Tests for the health-v2 DETAIL VIEW's Curve-Workbench-fork slice
(engram-alpha#2754 PR A -- read-only fork of health-workbench.js).

Scope: this is the frontend slice that CONSUMES `/api/health/v2`,
`/api/health/v2/series`, and (optionally, as an enrichment layer)
`/api/health/v2/model` -- all three endpoints and their backend loaders
(`viz/data/health_v2.py`, `viz/data/health_workbench.py`, `health/series.py`)
are already built and covered by `tests/test_viz_health_v2.py` /
`tests/test_viz_health_workbench.py`; nothing here re-tests them.

Before #2754, the Detail view's render logic lived entirely INSIDE
health.html's inline `<script>` (plain string-built HTML, no DOM API), which
let this file extract and run `renderHistoryStrip` etc. directly under
`node`. #2754 PR A forked the Curve Workbench (health-workbench.js) instead,
which builds its DOM via the real `document.createElementNS`/`.appendChild`
API -- exactly like health-workbench.js's own render logic, which
`tests/test_viz_health_workbench.py`'s own module docstring explicitly
excludes from node-executed testing for that reason ("the page's client-side
JS render logic" is "out of scope for THIS file", covered only by the pure-
math parity tests + static string assertions). health-detail.js follows the
same convention: its DOM-touching functions are covered by static
string/structure assertions on the served page + the served static file,
never by extracting-and-running them under plain `node` (no jsdom on this
seat) -- the pure math it calls (piecewiseLinear, collapseUnavailableRuns,
computeDistributionStats, ...) is already covered, unchanged, by
tests/test_health_workbench_curve_parity.py, tests/test_health_workbench_
round5_parity.py, and the TestDistributionMathViaNode class below.

Same `_LiveServerBase` convention as `tests/test_viz_health_view_toggle.py`
and `tests/test_viz_health_v2.py` (real `HTTPServer` thread + `urlopen`, no
headless browser available on this seat).
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
_DETAIL_JS_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "engram"
    / "viz"
    / "static"
    / "health-detail.js"
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


class TestDetailScriptIncludesPresentAndOrdered(_LiveServerBase):
    """health.html must load health-detail-math.js, then health-workbench-
    curve.js (the shared pure-math library, per #2754's "share only the
    pure math library, fork the page" ruling), then health-detail.js -- in
    that order, and all three BEFORE the page's own inline <script> (which
    synchronously calls the view-toggle's `applyView()`, whose Detail
    branch calls `loadDetailView()` -- defined in health-detail.js -- at
    page-load time when the initial view already resolves to 'detail')."""

    def test_three_script_tags_present_in_order(self):
        body = self._get_text("/health")
        idx_math = body.index('<script src="/static/health-detail-math.js"></script>')
        idx_curve = body.index('<script src="/static/health-workbench-curve.js"></script>')
        idx_detail = body.index('<script src="/static/health-detail.js"></script>')
        idx_inline = body.index("<script>")
        self.assertLess(idx_math, idx_curve)
        self.assertLess(idx_curve, idx_detail)
        self.assertLess(idx_detail, idx_inline)

    def test_detail_js_served_with_js_content_type(self):
        resp = self._get("/static/health-detail.js")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("Content-Type"), "application/javascript")

    def test_curve_js_served_with_js_content_type(self):
        """Sibling regression guard for the tiers.json retag this PR makes
        (dev -> convenience for health-workbench-curve.js): the static
        route itself doesn't consult tiers.json (that's a BUILD-time
        filter, not a request-time gate), so this only confirms the file is
        still served correctly from source -- the tier retag's actual
        purpose (surviving a --tier convenience build) is a packaging-time
        property, not something a live-server test can exercise."""
        resp = self._get("/static/health-workbench-curve.js")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("Content-Type"), "application/javascript")


class TestDetailJsFileContract(unittest.TestCase):
    """Static assertions on health-detail.js's own source -- the entry
    point contract, and that every editing affordance named in the issue's
    strip-list is actually gone."""

    def setUp(self):
        self.src = _DETAIL_JS_PATH.read_text(encoding="utf-8")

    def test_file_exists(self):
        self.assertTrue(_DETAIL_JS_PATH.is_file(), f"missing {_DETAIL_JS_PATH}")

    def test_exposes_global_entry_point(self):
        self.assertIn("window.loadDetailView", self.src)

    def test_wrapped_in_iife_no_leaked_globals(self):
        """health.html's inline <script> declares its OWN `render`/`el`-
        shaped globals for the Normal view -- an unscoped health-workbench.
        js-style helper set would silently collide. Confirm the fork stays
        wrapped -- stripping the leading `//`-comment block first (the
        module docstring), since the IIFE itself is the first STATEMENT,
        not necessarily the first line of the file."""
        body_without_leading_comments = re.sub(r"^(\s*//.*\n)+", "", self.src)
        self.assertRegex(body_without_leading_comments.strip(), r"^\(function \(\) \{")
        self.assertRegex(self.src.strip(), r"\}\)\(\);\s*$")

    def test_no_knot_drag_or_insert_ui(self):
        for gone in ("insertMidKnot", "delBtn", "plusBtn", "kbtn add"):
            self.assertNotIn(gone, self.src, f"editing affordance {gone!r} should be stripped")

    def test_no_importance_input_element(self):
        # The workbench's editable importance <input> -- replaced here by a
        # plain read-only <div class="metric"> readout (see
        # renderSignalCard).
        self.assertNotIn('"data-imp"', self.src)
        self.assertNotIn("validateImportance", self.src)

    def test_no_export_apply_reset(self):
        for gone in ("buildExportPayload", "refreshExportBox", "wb-export", "saveEdits", "loadEdits"):
            self.assertNotIn(gone, self.src, f"export/persistence affordance {gone!r} should be stripped")

    def test_no_set_as_reference_control_but_markers_survive(self):
        # No functional "set as reference" control (the button/click-handler
        # this fork removes) -- a comment MAY still mention the phrase
        # (documenting what health-workbench.js's own control looked like),
        # so this checks for the functional markers, not the bare string.
        self.assertNotIn("setReference(", self.src)
        self.assertNotIn("refBtn", self.src)
        self.assertIn("hold-out metric", self.src)
        self.assertIn("current reference", self.src)

    def test_no_changed_highlight_overlay(self):
        # curvesDiffer / the stored-vs-edited dashed overlay -- moot here
        # since nothing is ever edited.
        self.assertNotIn("curvesDiffer", self.src)
        self.assertNotIn('"changed"', self.src)

    def test_reuses_shared_math_functions_by_name(self):
        # These are NOT redeclared in health-detail.js -- they're globals
        # from health-workbench-curve.js, loaded first (see the script-
        # order test above). Referencing them by name here confirms the
        # fork didn't accidentally duplicate the math instead of sharing
        # it, per the issue's "share only the pure math library" ruling.
        for name in (
            "piecewiseLinear", "aspectScore", "composeScore",
            "collapseUnavailableRuns", "dominantReasonKey",
            "formatCheckpointLabel", "computeAxisTicks", "clamp",
        ):
            self.assertIn(name, self.src)

    def test_gracefully_degrades_when_model_unavailable(self):
        self.assertIn("modelAvailable", self.src)
        self.assertIn("renderFlat", self.src)


class TestDetailViewMarkup(_LiveServerBase):
    def test_detail_content_containers_present(self):
        body = self._get_text("/health")
        for elem_id in (
            "detail-empty-state",
            "detail-content",
            "detail-scores-note",
            "detail-composed-block",
            "detail-aspect-sections",
        ):
            self.assertIn(f'id="{elem_id}"', body)

    def test_old_flat_containers_removed(self):
        """The pre-fork flat-list containers (#2518) are gone -- replaced
        by #detail-aspect-sections, which renders either aspect-grouped
        (model available) or flat (model unavailable) content at runtime,
        per health-detail.js's own render()/renderFlat()/
        renderAspectGrouped()."""
        body = self._get_text("/health")
        self.assertNotIn('id="detail-signal-rows"', body)
        self.assertNotIn('id="detail-aspect-tiles"', body)

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

    def test_apply_view_hooks_load_detail_view(self):
        """applyView()'s detail branch must still call loadDetailView() --
        now the global entry point health-detail.js exposes -- the hook
        that covers both the initial-page-load-in-detail case and a later
        Normal->Detail toggle click. Unchanged by this PR (out of scope:
        the spec explicitly keeps this hook's trigger condition untouched,
        only what it calls)."""
        body = self._get_text("/health")
        m = re.search(r"function applyView\(view, persist\)\s*\{(.*?)\n  \}", body, re.DOTALL)
        self.assertIsNotNone(m, "couldn't find applyView()")
        self.assertIn("loadDetailView()", m.group(1))


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


@unittest.skipUnless(_NODE_AVAILABLE, "node not available on this seat")
class TestDistributionMathViaNode(unittest.TestCase):
    """Genuine executed-JS coverage for the pure math: requires the actual
    `health-detail-math.js` file under `node` and asserts on known
    inputs -> known outputs (percentile nearest-rank, population sd,
    Freedman-Diaconis with Sturges fallback). Unchanged by #2754 -- this
    file's own math is untouched, only its consumers (health-detail.js
    instead of the old inline renderDistributionPanel) changed."""

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
