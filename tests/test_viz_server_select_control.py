"""Tests for the viz 'select' control type (Surface 8, PR #337)
(engram-alpha#2555 PR A retarget -- was viz_server).

Covers:
1. renderControl-equivalent: annotate_schema marks coder/reviewer_fairy_policy
   rows with control='select' and allowed_values populated.
2. PUT with valid value ("explicit" / "auto" / "always") returns ok=True +
   updates config on disk.
3. PUT with invalid value ("garbage") returns ok=False with allowed_values
   mentioned in the error.
4. HTML config page renders the select-related CSS tokens (smoke test).
5. write_config_key rejects values not in allowed_values for select keys.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# append (not insert(0)) -- `tools.config_schema` below is a qualified
# import that only needs REPO_ROOT (already inserted above) on sys.path;
# inserting tools/ itself at position 0 would needlessly outrank src/forum
# for the rest of the pytest run, shadowing the real `forum` package for
# every test collected afterward (#2037/#2063).
sys.path.append(str(Path(__file__).resolve().parent.parent / "tools"))

from viz import agents as viz_agents
from viz import server as viz_server
from viz import shell as viz_shell
from viz.data import config as viz_config_data
from tools.config_schema import SCHEMA, annotate_schema  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal config for fairy-policy keys
# ---------------------------------------------------------------------------

def _make_config() -> dict:
    """Minimal config.json that includes coder/reviewer_fairy_policy."""
    return {
        "schema_version": 2,
        "trust_pool": ["reuters.com"],
        "primary_user": "test_user",
        "agent_name": "test_agent",
        "mode": "single",
        "counterparts": [],
        "github": {"enabled": False, "primary_user_login": "", "counterparts_logins": []},
        "embedding": {"enabled": True, "model": "all-MiniLM-L6-v2"},
        "cadence": {
            "drowsiness_caution_pct": 80,
            "drowsiness_urgent_pct": 90,
            "auto_sleep_enabled": False,
            "auto_sleep_time": "03:00",
        },
        "memory": {
            "tier1_max_nodes": 200,
            "tier2_max_nodes": 1000,
            "decay_base": 1.014,
        },
        "polarity": {
            "enabled": False,
            "model": "dleemiller/ModernCE-large-nli",
            "threshold": 0.46,
            "min_similarity_for_check": 0.30,
        },
        "yellow_domains": [],
        "confidence_map": {},
        "coder_fairy_policy": "auto",
        "reviewer_fairy_policy": "auto",
    }


class SelectControlSchemaBase(unittest.TestCase):
    """Base setUp/tearDown for write tests."""

    def setUp(self):
        self._saved_agents = viz_agents.AGENTS
        self._saved_backups = dict(viz_config_data._CONFIG_WRITE_BACKUPS)
        viz_config_data._CONFIG_WRITE_BACKUPS.clear()

        self.tmpdir = tempfile.mkdtemp(prefix="engram_select_test_")
        self.engram_home = Path(self.tmpdir) / "agent_dir"
        self.engram_home.mkdir()
        self.db_path = self.engram_home / "knowledge.db"
        self.db_path.write_bytes(b"")
        self.config_path = self.engram_home / "config.json"
        self.config_path.write_text(json.dumps(_make_config(), indent=2))

        viz_agents.AGENTS = {
            "test": {
                "name": "test",
                "label": "Test",
                "db": str(self.db_path),
            }
        }

    def tearDown(self):
        viz_agents.AGENTS = self._saved_agents
        viz_config_data._CONFIG_WRITE_BACKUPS.clear()
        viz_config_data._CONFIG_WRITE_BACKUPS.update(self._saved_backups)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read_config(self) -> dict:
        return json.loads(self.config_path.read_text())


# ---------------------------------------------------------------------------
# 1. annotate_schema marks the rows with control='select' and allowed_values
# ---------------------------------------------------------------------------

class TestSelectControlSchemaAnnotation(unittest.TestCase):
    """annotate_schema correctly marks coder/reviewer_fairy_policy as select."""

    def _annotated_row(self, key: str) -> dict | None:
        """Return the annotated row for the given key, or None if absent."""
        config = _make_config()
        annotated = annotate_schema(config)
        for sec in annotated["sections"]:
            for row in sec["rows"]:
                if row["key"] == key:
                    return row
        return None

    def test_coder_fairy_policy_control_is_select(self):
        row = self._annotated_row("coder_fairy_policy")
        self.assertIsNotNone(row, "coder_fairy_policy not found in annotated schema")
        self.assertEqual(row["control"], "select",
                         f"Expected control='select', got {row['control']!r}")

    def test_reviewer_fairy_policy_control_is_select(self):
        row = self._annotated_row("reviewer_fairy_policy")
        self.assertIsNotNone(row, "reviewer_fairy_policy not found in annotated schema")
        self.assertEqual(row["control"], "select",
                         f"Expected control='select', got {row['control']!r}")

    def test_coder_fairy_policy_allowed_values_populated(self):
        row = self._annotated_row("coder_fairy_policy")
        self.assertIsNotNone(row)
        self.assertEqual(row.get("allowed_values"), ["explicit", "auto", "always"],
                         f"allowed_values mismatch: {row.get('allowed_values')!r}")

    def test_coder_fairy_policy_value_matches_config(self):
        """annotated value must reflect the config value ('auto' from _make_config)."""
        row = self._annotated_row("coder_fairy_policy")
        self.assertIsNotNone(row)
        self.assertEqual(row["value"], "auto",
                         f"annotated value should be 'auto', got {row['value']!r}")


# ---------------------------------------------------------------------------
# 2 + 3. write_config_key allowed-values validation for select keys
# ---------------------------------------------------------------------------

class TestSelectControlWriteValidation(SelectControlSchemaBase):
    """write_config_key validates allowed_values for coder/reviewer_fairy_policy."""

    def test_write_explicit_value_ok(self):
        result = viz_config_data.write_config_key("test", "coder_fairy_policy", "explicit")
        self.assertTrue(result["ok"], f"Expected ok=True for 'explicit': {result}")
        self.assertEqual(result["validated_value"], "explicit")
        cfg = self._read_config()
        self.assertEqual(cfg["coder_fairy_policy"], "explicit")

    def test_write_auto_value_ok(self):
        result = viz_config_data.write_config_key("test", "coder_fairy_policy", "auto")
        self.assertTrue(result["ok"], f"Expected ok=True for 'auto': {result}")
        self.assertEqual(result["validated_value"], "auto")

    def test_write_always_value_ok(self):
        result = viz_config_data.write_config_key("test", "reviewer_fairy_policy", "always")
        self.assertTrue(result["ok"], f"Expected ok=True for 'always': {result}")
        cfg = self._read_config()
        self.assertEqual(cfg["reviewer_fairy_policy"], "always")

    def test_write_invalid_value_rejected(self):
        result = viz_config_data.write_config_key("test", "coder_fairy_policy", "garbage")
        self.assertFalse(result["ok"], "Expected ok=False for 'garbage'")
        self.assertIn("allowed", result["error"].lower(),
                      f"Error should mention 'allowed': {result['error']!r}")

    def test_write_invalid_value_error_names_allowed_values(self):
        """Error message for bad value must name the allowed values."""
        result = viz_config_data.write_config_key("test", "coder_fairy_policy", "never")
        self.assertFalse(result["ok"])
        error = result["error"]
        # The error should mention at least one valid value or "allowed_values"
        self.assertTrue(
            "explicit" in error or "auto" in error or "always" in error or "allowed" in error,
            f"Error should name allowed values: {error!r}"
        )

    def test_reviewer_write_invalid_value_rejected(self):
        result = viz_config_data.write_config_key("test", "reviewer_fairy_policy", "sometimes")
        self.assertFalse(result["ok"], "Expected ok=False for 'sometimes'")
        self.assertIn("allowed", result["error"].lower())


# ---------------------------------------------------------------------------
# 4. HTTP PUT route: valid value returns 200, invalid returns 400
# ---------------------------------------------------------------------------

class TestSelectControlHTTPPut(SelectControlSchemaBase):
    """HTTP PUT /api/config/coder_fairy_policy returns correct status codes."""

    def setUp(self):
        super().setUp()
        self.server = HTTPServer(("127.0.0.1", 0), viz_server.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        super().tearDown()

    def _put(self, key: str, value, agent: str = "test") -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}/api/config/{key}?agent={agent}"
        body = json.dumps({"value": value}).encode()
        req = Request(url, data=body, method="PUT")
        req.add_header("Content-Type", "application/json")
        # same-origin proofs the config page's own fetch sends (#4441)
        req.add_header("X-Engram-Viz", "1")
        req.add_header("Origin", f"http://127.0.0.1:{self.port}")
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except HTTPError as e:
            return e.code, json.loads(e.read())

    def test_put_valid_value_returns_200(self):
        status, body = self._put("coder_fairy_policy", "explicit")
        self.assertEqual(status, 200, f"Expected 200, got {status}: {body}")
        self.assertTrue(body.get("ok"), f"Expected ok=True: {body}")

    def test_put_invalid_value_returns_400(self):
        status, body = self._put("coder_fairy_policy", "garbage")
        self.assertEqual(status, 400, f"Expected 400 for 'garbage', got {status}: {body}")
        self.assertFalse(body.get("ok"), f"Expected ok=False: {body}")
        self.assertIn("error", body)

    def test_put_auto_value_returns_200_and_updates(self):
        # Write 'explicit' first, then write 'auto' back.
        self._put("reviewer_fairy_policy", "always")
        status, body = self._put("reviewer_fairy_policy", "auto")
        self.assertEqual(status, 200, f"Expected 200, got {status}: {body}")
        cfg = self._read_config()
        self.assertEqual(cfg["reviewer_fairy_policy"], "auto")


# ---------------------------------------------------------------------------
# 5. CONFIG_HTML contains select-related CSS (smoke test)
# ---------------------------------------------------------------------------

class TestConfigHtmlSelectCss(unittest.TestCase):
    """CONFIG_HTML must include CSS tokens for the select control."""

    def test_cfg_select_class_in_html(self):
        self.assertIn("cfg-select", viz_shell.CONFIG_HTML,
                      "CONFIG_HTML missing 'cfg-select' CSS class definition")

    def test_saveField_function_in_html(self):
        self.assertIn("saveField", viz_shell.CONFIG_HTML,
                      "CONFIG_HTML missing 'saveField' JS function")

    def test_select_case_in_renderControl(self):
        """CONFIG_HTML must contain the select branch in renderControl."""
        self.assertIn("ctrl === 'select'", viz_shell.CONFIG_HTML,
                      "CONFIG_HTML missing select case in renderControl")


# ---------------------------------------------------------------------------
# 6. Global top-bar agent selector — canonical ID present in all four tabs
# ---------------------------------------------------------------------------

class TestGlobalNavSelectorId(unittest.TestCase):
    """All four tabs must render the same global agent-selector ID.

    PR B (#2555 amendment-1): nav rendering moved from import-time page
    baking to request-time (`shell._render_nav`) -- see shell.py's module
    docstring. The nav div (and therefore this id) is no longer present in
    HTML/HEALTH_HTML/STATS_HTML/CONFIG_HTML at all; those constants now
    carry only the `<!--NAV_PLACEHOLDER-->` sentinel where it used to be
    baked in. These assertions test `_render_nav`'s output directly instead
    -- the actual unit now responsible for this content. See
    TestAllTabsSameNavBar below for the fuller nav-fragment coverage, and
    the live-server gate 1b crawl in test_viz_url_for.py for the
    end-to-end HTTP-level equivalent (nav div byte-identical across all 4
    routes modulo the active-tab bold marker)."""

    def test_global_selector_id_in_graph_view(self):
        self.assertIn('id="agent-select-nav"', viz_shell._render_nav("graph"),
                      "Graph view nav fragment missing id='agent-select-nav'")

    def test_global_selector_id_in_health_dashboard(self):
        self.assertIn('id="agent-select-nav"', viz_shell._render_nav("health"),
                      "Health dashboard nav fragment missing id='agent-select-nav'")

    def test_global_selector_id_in_stats(self):
        self.assertIn('id="agent-select-nav"', viz_shell._render_nav("stats"),
                      "Stats nav fragment missing id='agent-select-nav'")

    def test_global_selector_id_in_config(self):
        self.assertIn('id="agent-select-nav"', viz_shell._render_nav("config"),
                      "Config nav fragment missing id='agent-select-nav'")

    def test_old_sidebar_selector_absent_from_graph_view(self):
        """The old sidebar #agent-select must be gone; only the nav selector remains."""
        # The old ID was 'agent-select' (no suffix).  The nav selector uses
        # 'agent-select-nav'.  There should be no element with the bare old ID.
        import re
        old_bare = re.compile(r'id=["\']agent-select["\']')
        self.assertIsNone(old_bare.search(viz_shell.HTML),
                          "Graph view still contains old bare id='agent-select' "
                          "(sidebar selector not removed)")

    def test_old_health_selector_absent(self):
        self.assertNotIn('id="agent-select-health"', viz_shell.HEALTH_HTML,
                         "Health dashboard still contains old id='agent-select-health'")

    def test_old_config_selector_absent(self):
        self.assertNotIn('id="agent-select-config"', viz_shell.CONFIG_HTML,
                         "Config page still contains old id='agent-select-config'")


# ---------------------------------------------------------------------------
# 7. All four tab pages render the same nav-bar fragment
# ---------------------------------------------------------------------------

class TestAllTabsSameNavBar(unittest.TestCase):
    """Every tab's rendered nav fragment must contain the shared nav elements.

    PR B (#2555 amendment-1): nav rendering moved to request time
    (`shell._render_nav` -- see shell.py's module docstring), so `_PAGES`
    here holds each tab's rendered nav FRAGMENT rather than the full,
    pre-baked page HTML constant (which, post-PR-B, contains only the
    `<!--NAV_PLACEHOLDER-->` sentinel where the nav div used to be baked
    in). The assertion bodies below are otherwise UNCHANGED from PR A --
    they still test exactly the same nav-div content, just sourced from
    the function that now produces it instead of a pre-baked constant.
    """

    _PAGES = {
        "graph":  viz_shell._render_nav("graph"),
        "health": viz_shell._render_nav("health"),
        "stats":  viz_shell._render_nav("stats"),
        "config": viz_shell._render_nav("config"),
    }

    def test_all_tabs_have_nav_div(self):
        for tab, html in self._PAGES.items():
            self.assertIn('<div class="nav">', html,
                          f"{tab} page missing nav div")

    def test_exactly_one_nav_css_rule_opener_in_viz(self):
        """Gate 1b (forum #389, strong form): the .nav CSS rule opener
        (the CSS RULE, not the class name in markup -- e.g. NOT
        'class="nav"') must appear exactly ONCE across src/engram/viz/,
        in static/base.css. Scoped to viz/ specifically, not repo-wide --
        a repo-wide grep would false-positive on src/forum/templates/'s own
        unrelated .nav rule (Kepler's own scope correction, forum #389).

        A leftover page-local .nav rule loses to the cascade (base.css's
        <link> comes first in every template) and renders invisibly
        correct while still being dead, wrong CSS -- markup-parity tests
        like test_all_tabs_have_nav_div above would NOT catch that; only a
        rule-opener grep does.
        """
        import re

        viz_dir = Path(__file__).resolve().parent.parent / "src" / "engram" / "viz"
        pattern = re.compile(r"\.nav\s*\{")
        hits = []
        for path in viz_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in (".css", ".html"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for m in pattern.finditer(text):
                hits.append(f"{path.relative_to(viz_dir)}:{m.start()}")
        self.assertEqual(
            len(hits), 1,
            f"expected exactly one '.nav {{' CSS-rule-opener hit, found "
            f"{len(hits)}: {hits!r}",
        )
        self.assertTrue(
            hits[0].startswith(f"static{os.sep}base.css:"),
            f"the one '.nav {{' hit must be in static/base.css, found: {hits[0]!r}",
        )

    def test_all_tabs_have_graph_view_link(self):
        for tab, html in self._PAGES.items():
            self.assertIn('href="/"', html,
                          f"{tab} page missing graph view link")

    def test_all_tabs_have_health_link(self):
        for tab, html in self._PAGES.items():
            self.assertIn('href="/health"', html,
                          f"{tab} page missing health link")

    def test_all_tabs_have_stats_link(self):
        for tab, html in self._PAGES.items():
            self.assertIn('href="/stats"', html,
                          f"{tab} page missing stats link")

    def test_all_tabs_have_config_link(self):
        for tab, html in self._PAGES.items():
            self.assertIn('href="/config"', html,
                          f"{tab} page missing config link")

    def test_each_tab_has_active_link_bolded(self):
        """Each page must bold exactly its own tab link."""
        active_map = {
            "graph":  'href="/" style="font-weight:600;"',
            "health": 'href="/health" style="font-weight:600;"',
            "stats":  'href="/stats" style="font-weight:600;"',
            "config": 'href="/config" style="font-weight:600;"',
        }
        for tab, fragment in active_map.items():
            self.assertIn(fragment, self._PAGES[tab],
                          f"{tab} page active link not bolded")

    def test_nav_agent_wrapper_present_in_all_tabs(self):
        for tab, html in self._PAGES.items():
            self.assertIn('id="nav-agent-wrapper"', html,
                          f"{tab} page missing nav-agent-wrapper span")


# ---------------------------------------------------------------------------
# 8. Shared nav JS: now a real static asset, referenced (not inlined); the
#    old client-side tab-link rewrite is gone (superseded by server-side
#    url_for()) -- Test class renamed from TestNavScriptTabLinkRewrite,
#    which tested exactly that now-deleted logic, per the PR's regression-
#    test discipline: assert the NEW behavior with an explanatory comment,
#    don't just drop the coverage silently.
#    Removed-test: tests/test_viz_server_select_control.py::TestNavScriptTabLinkRewrite::test_nav_script_rewrite_logic_in_all_pages — tested the client-side querySelectorAll('.nav a') tab-href rewrite, which PR B deletes from nav.js (superseded by server-side url_for()); replaced by test_tab_link_rewrite_removed_from_nav_script below (a positive check that it's gone).
#    Removed-test: tests/test_viz_server_select_control.py::TestNavScriptTabLinkRewrite::test_nav_script_agent_param_in_all_pages — asserted 'agent-select-nav' was present INLINE in each page constant (true when nav.js was inlined per-page); PR B moves nav.js to /static/nav.js referenced via <script src>, so the string is no longer inline. Replaced by test_pages_reference_static_nav_script (the <script src> reference) + test_nav_script_agent_param_still_present (asserts it directly against _NAV_SCRIPT, unchanged).
#    Removed-test: tests/test_viz_server_select_control.py::TestNavScriptTabLinkRewrite::test_nav_script_onchange_full_reload_in_all_pages — same inline-vs-referenced issue as above; replaced by test_nav_script_onchange_full_reload_present (asserts against _NAV_SCRIPT directly).
# ---------------------------------------------------------------------------

class TestNavScriptStaticReference(unittest.TestCase):
    """PR B (#2555 amendment-1): the shared nav script is now served from
    /static/nav.js and referenced via <script src>, not inlined per-page.
    Server-side url_for() (see viz.urls, called from shell._render_nav) now
    carries ?agent= through nav links, which makes the OLD client-side
    querySelectorAll('.nav a') tab-href rewrite in nav.js dead code -- so
    it was deleted from nav.js itself, not merely left unused."""

    # graph.html deliberately does NOT reference /static/nav.js -- it has
    # its own near-duplicate agent-selector script (graph-agent-select.js,
    # asserted separately below), a pre-existing PR-A design choice this PR
    # doesn't change.
    _NAV_JS_PAGES = {
        "health": viz_shell.HEALTH_HTML,
        "stats":  viz_shell.STATS_HTML,
        "config": viz_shell.CONFIG_HTML,
    }

    def test_pages_reference_static_nav_script(self):
        for tab, html in self._NAV_JS_PAGES.items():
            self.assertIn('<script src="/static/nav.js"></script>', html,
                          f"{tab} page missing /static/nav.js script reference")

    def test_graph_page_references_static_graph_agent_select_script(self):
        self.assertIn('<script src="/static/graph-agent-select.js"></script>',
                      viz_shell.HTML,
                      "graph page missing /static/graph-agent-select.js "
                      "script reference")

    def test_tab_link_rewrite_removed_from_nav_script(self):
        """Confirm the client-side rewrite is actually gone, not just unused
        -- catches reintroduction of now-redundant dead code."""
        self.assertNotIn(
            "querySelectorAll('.nav a')", viz_shell._NAV_SCRIPT,
            "_NAV_SCRIPT still contains the now-redundant client-side "
            "tab-href rewrite (url_for() carries ?agent= through "
            "server-side now -- see viz.urls / shell._render_nav)")

    def test_nav_script_agent_param_still_present(self):
        """The agent-selector population logic itself is UNCHANGED -- only
        the tab-href-rewrite sub-block was removed."""
        self.assertIn("agent-select-nav", viz_shell._NAV_SCRIPT,
                      "_NAV_SCRIPT missing agent-select-nav reference")

    def test_nav_script_onchange_full_reload_present(self):
        self.assertIn("window.location.href", viz_shell._NAV_SCRIPT,
                      "_NAV_SCRIPT missing window.location.href navigation")

    def test_tab_link_rewrite_removed_from_graph_agent_select_script(self):
        """graph-agent-select.js is nav.js's near-duplicate for the graph
        page (a pre-existing PR-A design choice); it carried the same now-
        redundant tab-href rewrite and gets the same removal."""
        content = (viz_shell._STATIC_DIR / "graph-agent-select.js").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(
            "querySelectorAll('.nav a')", content,
            "graph-agent-select.js still contains the now-redundant "
            "client-side tab-href rewrite")


# ---------------------------------------------------------------------------
# 9. Single-agent mode: selector hidden
# ---------------------------------------------------------------------------

class TestSingleAgentModeSelectorHidden(unittest.TestCase):
    """When /api/agents returns one agent, the selector must stay hidden.
    The shared nav script guards: agents.length <= 1 → return early."""

    def test_single_agent_guard_in_nav_script(self):
        """The shared nav script must include the single-agent guard."""
        # Check the script constant directly.
        self.assertIn("agents.length <= 1", viz_shell._NAV_SCRIPT,
                      "_NAV_SCRIPT missing single-agent guard (agents.length <= 1)")

    def test_nav_agent_wrapper_initially_hidden(self):
        """nav-agent-wrapper must start hidden (display:none) in every page.

        PR B (#2555 amendment-1): the wrapper ELEMENT is now spliced in at
        request time (shell._render_nav), and its display:none hiding rule
        lives in static/base.css (linked via <link rel="stylesheet"
        href="/static/base.css"> on every page -- for graph.html
        specifically that's the ONLY place the rule lives now; health/stats/
        config's own inline <style> blocks still duplicate the same
        #nav-agent-wrapper rule too, untouched by this PR). Assemble each
        page the same way server.py does (splice the placeholder) so this
        test exercises the real end-to-end result rather than the raw,
        pre-splice shell constant.
        """
        pages = {
            "graph":  viz_shell.HTML,
            "health": viz_shell.HEALTH_HTML,
            "stats":  viz_shell.STATS_HTML,
            "config": viz_shell.CONFIG_HTML,
        }
        for tab, shell_html in pages.items():
            assembled = shell_html.replace(
                viz_shell._NAV_PLACEHOLDER, viz_shell._render_nav(tab)
            )
            self.assertIn("nav-agent-wrapper", assembled,
                          f"{tab} page missing nav-agent-wrapper CSS/element")

    def test_wrapper_only_shown_in_multi_agent_mode(self):
        """The nav script must only show the wrapper when agents.length > 1."""
        # The early-return guard means the wrapper.style.display = 'inline-flex'
        # line is only reached after the agents.length <= 1 check.
        script = viz_shell._NAV_SCRIPT
        guard_pos = script.find("agents.length <= 1")
        show_pos = script.find("wrapper.style.display = 'inline-flex'")
        self.assertGreater(show_pos, guard_pos,
                           "wrapper.style.display = 'inline-flex' must come after "
                           "single-agent guard")

    def test_wrapper_show_uses_explicit_display_not_empty_reset(self):
        """Regression guard: the show-path must set an explicit display value.

        Setting wrapper.style.display = '' only removes the inline override and
        lets the CSS cascade apply — which restores display:none from the
        shared nav CSS's #nav-agent-wrapper rule (served from
        static/base.css and linked via <link rel="stylesheet"
        href="/static/base.css"> on every page as of #2555 PR B; previously
        baked in per-page via a shell.py constant named _NAV_SHARED_CSS,
        which no longer exists). The correct fix is an explicit non-empty
        value (e.g. 'inline-flex') so the wrapper actually becomes visible.

        This test catches reintroduction of the silent-hidden bug (PR #425 r3).
        """
        script = viz_shell._NAV_SCRIPT
        # Must NOT contain the empty-string reset pattern.
        self.assertNotIn(
            "wrapper.style.display = ''",
            script,
            "_NAV_SCRIPT contains wrapper.style.display = '' which silently "
            "keeps the wrapper hidden (CSS display:none wins over empty reset). "
            "Use an explicit value such as 'inline-flex'.",
        )
        # Must contain an explicit display value in the show-path.
        self.assertIn(
            "wrapper.style.display = 'inline-flex'",
            script,
            "_NAV_SCRIPT missing explicit inline-flex display assignment "
            "for the multi-agent show-path.",
        )
