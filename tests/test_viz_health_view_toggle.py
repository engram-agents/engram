"""Tests for the health-view toggle shell (engram-alpha#2518, slice 1).

Scope: the toggle chrome + inert "detail" placeholder ONLY -- the real
detail-view data source (a separate `health/` engine package) is still
in-flight in a different, unmerged PR and is deliberately out of scope
here. These tests cover:

  1. The served ``/health`` page contains the toggle control and a
     ``#detail-view`` container.
  2. Default state (no ``?view=`` param): ``#detail-view`` hidden,
     ``#normal-view`` visible.
  3. The inline script's view-resolution logic reads the URL's ``?view=``
     query param, and does so BEFORE falling back to ``localStorage`` --
     since a plain HTTP fetch can't execute JS (no headless browser on this
     seat, same constraint noted in test_viz_frozen_header.py), this is a
     static check on the served inline script rather than a live DOM
     assertion.
  4. The "Snapshot now" button and its dead CSS class are ABSENT
     (engram-alpha#2518, live-score slice -- Lei's direction: the health
     dashboard always recomputes live on page refresh, so there is no
     "snapshot now" action to trigger any more; see
     TestSnapshotNowButtonRemoved below).
  5. Sanity check: the existing dashboard's #dashboard/#cards structure
     (and #loading) survive untouched.
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


class TestToggleControlPresent(_LiveServerBase):
    def test_health_page_has_toggle_and_detail_view(self):
        body = self._get("/health")
        self.assertIn('id="view-toggle"', body)
        self.assertIn('id="view-toggle-normal"', body)
        self.assertIn('id="view-toggle-detail"', body)
        self.assertIn(">Normal<", body)
        self.assertIn(">Detail<", body)
        self.assertIn('id="detail-view"', body)

    def test_detail_view_has_the_empty_state_message(self):
        body = self._get("/health")
        self.assertIn("No detail-view snapshot data yet", body)
        self.assertIn("under construction", body)


class TestDefaultViewState(_LiveServerBase):
    """No ?view= param -> #detail-view hidden, #normal-view visible, in the
    served (pre-JS) markup."""

    def test_detail_view_default_hidden(self):
        body = self._get("/health")
        m = re.search(r'<div id="detail-view"([^>]*)>', body)
        self.assertIsNotNone(m, "couldn't find the #detail-view opening tag")
        self.assertIn("display:none", m.group(1).replace(" ", ""),
                      "#detail-view should default to hidden in the served markup")

    def test_normal_view_default_visible(self):
        body = self._get("/health")
        m = re.search(r'<div id="normal-view"([^>]*)>', body)
        self.assertIsNotNone(m, "couldn't find the #normal-view opening tag")
        self.assertNotIn("display:none", m.group(1).replace(" ", ""),
                         "#normal-view should default to visible in the served markup")


class TestViewParamScriptLogic(_LiveServerBase):
    """A plain HTTP fetch can't execute JS, so this checks the SERVED
    inline script's own view-resolution logic reads the URL's ?view= param
    (and prioritizes it ahead of localStorage), per the spec's allowance
    for a static/inline-script-level check."""

    def test_served_page_ignores_query_string_for_routing(self):
        # /health?view=detail must resolve to the same page (routes.py
        # dispatches on parsed.path, not the raw query string) -- this
        # confirms the query param reaches the client untouched, rather
        # than 404ing or being stripped server-side.
        body_plain = self._get("/health")
        body_detail = self._get("/health?view=detail")
        self.assertEqual(body_plain, body_detail,
                         "the server should serve the identical shell "
                         "regardless of ?view= -- the toggle is client-side")

    def test_script_reads_view_query_param(self):
        body = self._get("/health?view=detail")
        script = _extract_inline_script(body)
        self.assertIn("searchParams.get('view')", script)

    def test_url_param_checked_before_localstorage_fallback(self):
        """Per the spec: "read ?view=detail from the URL query string on
        page load -- if present, it wins. Otherwise, fall back to
        localStorage." Confirm the script's own getInitialView() function
        checks the URL param first, textually, so ?view=detail would in
        fact win over any localStorage value when this script runs."""
        body = self._get("/health?view=detail")
        script = _extract_inline_script(body)
        fn_match = re.search(
            r"function getInitialView\(\).*?\n\s*\}", script, re.DOTALL)
        self.assertIsNotNone(fn_match, "couldn't find getInitialView()")
        fn_body = fn_match.group(0)
        url_idx = fn_body.find("searchParams.get('view')")
        storage_idx = fn_body.find("localStorage.getItem")
        self.assertGreater(url_idx, -1, "getInitialView doesn't read the URL param")
        self.assertGreater(storage_idx, -1, "getInitialView doesn't read localStorage")
        self.assertLess(url_idx, storage_idx,
                        "the URL param check must come before the "
                        "localStorage fallback, so ?view=detail wins")

    def test_apply_view_toggles_detail_and_normal_display(self):
        """Confirm applyView's own logic sets detail-view visible / normal-view
        hidden when passed 'detail' -- the mechanism that makes ?view=detail
        (once read by getInitialView) actually show the detail container."""
        body = self._get("/health")
        script = _extract_inline_script(body)
        self.assertIn(
            "document.getElementById('detail-view').style.display = "
            "isDetail ? '' : 'none';", script)
        self.assertIn(
            "document.getElementById('normal-view').style.display = "
            "isDetail ? 'none' : '';", script)

    def test_replace_state_used_not_reload(self):
        body = self._get("/health")
        script = _extract_inline_script(body)
        self.assertIn("history.replaceState", script)
        self.assertNotIn("location.reload", script)
        self.assertNotIn("location.href =", script)

    def test_default_param_not_added_for_normal(self):
        """Switching back to normal must remove ?view= entirely (clean
        normal-view URLs), not set ?view=normal."""
        body = self._get("/health")
        script = _extract_inline_script(body)
        self.assertIn("url.searchParams.delete('view')", script)
        self.assertIn("url.searchParams.set('view', 'detail')", script)


class TestLocalStorageGuarded(_LiveServerBase):
    """Ariadne's colleague-review finding on #2579: an unguarded
    localStorage.getItem/setItem throws SecurityError in a browser that
    denies site data (Chrome "Block all cookies", Firefox
    dom.storage.enabled=false, some embedded contexts) -- and since
    getInitialView() runs at top level in the inline script, that throw
    aborts the WHOLE script before loadHealth() runs, taking down the
    entire dashboard (not just the toggle's persistence). This is a static
    proxy per her own suggestion ("not asking for browser tests in this
    PR... a static test asserting the getItem call sits inside a try block
    is cheap, catches the regression, and is honest about being a proxy") --
    it cannot itself observe a SecurityError firing, only that the source
    text puts each call where such a throw would be caught."""

    def test_getitem_call_is_inside_a_try_block(self):
        body = self._get("/health")
        script = _extract_inline_script(body)
        fn_match = re.search(
            r"function getInitialView\(\)\s*\{(.*?)\n  \}", script, re.DOTALL)
        self.assertIsNotNone(fn_match, "couldn't find getInitialView()")
        fn_body = fn_match.group(1)
        try_idx = fn_body.find("try {")
        get_idx = fn_body.find("localStorage.getItem")
        catch_idx = fn_body.find("} catch")
        self.assertGreater(try_idx, -1, "getInitialView has no try block")
        self.assertGreater(get_idx, -1, "getInitialView doesn't call localStorage.getItem")
        self.assertGreater(catch_idx, -1, "getInitialView's try has no catch")
        self.assertTrue(
            try_idx < get_idx < catch_idx,
            "localStorage.getItem must sit BETWEEN a try and its catch, "
            "not before the try or after the catch closes")

    def test_setitem_call_is_inside_a_try_block(self):
        body = self._get("/health")
        script = _extract_inline_script(body)
        fn_match = re.search(
            r"function applyView\(view, persist\)\s*\{(.*?)\n  \}", script, re.DOTALL)
        self.assertIsNotNone(fn_match, "couldn't find applyView()")
        fn_body = fn_match.group(1)
        try_idx = fn_body.find("try {")
        set_idx = fn_body.find("localStorage.setItem")
        catch_idx = fn_body.find("} catch")
        self.assertGreater(try_idx, -1, "applyView has no try block")
        self.assertGreater(set_idx, -1, "applyView doesn't call localStorage.setItem")
        self.assertGreater(catch_idx, -1, "applyView's try has no catch")
        self.assertTrue(
            try_idx < set_idx < catch_idx,
            "localStorage.setItem must sit BETWEEN a try and its catch, "
            "not before the try or after the catch closes")

    def test_falsifier_unguarded_call_would_fail_this_gate(self):
        """Prove the gate actually fires, per this codebase's own
        whitelist-gate convention (test_viz_frozen_header.py does the same
        for its gates) -- a deliberately-unguarded fixture must be REJECTED
        by the identical position check used above."""
        unguarded = (
            "function getInitialView() {\n"
            "    var stored = localStorage.getItem('x');\n"
            "    return stored;\n"
            "  }"
        )
        fn_match = re.search(
            r"function getInitialView\(\)\s*\{(.*?)\n  \}", unguarded, re.DOTALL)
        self.assertIsNotNone(fn_match, "fixture setup itself is broken")
        fn_body = fn_match.group(1)
        try_idx = fn_body.find("try {")
        self.assertEqual(try_idx, -1,
            "the gate above would pass even on real breakage -- this "
            "unguarded fixture must have no try block to find")


class TestSnapshotNowButtonRemoved(_LiveServerBase):
    """engram-alpha#2518, live-score slice: the disabled "Snapshot now"
    button (and its dead ``.snapshot-now-btn`` CSS class) is gone -- Lei's
    direction ("always recompute on page refresh and don't even show a
    'snapshot now' button at all") retires the placeholder this class used
    to pin. Supersedes the old ``TestSnapshotNowButton`` (button-present)
    class, which asserted exactly the shell this slice removes."""

    def test_button_markup_absent(self):
        body = self._get("/health")
        self.assertNotIn('id="snapshot-now-btn"', body)

    def test_dead_css_class_absent(self):
        body = self._get("/health")
        self.assertNotIn(".snapshot-now-btn", body)


class TestExistingDashboardUnchanged(_LiveServerBase):
    def test_dashboard_and_cards_ids_survive(self):
        body = self._get("/health")
        self.assertIn('id="dashboard"', body)
        self.assertIn('id="cards"', body)
        self.assertIn('id="loading"', body)
        self.assertIn("Loading health data...", body)

    def test_dashboard_still_inside_normal_view(self):
        body = self._get("/health")
        normal_start = body.find('id="normal-view"')
        detail_start = body.find('id="detail-view"')
        dashboard_start = body.find('id="dashboard"')
        self.assertNotEqual(normal_start, -1)
        self.assertNotEqual(dashboard_start, -1)
        self.assertLess(normal_start, dashboard_start,
                        "#dashboard should be nested inside #normal-view")
        self.assertLess(dashboard_start, detail_start,
                        "#normal-view (and its #dashboard) should precede "
                        "#detail-view in document order")


if __name__ == "__main__":
    unittest.main()
