"""Tests for the viz_server 'select' control type (Surface 8, PR #337).

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

import viz_server
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
        self._saved_agents = viz_server.AGENTS
        self._saved_backups = dict(viz_server._CONFIG_WRITE_BACKUPS)
        viz_server._CONFIG_WRITE_BACKUPS.clear()

        self.tmpdir = tempfile.mkdtemp(prefix="engram_select_test_")
        self.engram_home = Path(self.tmpdir) / "agent_dir"
        self.engram_home.mkdir()
        self.db_path = self.engram_home / "knowledge.db"
        self.db_path.write_bytes(b"")
        self.config_path = self.engram_home / "config.json"
        self.config_path.write_text(json.dumps(_make_config(), indent=2))

        viz_server.AGENTS = {
            "test": {
                "name": "test",
                "label": "Test",
                "db": str(self.db_path),
            }
        }

    def tearDown(self):
        viz_server.AGENTS = self._saved_agents
        viz_server._CONFIG_WRITE_BACKUPS.clear()
        viz_server._CONFIG_WRITE_BACKUPS.update(self._saved_backups)
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
        result = viz_server.write_config_key("test", "coder_fairy_policy", "explicit")
        self.assertTrue(result["ok"], f"Expected ok=True for 'explicit': {result}")
        self.assertEqual(result["validated_value"], "explicit")
        cfg = self._read_config()
        self.assertEqual(cfg["coder_fairy_policy"], "explicit")

    def test_write_auto_value_ok(self):
        result = viz_server.write_config_key("test", "coder_fairy_policy", "auto")
        self.assertTrue(result["ok"], f"Expected ok=True for 'auto': {result}")
        self.assertEqual(result["validated_value"], "auto")

    def test_write_always_value_ok(self):
        result = viz_server.write_config_key("test", "reviewer_fairy_policy", "always")
        self.assertTrue(result["ok"], f"Expected ok=True for 'always': {result}")
        cfg = self._read_config()
        self.assertEqual(cfg["reviewer_fairy_policy"], "always")

    def test_write_invalid_value_rejected(self):
        result = viz_server.write_config_key("test", "coder_fairy_policy", "garbage")
        self.assertFalse(result["ok"], "Expected ok=False for 'garbage'")
        self.assertIn("allowed", result["error"].lower(),
                      f"Error should mention 'allowed': {result['error']!r}")

    def test_write_invalid_value_error_names_allowed_values(self):
        """Error message for bad value must name the allowed values."""
        result = viz_server.write_config_key("test", "coder_fairy_policy", "never")
        self.assertFalse(result["ok"])
        error = result["error"]
        # The error should mention at least one valid value or "allowed_values"
        self.assertTrue(
            "explicit" in error or "auto" in error or "always" in error or "allowed" in error,
            f"Error should name allowed values: {error!r}"
        )

    def test_reviewer_write_invalid_value_rejected(self):
        result = viz_server.write_config_key("test", "reviewer_fairy_policy", "sometimes")
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
        self.assertIn("cfg-select", viz_server.CONFIG_HTML,
                      "CONFIG_HTML missing 'cfg-select' CSS class definition")

    def test_saveField_function_in_html(self):
        self.assertIn("saveField", viz_server.CONFIG_HTML,
                      "CONFIG_HTML missing 'saveField' JS function")

    def test_select_case_in_renderControl(self):
        """CONFIG_HTML must contain the select branch in renderControl."""
        self.assertIn("ctrl === 'select'", viz_server.CONFIG_HTML,
                      "CONFIG_HTML missing select case in renderControl")


# ---------------------------------------------------------------------------
# 6. Global top-bar agent selector — canonical ID present in all four tabs
# ---------------------------------------------------------------------------

class TestGlobalNavSelectorId(unittest.TestCase):
    """All four tab pages must render the same global agent-selector ID."""

    _PAGES = [
        ("HTML",        viz_server.HTML),
        ("HEALTH_HTML", viz_server.HEALTH_HTML),
        ("STATS_HTML",  viz_server.STATS_HTML),
        ("CONFIG_HTML", viz_server.CONFIG_HTML),
    ]

    def test_global_selector_id_in_graph_view(self):
        self.assertIn('id="agent-select-nav"', viz_server.HTML,
                      "Graph view missing global selector id='agent-select-nav'")

    def test_global_selector_id_in_health_dashboard(self):
        self.assertIn('id="agent-select-nav"', viz_server.HEALTH_HTML,
                      "Health dashboard missing global selector id='agent-select-nav'")

    def test_global_selector_id_in_stats(self):
        self.assertIn('id="agent-select-nav"', viz_server.STATS_HTML,
                      "Stats page missing global selector id='agent-select-nav'")

    def test_global_selector_id_in_config(self):
        self.assertIn('id="agent-select-nav"', viz_server.CONFIG_HTML,
                      "Config page missing global selector id='agent-select-nav'")

    def test_old_sidebar_selector_absent_from_graph_view(self):
        """The old sidebar #agent-select must be gone; only the nav selector remains."""
        # The old ID was 'agent-select' (no suffix).  The nav selector uses
        # 'agent-select-nav'.  There should be no element with the bare old ID.
        import re
        old_bare = re.compile(r'id=["\']agent-select["\']')
        self.assertIsNone(old_bare.search(viz_server.HTML),
                          "Graph view still contains old bare id='agent-select' "
                          "(sidebar selector not removed)")

    def test_old_health_selector_absent(self):
        self.assertNotIn('id="agent-select-health"', viz_server.HEALTH_HTML,
                         "Health dashboard still contains old id='agent-select-health'")

    def test_old_config_selector_absent(self):
        self.assertNotIn('id="agent-select-config"', viz_server.CONFIG_HTML,
                         "Config page still contains old id='agent-select-config'")


# ---------------------------------------------------------------------------
# 7. All four tab pages render the same nav-bar fragment
# ---------------------------------------------------------------------------

class TestAllTabsSameNavBar(unittest.TestCase):
    """Every tab page must contain the shared nav elements."""

    _PAGES = {
        "graph":  viz_server.HTML,
        "health": viz_server.HEALTH_HTML,
        "stats":  viz_server.STATS_HTML,
        "config": viz_server.CONFIG_HTML,
    }

    def test_all_tabs_have_nav_div(self):
        for tab, html in self._PAGES.items():
            self.assertIn('<div class="nav">', html,
                          f"{tab} page missing nav div")

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
# 8. Shared nav JS: tab link rewrite logic present in all pages
# ---------------------------------------------------------------------------

class TestNavScriptTabLinkRewrite(unittest.TestCase):
    """The shared nav script that rewrites tab links to preserve ?agent= must
    be injected into every page."""

    _PAGES = {
        "graph":  viz_server.HTML,
        "health": viz_server.HEALTH_HTML,
        "stats":  viz_server.STATS_HTML,
        "config": viz_server.CONFIG_HTML,
    }

    def test_nav_script_rewrite_logic_in_all_pages(self):
        """querySelectorAll('.nav a') rewrite must appear in every page."""
        for tab, html in self._PAGES.items():
            self.assertIn("querySelectorAll('.nav a')", html,
                          f"{tab} page missing tab-link rewrite JS")

    def test_nav_script_agent_param_in_all_pages(self):
        """The nav script must use 'agent-select-nav' selector in every page."""
        for tab, html in self._PAGES.items():
            self.assertIn("agent-select-nav", html,
                          f"{tab} page missing agent-select-nav reference in script")

    def test_nav_script_onchange_full_reload_in_all_pages(self):
        """Selector onchange must trigger a full URL navigation (not in-place refresh)."""
        for tab, html in self._PAGES.items():
            self.assertIn("window.location.href", html,
                          f"{tab} page nav script missing window.location.href navigation")


# ---------------------------------------------------------------------------
# 9. Single-agent mode: selector hidden
# ---------------------------------------------------------------------------

class TestSingleAgentModeSelectorHidden(unittest.TestCase):
    """When /api/agents returns one agent, the selector must stay hidden.
    The shared nav script guards: agents.length <= 1 → return early."""

    def test_single_agent_guard_in_nav_script(self):
        """The shared nav script must include the single-agent guard."""
        # Check the script constant directly.
        self.assertIn("agents.length <= 1", viz_server._NAV_SCRIPT,
                      "_NAV_SCRIPT missing single-agent guard (agents.length <= 1)")

    def test_nav_agent_wrapper_initially_hidden(self):
        """nav-agent-wrapper must start hidden (display:none) in every page."""
        for tab, html in [
            ("graph",  viz_server.HTML),
            ("health", viz_server.HEALTH_HTML),
            ("stats",  viz_server.STATS_HTML),
            ("config", viz_server.CONFIG_HTML),
        ]:
            # The wrapper is hidden via CSS; either inline display:none or a
            # CSS rule.  We check for the CSS rule in the page.
            self.assertIn("nav-agent-wrapper", html,
                          f"{tab} page missing nav-agent-wrapper CSS/element")

    def test_wrapper_only_shown_in_multi_agent_mode(self):
        """The nav script must only show the wrapper when agents.length > 1."""
        # The early-return guard means the wrapper.style.display = 'inline-flex'
        # line is only reached after the agents.length <= 1 check.
        script = viz_server._NAV_SCRIPT
        guard_pos = script.find("agents.length <= 1")
        show_pos = script.find("wrapper.style.display = 'inline-flex'")
        self.assertGreater(show_pos, guard_pos,
                           "wrapper.style.display = 'inline-flex' must come after "
                           "single-agent guard")

    def test_wrapper_show_uses_explicit_display_not_empty_reset(self):
        """Regression guard: the show-path must set an explicit display value.

        Setting wrapper.style.display = '' only removes the inline override and
        lets the CSS cascade apply — which restores display:none from the
        _NAV_SHARED_CSS rule.  The correct fix is an explicit non-empty value
        (e.g. 'inline-flex') so the wrapper actually becomes visible.

        This test catches reintroduction of the silent-hidden bug (PR #425 r3).
        """
        script = viz_server._NAV_SCRIPT
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
