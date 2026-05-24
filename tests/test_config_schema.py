"""Tests for tools/config_schema.py SCHEMA and _default_config cadence block.

Regression guard for the auto-sleep config fields added in the auto-sleep
config schema PR. Both auto_sleep_enabled and auto_sleep_time are consumed
by the SessionStart hook at session start — their restart_required=True
semantics are load-bearing (a change takes effect next session, not mid-session).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# config_schema.py lives in tools/, not at repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

from tools.config_schema import SCHEMA  # noqa: E402


def _schema_by_key() -> dict[str, dict]:
    """Return SCHEMA indexed by key for O(1) lookup."""
    return {row["key"]: row for row in SCHEMA}


class TestAutoSleepSchemaEntries:
    """The two new cadence.auto_sleep_* keys must be present with correct metadata."""

    def test_auto_sleep_enabled_in_schema(self):
        """cadence.auto_sleep_enabled must be present in SCHEMA."""
        by_key = _schema_by_key()
        assert "cadence.auto_sleep_enabled" in by_key, (
            "cadence.auto_sleep_enabled missing from SCHEMA"
        )

    def test_auto_sleep_time_in_schema(self):
        """cadence.auto_sleep_time must be present in SCHEMA."""
        by_key = _schema_by_key()
        assert "cadence.auto_sleep_time" in by_key, (
            "cadence.auto_sleep_time missing from SCHEMA"
        )

    def test_auto_sleep_enabled_restart_required(self):
        """cadence.auto_sleep_enabled must have restart_required=True (SessionStart hook reads it)."""
        by_key = _schema_by_key()
        row = by_key["cadence.auto_sleep_enabled"]
        assert row.get("restart_required") is True, (
            f"cadence.auto_sleep_enabled restart_required={row.get('restart_required')!r}; expected True"
        )

    def test_auto_sleep_time_restart_required(self):
        """cadence.auto_sleep_time must have restart_required=True (SessionStart hook reads it)."""
        by_key = _schema_by_key()
        row = by_key["cadence.auto_sleep_time"]
        assert row.get("restart_required") is True, (
            f"cadence.auto_sleep_time restart_required={row.get('restart_required')!r}; expected True"
        )

    def test_auto_sleep_enabled_is_boolean_type(self):
        """cadence.auto_sleep_enabled type must be 'boolean'."""
        by_key = _schema_by_key()
        row = by_key["cadence.auto_sleep_enabled"]
        assert row.get("type") == "boolean", (
            f"cadence.auto_sleep_enabled type={row.get('type')!r}; expected 'boolean'"
        )

    def test_auto_sleep_time_is_string_type(self):
        """cadence.auto_sleep_time type must be 'string'."""
        by_key = _schema_by_key()
        row = by_key["cadence.auto_sleep_time"]
        assert row.get("type") == "string", (
            f"cadence.auto_sleep_time type={row.get('type')!r}; expected 'string'"
        )

    def test_auto_sleep_enabled_editable(self):
        """cadence.auto_sleep_enabled must be user-editable."""
        by_key = _schema_by_key()
        assert _schema_by_key()["cadence.auto_sleep_enabled"].get("editable") is True

    def test_auto_sleep_time_editable(self):
        """cadence.auto_sleep_time must be user-editable."""
        by_key = _schema_by_key()
        assert _schema_by_key()["cadence.auto_sleep_time"].get("editable") is True

    def test_auto_sleep_entries_same_tier_as_other_cadence_keys(self):
        """Both auto_sleep keys must use TIER_A — same as drowsiness cadence keys."""
        from tools.config_schema import TIER_A
        by_key = _schema_by_key()
        for key in ("cadence.auto_sleep_enabled", "cadence.auto_sleep_time"):
            assert by_key[key].get("tier") == TIER_A, (
                f"{key} tier={by_key[key].get('tier')!r}; expected TIER_A={TIER_A!r}"
            )

    def test_auto_sleep_section_exists_in_sections(self):
        """The auto_sleep section must be registered in SECTIONS."""
        from tools.config_schema import SECTIONS
        section_ids = {s["id"] for s in SECTIONS}
        assert "auto_sleep" in section_ids, (
            f"'auto_sleep' section missing from SECTIONS; found: {sorted(section_ids)}"
        )


class TestDefaultConfigCadence:
    """_ensure_data_dir writes default config with the two new cadence keys."""

    def test_default_config_cadence_has_auto_sleep_enabled(self):
        """_default_config() cadence block must include auto_sleep_enabled=False."""
        import importlib
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp()
        try:
            import server
            # Reload to avoid cached module state from other tests
            importlib.reload(server)
            server._configure_paths(tmp)
            server._ensure_data_dir()

            import json
            config_path = Path(tmp) / "config.json"
            config = json.loads(config_path.read_text())
            cadence = config.get("cadence", {})
            assert "auto_sleep_enabled" in cadence, (
                f"auto_sleep_enabled missing from cadence block; keys: {list(cadence.keys())}"
            )
            assert cadence["auto_sleep_enabled"] is False, (
                f"auto_sleep_enabled default should be False (opt-in); got {cadence['auto_sleep_enabled']!r}"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_default_config_cadence_has_auto_sleep_time(self):
        """_default_config() cadence block must include auto_sleep_time='03:00'."""
        import importlib
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp()
        try:
            import server
            importlib.reload(server)
            server._configure_paths(tmp)
            server._ensure_data_dir()

            import json
            config_path = Path(tmp) / "config.json"
            config = json.loads(config_path.read_text())
            cadence = config.get("cadence", {})
            assert "auto_sleep_time" in cadence, (
                f"auto_sleep_time missing from cadence block; keys: {list(cadence.keys())}"
            )
            assert cadence["auto_sleep_time"] == "03:00", (
                f"auto_sleep_time default should be '03:00'; got {cadence['auto_sleep_time']!r}"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
