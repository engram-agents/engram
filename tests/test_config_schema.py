"""Tests for tools/config_schema.py SCHEMA and _default_config cadence block.

Regression guard for the auto-sleep config fields added in the auto-sleep
config schema PR. Both auto_sleep_enabled and auto_sleep_time are consumed
by the SessionStart hook at session start — their restart_required=True
semantics are load-bearing (a change takes effect next session, not mid-session).

Import-pattern contract (#948 wave C — R1):
    This file imports engram_core directly and contains no ``import server``.
    _configure_paths and _ensure_data_dir are defined in engram_core;
    server.py wraps them via thin forwarding functions.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# config_schema.py lives in tools/, not at repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import engram_core  # noqa: E402 — must follow sys.path setup

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


class TestEngagedWindowDefault:
    """The engaged-window schema `default` surfaces the effective value the
    config UI shows as a placeholder when the key is absent (#1172 finding).
    """

    def test_engaged_row_has_default(self):
        """cadence.engaged_window_seconds must carry a `default` for placeholder display."""
        row = _schema_by_key()["cadence.engaged_window_seconds"]
        assert "default" in row, (
            "cadence.engaged_window_seconds missing `default` — the config UI "
            "would render an empty box with no hint of the effective value"
        )

    def test_engaged_default_matches_status_derive(self):
        """Drift guard: the schema `default` must equal the value the hook actually
        uses (_status_derive._DEFAULT_ENGAGED_WINDOW). Two sources, one truth —
        this test is the mechanical link, so a change to one fails loudly if the
        other isn't updated.
        """
        from _status_derive import _DEFAULT_ENGAGED_WINDOW
        row = _schema_by_key()["cadence.engaged_window_seconds"]
        assert row["default"] == _DEFAULT_ENGAGED_WINDOW, (
            f"schema default {row['default']!r} != hook default "
            f"{_DEFAULT_ENGAGED_WINDOW!r} — config UI placeholder would lie about "
            f"the effective engaged window"
        )

    def test_annotate_schema_carries_default_when_value_absent(self):
        """With the key absent from config, annotate_schema surfaces value=None
        AND the `default` (so the renderer can show it as a placeholder)."""
        from tools.config_schema import annotate_schema
        out = annotate_schema({})  # empty config — nothing set
        engaged_rows = [
            r for sec in out["sections"] for r in sec["rows"]
            if r["key"] == "cadence.engaged_window_seconds"
        ]
        assert len(engaged_rows) == 1
        row = engaged_rows[0]
        assert row["value"] is None, f"expected value=None for absent key; got {row['value']!r}"
        assert row.get("default") == 360, f"expected default=360 to pass through; got {row.get('default')!r}"

    def test_annotate_schema_value_present_suppresses_placeholder(self):
        """When the key IS set, annotate_schema returns the configured value (not
        None) — so the renderer shows the value and the placeholder never fires
        (the JS condition is `val === '' && row.default`). Guards against a future
        widening of the placeholder condition that would mask a real value."""
        from tools.config_schema import annotate_schema
        out = annotate_schema({"cadence": {"engaged_window_seconds": 300}})
        row = next(
            r for sec in out["sections"] for r in sec["rows"]
            if r["key"] == "cadence.engaged_window_seconds"
        )
        assert row["value"] == 300, f"expected configured value 300; got {row['value']!r}"


class TestDefaultConfigCadence:
    """_ensure_data_dir writes default config with the two new cadence keys."""

    def test_default_config_cadence_has_auto_sleep_enabled(self):
        """_default_config() cadence block must include auto_sleep_enabled=False."""
        import importlib
        import json
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp()
        try:
            # Reload engram_core to avoid cached module state from other tests,
            # then configure paths and write defaults. A prior test may have purged
            # engram_* from sys.modules, so import (idempotent) before reload() —
            # reload() raises ImportError if the module isn't currently imported.
            import engram_core
            core = importlib.reload(engram_core)
            core._configure_paths(tmp)
            core._ensure_data_dir()

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
        import json
        import shutil
        import tempfile

        tmp = tempfile.mkdtemp()
        try:
            # Reload engram_core to avoid cached module state from other tests,
            # then configure paths and write defaults. A prior test may have purged
            # engram_* from sys.modules, so import (idempotent) before reload() —
            # reload() raises ImportError if the module isn't currently imported.
            import engram_core
            core = importlib.reload(engram_core)
            core._configure_paths(tmp)
            core._ensure_data_dir()

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
