"""Tests for the normal view's Score panel switching to the health-v2
composed score (engram-alpha#2757, part of #2518).

Scope: the normal (default) view's top "Health Score" card must render the
health-v2 composed score (with its snapshot's ``written_at``), not the v1
``d.health_score`` -- and must expose an in-panel link that jumps straight
to the Detail view. Both are pure client-side JS changes to the same inline
`<script>` block `test_viz_health_view_toggle.py` and
`test_viz_health_v2_detail_view.py` already cover -- same convention here:
static assertions on the SERVED page's inline script text (no headless
browser on this seat, so nothing here can execute the JS and observe a live
DOM -- see those two files' own module docstrings for the same constraint).
"""
from __future__ import annotations

import re
import sys
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viz import server as viz_server  # noqa: E402


class _LiveServerBase(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), viz_server.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()

    def _get(self, path: str) -> str:
        with urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
            return resp.read().decode()


def _extract_inline_script(html_text: str) -> str:
    m = re.search(r"<script>(.*?)</script>", html_text, re.DOTALL)
    assert m is not None, "no inline <script> block found"
    return m.group(1)


def _extract_function(script: str, name: str) -> str:
    """Extract a top-level `function <name>(...) { ... }` block via brace
    counting -- same helper as test_viz_health_v2_detail_view.py's, needed
    here because render() contains many nested `{...}` template-literal /
    callback bodies that a naive line-anchored regex would stop at
    prematurely."""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", script)
    assert m is not None, f"{name} not found in inline script"
    start = m.end() - 1
    depth = 0
    for i in range(start, len(script)):
        if script[i] == "{":
            depth += 1
        elif script[i] == "}":
            depth -= 1
            if depth == 0:
                return script[m.start():i + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


class TestLoadHealthFetchesV2(_LiveServerBase):
    def test_load_health_fetches_health_v2(self):
        body = self._get("/health")
        script = _extract_inline_script(body)
        fn_body = _extract_function(script, "loadHealth")
        self.assertIn("_withAgent('/api/health/v2')", fn_body)

    def test_v2_fetch_has_non_fatal_catch(self):
        """Per the spec: like the existing /api/calibration fetch, a failed
        /api/health/v2 fetch must never break the rest of the normal-view
        render."""
        body = self._get("/health")
        script = _extract_inline_script(body)
        fn_body = _extract_function(script, "loadHealth")
        v2_idx = fn_body.find("_withAgent('/api/health/v2')")
        self.assertGreater(v2_idx, -1)
        tail = fn_body[v2_idx:v2_idx + 200]
        self.assertIn(".catch(() => null)", tail)

    def test_render_called_with_v2_second_arg(self):
        body = self._get("/health")
        script = _extract_inline_script(body)
        fn_body = _extract_function(script, "loadHealth")
        self.assertIn("render(data, v2)", fn_body)


class TestRenderFailHonestBranch(_LiveServerBase):
    def test_render_signature_takes_v2(self):
        body = self._get("/health")
        script = _extract_inline_script(body)
        self.assertRegex(script, r"function render\(d,\s*v2\)")

    def test_fail_honest_message_present(self):
        body = self._get("/health")
        script = _extract_inline_script(body)
        fn_body = _extract_function(script, "render")
        self.assertIn("health-v2 score unavailable", fn_body)

    def test_no_v1_health_score_fallback_in_score_panel(self):
        """The OLD unconditional `${d.health_score}` interpolation inside
        the Score-panel card's own template literal must be gone -- checked
        precisely against the Score-panel's own block (bounded by its
        `<h3>Health Score</h3>` marker and the next `<h3>` after it), so
        this can never be tripped by the separate Health Score Trend card,
        which legitimately still reads trend-array `t.health_score` values
        elsewhere in the same file."""
        body = self._get("/health")
        script = _extract_inline_script(body)
        fn_body = _extract_function(script, "render")
        panel_start = fn_body.find("<h3>Health Score</h3>")
        self.assertGreater(panel_start, -1, "Score panel marker not found")
        next_h3 = fn_body.find("<h3>", panel_start + len("<h3>Health Score</h3>"))
        self.assertGreater(next_h3, -1, "couldn't bound the Score panel block")
        score_panel_block = fn_body[panel_start:next_h3]
        self.assertNotIn("${d.health_score}", score_panel_block)

    def test_no_gauge_arc_drawn_when_unavailable(self):
        """drawGauge() must not be called unconditionally -- only guarded on
        a real v2 composed value."""
        body = self._get("/health")
        script = _extract_inline_script(body)
        fn_body = _extract_function(script, "render")
        self.assertNotIn("drawGauge(d.health_score)", fn_body)
        self.assertIn("if (v2Score !== null) drawGauge(v2Score);", fn_body)

    def test_composed_value_used_for_score_and_color(self):
        body = self._get("/health")
        script = _extract_inline_script(body)
        fn_body = _extract_function(script, "render")
        self.assertIn("v2.composed", fn_body)
        self.assertIn("scoreColor(v2Score)", fn_body)

    def test_provenance_written_at_rendered(self):
        body = self._get("/health")
        script = _extract_inline_script(body)
        fn_body = _extract_function(script, "render")
        self.assertIn("v2.provenance", fn_body)
        self.assertIn("written_at", fn_body)


class TestScorePanelCoverageCaveat(_LiveServerBase):
    """engram-alpha#2757 round 2: Ariadne's contract blocker -- composed.value
    must never render without composed_coverage beside it. Same
    static-source-assertion convention as the rest of this file (no headless
    browser on this seat, so these check the render() SOURCE's conditional
    structure -- i.e. that the partial-coverage branch exists and computes/
    renders a coverage percentage, and that it is gated so it does not fire
    unconditionally -- rather than executing the JS against a live/mocked
    /api/health/v2 response body, which this file has no mechanism for."""

    def test_composed_coverage_read_from_v2(self):
        body = self._get("/health")
        script = _extract_inline_script(body)
        fn_body = _extract_function(script, "render")
        self.assertIn("v2.composed.composed_coverage", fn_body)

    def test_coverage_percentage_computed_and_rendered(self):
        body = self._get("/health")
        script = _extract_inline_script(body)
        fn_body = _extract_function(script, "render")
        self.assertIn("Math.round(coverage * 100)", fn_body)
        self.assertIn("% coverage", fn_body)

    def test_coverage_caveat_gated_on_partial_coverage_only(self):
        """The caveat-rendering branch must be conditioned on coverage < 1 --
        additive only, so a full-coverage (or absent-field) snapshot renders
        nothing extra. Checked by confirming the DEFAULT value is the empty
        string, and that the percentage computation only appears AFTER (i.e.
        nested inside) the `coverage < 1` guard -- so a coverage of exactly
        1.0, or a null/undefined field, falls through to the untouched
        default and renders nothing extra."""
        body = self._get("/health")
        script = _extract_inline_script(body)
        fn_body = _extract_function(script, "render")
        cov_var_idx = fn_body.find("let coverageHtml = '';")
        self.assertGreater(cov_var_idx, -1, "default empty coverageHtml not found")
        guard_idx = fn_body.find("coverage < 1", cov_var_idx)
        self.assertGreater(guard_idx, -1, "`coverage < 1` guard not found after default")
        pct_idx = fn_body.find("Math.round(coverage * 100)", guard_idx)
        self.assertGreater(
            pct_idx, -1,
            "percentage computation must appear AFTER the `coverage < 1` "
            "guard -- i.e. nested inside it, not unconditional",
        )

    def test_coverage_caveat_optionally_names_unscored_aspects(self):
        body = self._get("/health")
        script = _extract_inline_script(body)
        fn_body = _extract_function(script, "render")
        self.assertIn("v2.composed.unscored_aspects", fn_body)
        self.assertIn("unavailable", fn_body)

    def test_coverage_value_escaped(self):
        """Follow the file's existing esc() convention for interpolated
        values (round 1 already escapes v2Score the same way)."""
        body = self._get("/health")
        script = _extract_inline_script(body)
        fn_body = _extract_function(script, "render")
        self.assertIn("esc(coveragePct)", fn_body)
        self.assertIn("esc(unscoredAspects)", fn_body)


class TestScorePanelDetailsLink(_LiveServerBase):
    def test_details_link_id_present_in_served_markup(self):
        body = self._get("/health")
        self.assertIn('id="score-panel-details-link"', body)

    def test_details_link_calls_apply_view_detail(self):
        body = self._get("/health")
        script = _extract_inline_script(body)
        fn_body = _extract_function(script, "render")
        link_idx = fn_body.find('id="score-panel-details-link"')
        self.assertGreater(link_idx, -1)
        tail = fn_body[link_idx:link_idx + 200]
        self.assertIn("applyView('detail', true)", tail)


class TestScorePanelUnavailableSentinelGate(_LiveServerBase):
    """engram-alpha#2757 round 3: engine.py's ``_to_jsonable()`` converts the
    UNAVAILABLE sentinel to the JSON string literal ``"UNAVAILABLE"`` (never
    ``null``) whenever ``compose_aspect_scores()`` holds every aspect out of
    composition (``points_in_play <= 0``) -- a real, reachable state (e.g. a
    fresh agent graph with no scoreable data yet). The v2Score gate must
    route that string into the same fail-honest branch as null/undefined,
    never treat it as a real score. Same static-source-assertion convention
    as the rest of this file -- no headless browser on this seat, so these
    check the render() SOURCE's gate expression directly."""

    def test_v2score_gate_excludes_unavailable_string(self):
        body = self._get("/health")
        script = _extract_inline_script(body)
        fn_body = _extract_function(script, "render")
        v2score_idx = fn_body.find("const v2Score =")
        self.assertGreater(v2score_idx, -1, "v2Score assignment not found")
        # Bound the gate to just its own statement (up to the first ';'
        # after the ternary), so a coincidental later 'UNAVAILABLE' mention
        # elsewhere in render() can't produce a false pass.
        stmt_end = fn_body.find(";", v2score_idx)
        self.assertGreater(stmt_end, -1)
        gate_stmt = fn_body[v2score_idx:stmt_end]
        self.assertIn("!== 'UNAVAILABLE'", gate_stmt)

    def test_numeric_zero_not_excluded_by_gate(self):
        """The gate must be a precise equality check against the string
        literal 'UNAVAILABLE', never a loose truthy/typeof check that would
        also reject a legitimate numeric 0 composed score -- a real score of
        exactly zero must still pass through as v2Score, not be
        fabricated-unavailable. Discriminating check: the existing
        null/undefined comparisons are still present (additive fix, not a
        rewrite) and no truthy/typeof-based exclusion -- which is the only
        shape of check that could accidentally also reject 0 -- was
        introduced."""
        body = self._get("/health")
        script = _extract_inline_script(body)
        fn_body = _extract_function(script, "render")
        v2score_idx = fn_body.find("const v2Score =")
        stmt_end = fn_body.find(";", v2score_idx)
        gate_stmt = fn_body[v2score_idx:stmt_end]
        self.assertNotIn("!v2.composed.value", gate_stmt)
        self.assertNotIn("typeof v2.composed.value", gate_stmt)
        self.assertIn("!== null", gate_stmt)
        self.assertIn("!== undefined", gate_stmt)


class TestExistingBehaviorUnaffected(_LiveServerBase):
    """Sanity checks that this change didn't disturb the sibling Trend card
    or the pre-existing view-toggle machinery."""

    def test_trend_card_still_reads_health_trend_array(self):
        body = self._get("/health")
        script = _extract_inline_script(body)
        self.assertIn("d.health_trend", script)
        self.assertIn("t.health_score", script)

    def test_apply_view_function_unchanged_signature(self):
        body = self._get("/health")
        script = _extract_inline_script(body)
        self.assertIn("function applyView(view, persist)", script)


if __name__ == "__main__":
    unittest.main()
