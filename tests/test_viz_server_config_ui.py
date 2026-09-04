"""Tests for viz.shell/viz.server config editable UI (PR-3) (engram-alpha#2555
PR A retarget -- was viz_server).

Structural / server-side checks that don't require a browser:
1.  CONFIG_HTML contains editable input controls (not just disabled ones).
2.  CONFIG_HTML contains the PUT-fetch JS wiring (saveField, saveFieldFromInput).
3.  CONFIG_HTML contains the restart-required banner element.
4.  CONFIG_HTML contains depends_on JS logic (updateDependsOn).
5.  CONFIG_HTML contains the save button element and css class.
6.  Auto-sleep keys render as editable in the annotated schema.
7.  depends_on attribute propagates into annotated rows for cadence.auto_sleep_time.
8.  Read-only fields still render as disabled in the HTML template.
9.  GET /api/config returns annotated data including auto-sleep keys.
10. Tier-B + restart_required fields carry a soft-warning marker in the HTML.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import shutil
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viz import agents as viz_agents
from viz import server as viz_server
from viz import shell as viz_shell
from tools.config_schema import SCHEMA, annotate_schema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config() -> dict:
    """Minimal config.json with all schema-known keys populated."""
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
    }


# ---------------------------------------------------------------------------
# CONFIG_HTML structural checks
# ---------------------------------------------------------------------------

class TestConfigHtmlStructure(unittest.TestCase):
    """CONFIG_HTML must contain the editable UI elements required by the spec."""

    html = viz_shell.CONFIG_HTML

    def test_restart_banner_element_present(self):
        """The restart-required banner element must exist in the HTML."""
        self.assertIn('id="restart-banner"', self.html,
                      "restart-banner element missing from CONFIG_HTML")

    def test_save_button_css_class_present(self):
        """The .btn-save CSS class must be defined."""
        self.assertIn('.btn-save', self.html,
                      ".btn-save CSS class missing from CONFIG_HTML")

    def test_readonly_badge_present(self):
        """Non-editable fields get a visible read-only badge (#1172 finding —
        agent_name/mode/counterparts were ambiguous: disabled but unmarked)."""
        self.assertIn('badge-readonly', self.html,
                      "badge-readonly CSS class missing from CONFIG_HTML")
        self.assertIn('read-only</span>', self.html,
                      "read-only badge markup missing from CONFIG_HTML")

    def test_default_placeholder_logic_present(self):
        """Editable numeric fields surface the effective default as a placeholder
        when unset (#1172 finding — engaged-window rendered as a blank box)."""
        self.assertIn('row.default', self.html,
                      "default-placeholder logic missing from CONFIG_HTML")
        self.assertIn('(default)"', self.html,
                      "'(default)' placeholder suffix missing from CONFIG_HTML")

    def test_save_field_js_function_present(self):
        """saveField() JS function must be defined."""
        self.assertIn('async function saveField(', self.html,
                      "saveField() function missing from CONFIG_HTML")

    def test_save_field_from_input_js_present(self):
        """saveFieldFromInput() JS function must be defined."""
        self.assertIn('function saveFieldFromInput(', self.html,
                      "saveFieldFromInput() function missing from CONFIG_HTML")

    def test_put_fetch_wiring_present(self):
        """The JS must call fetch with PUT method to /api/config."""
        self.assertIn("method: 'PUT'", self.html,
                      "PUT fetch call missing from CONFIG_HTML")
        self.assertIn("'/api/config/'", self.html,
                      "/api/config/ path missing from CONFIG_HTML")

    def test_depends_on_update_function_present(self):
        """updateDependsOn() function must be defined for gate-field toggling."""
        self.assertIn('function updateDependsOn(', self.html,
                      "updateDependsOn() function missing from CONFIG_HTML")

    def test_depends_on_data_attribute_used(self):
        """data-depends-on attribute must be referenced in renderRow JS."""
        self.assertIn('data-depends-on', self.html,
                      "data-depends-on attribute not referenced in CONFIG_HTML")

    def test_editable_checkbox_onchange_present(self):
        """Editable checkboxes must call saveField in their onchange handler."""
        self.assertIn("saveField(", self.html,
                      "saveField() call missing from checkbox onchange in CONFIG_HTML")

    def test_checkbox_onchange_calls_update_depends_on_unconditionally(self):
        """Checkbox onchange must call updateDependsOn() before saveField so
        gated rows (e.g. auto_sleep_time) show/hide even when PUT fails."""
        # The onchange attribute should fire updateDependsOn() first, then saveField.
        self.assertIn("updateDependsOn(); saveField(", self.html,
                      "Checkbox onchange must call updateDependsOn() unconditionally "
                      "before saveField() — gate visibility should not wait for PUT success")

    def test_save_field_from_input_nan_guard(self):
        """saveFieldFromInput must guard against NaN for number inputs."""
        self.assertIn("Number.isNaN(", self.html,
                      "NaN guard (Number.isNaN) missing from saveFieldFromInput in CONFIG_HTML")

    def test_save_field_from_input_type_coercion(self):
        """saveFieldFromInput must coerce number-type inputs with Number()."""
        self.assertIn("Number(inputEl.value)", self.html,
                      "Numeric coercion (Number(inputEl.value)) missing from saveFieldFromInput")

    def test_field_status_css_class_present(self):
        """The .field-status CSS class must be defined for inline feedback."""
        self.assertIn('.field-status', self.html,
                      ".field-status CSS class missing from CONFIG_HTML")

    def test_warn_critical_css_class_present(self):
        """The .warn-critical CSS class must be defined for system-critical keys."""
        self.assertIn('.warn-critical', self.html,
                      ".warn-critical CSS class missing from CONFIG_HTML")

    def test_disabled_keyword_still_present_for_readonly(self):
        """Read-only fields must still be rendered as disabled in the renderControl JS."""
        # renderControl uses `disabled` for editable=false branches
        self.assertIn('disabled', self.html,
                      "disabled keyword not found — read-only rendering may have broken")

    def test_update_depends_on_called_after_render(self):
        """updateDependsOn() must be called after body.innerHTML = html."""
        # Both must appear in the HTML; the call after innerHTML assignment confirms wiring
        self.assertIn('updateDependsOn()', self.html,
                      "updateDependsOn() call missing from CONFIG_HTML")


# ---------------------------------------------------------------------------
# Schema annotation checks: auto-sleep keys must be editable
# ---------------------------------------------------------------------------

class TestAutoSleepAnnotation(unittest.TestCase):
    """The annotated schema for auto-sleep keys must mark them editable."""

    def _annotated_rows_by_key(self) -> dict:
        cfg = _make_config()
        annotated = annotate_schema(cfg)
        rows = {}
        for sec in annotated["sections"]:
            for row in sec["rows"]:
                rows[row["key"]] = row
        return rows

    def test_auto_sleep_enabled_editable_in_annotated(self):
        rows = self._annotated_rows_by_key()
        self.assertIn("cadence.auto_sleep_enabled", rows)
        row = rows["cadence.auto_sleep_enabled"]
        self.assertNotEqual(row.get("editable"), False,
                            "cadence.auto_sleep_enabled must be editable")

    def test_auto_sleep_time_editable_in_annotated(self):
        rows = self._annotated_rows_by_key()
        self.assertIn("cadence.auto_sleep_time", rows)
        row = rows["cadence.auto_sleep_time"]
        self.assertNotEqual(row.get("editable"), False,
                            "cadence.auto_sleep_time must be editable")

    def test_auto_sleep_time_depends_on_in_annotated(self):
        """cadence.auto_sleep_time must carry depends_on='cadence.auto_sleep_enabled'."""
        rows = self._annotated_rows_by_key()
        row = rows.get("cadence.auto_sleep_time", {})
        self.assertEqual(row.get("depends_on"), "cadence.auto_sleep_enabled",
                         "cadence.auto_sleep_time missing depends_on='cadence.auto_sleep_enabled'")

    def test_auto_sleep_enabled_no_depends_on(self):
        """cadence.auto_sleep_enabled must NOT have depends_on (it is the gate)."""
        rows = self._annotated_rows_by_key()
        row = rows.get("cadence.auto_sleep_enabled", {})
        self.assertNotIn("depends_on", row,
                         "cadence.auto_sleep_enabled should not have depends_on set")

    def test_auto_sleep_time_value_present_in_annotated(self):
        """The current config value for auto_sleep_time must appear in annotation."""
        rows = self._annotated_rows_by_key()
        row = rows["cadence.auto_sleep_time"]
        self.assertEqual(row["value"], "03:00",
                         "cadence.auto_sleep_time value not correctly read from config")


# ---------------------------------------------------------------------------
# Schema annotation: read-only fields must have editable=False
# ---------------------------------------------------------------------------

class TestReadOnlyAnnotation(unittest.TestCase):
    """Read-only SCHEMA entries must survive into annotated output."""

    def _annotated_rows_by_key(self) -> dict:
        cfg = _make_config()
        annotated = annotate_schema(cfg)
        rows = {}
        for sec in annotated["sections"]:
            for row in sec["rows"]:
                rows[row["key"]] = row
        return rows

    def test_agent_name_is_readonly(self):
        rows = self._annotated_rows_by_key()
        self.assertEqual(rows["agent_name"]["editable"], False)

    def test_embedding_enabled_is_readonly(self):
        rows = self._annotated_rows_by_key()
        self.assertEqual(rows["embedding.enabled"]["editable"], False)

    def test_embedding_model_is_readonly(self):
        rows = self._annotated_rows_by_key()
        self.assertEqual(rows["embedding.model"]["editable"], False)


# ---------------------------------------------------------------------------
# GET /api/config HTTP integration: auto-sleep keys in annotated response
# ---------------------------------------------------------------------------

class TestGetConfigAutoSleep(unittest.TestCase):
    """GET /api/config returns annotated data with auto-sleep keys editable."""

    def setUp(self):
        self._saved_agents = viz_agents.AGENTS

        self.tmpdir = tempfile.mkdtemp(prefix="engram_cfg_ui_test_")
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

        self._server = HTTPServer(("127.0.0.1", 0), viz_server.Handler)
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self):
        self._server.shutdown()
        viz_agents.AGENTS = self._saved_agents
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _get_config(self, agent: str = "test") -> dict:
        url = f"http://127.0.0.1:{self._port}/api/config?agent={agent}"
        resp = urlopen(url)
        return json.loads(resp.read())

    def _annotated_rows_by_key(self) -> dict:
        data = self._get_config()
        rows = {}
        for sec in data["annotated"]["sections"]:
            for row in sec["rows"]:
                rows[row["key"]] = row
        return rows

    def test_get_config_returns_annotated(self):
        data = self._get_config()
        self.assertIn("annotated", data)
        self.assertIn("sections", data["annotated"])

    def test_auto_sleep_enabled_in_response(self):
        rows = self._annotated_rows_by_key()
        self.assertIn("cadence.auto_sleep_enabled", rows)

    def test_auto_sleep_time_in_response(self):
        rows = self._annotated_rows_by_key()
        self.assertIn("cadence.auto_sleep_time", rows)

    def test_auto_sleep_enabled_editable_in_response(self):
        rows = self._annotated_rows_by_key()
        row = rows["cadence.auto_sleep_enabled"]
        self.assertNotEqual(row.get("editable"), False)

    def test_auto_sleep_time_editable_in_response(self):
        rows = self._annotated_rows_by_key()
        row = rows["cadence.auto_sleep_time"]
        self.assertNotEqual(row.get("editable"), False)

    def test_auto_sleep_time_depends_on_in_response(self):
        rows = self._annotated_rows_by_key()
        row = rows["cadence.auto_sleep_time"]
        self.assertEqual(row.get("depends_on"), "cadence.auto_sleep_enabled")


if __name__ == "__main__":
    unittest.main()
