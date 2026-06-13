"""Tests for tools/_status_derive.py.

Covers:
  1. derive_own_status — all signal combinations.
  2. _read_loop_mode — present / absent / malformed.
  3. _held_baton_turns — turn-matching logic.

Uses temp directories to avoid touching any real ~/.engram or /home/agents-shared
paths. The module's path constants are monkeypatched for each test that touches
them.
"""

import importlib.util
import json
import os
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

MODULE_PATH = (
    Path(__file__).parent.parent / "tools" / "_status_derive.py"
)


def _load_module():
    """Load _status_derive as a fresh module instance."""
    spec = importlib.util.spec_from_file_location("_status_derive", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# _read_loop_mode
# ---------------------------------------------------------------------------

class TestReadLoopMode:
    def test_absent_returns_none(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "LOOP_MODE_PATH", str(tmp_path / "loop-mode.json"))
        assert mod._read_loop_mode() is None

    def test_present_returns_dict(self, tmp_path, monkeypatch):
        mod = _load_module()
        lm = tmp_path / "loop-mode.json"
        lm.write_text(json.dumps({"topic": "testing", "cadence_seconds": 270}))
        monkeypatch.setattr(mod, "LOOP_MODE_PATH", str(lm))
        result = mod._read_loop_mode()
        assert result is not None
        assert result["topic"] == "testing"

    def test_malformed_returns_none(self, tmp_path, monkeypatch):
        mod = _load_module()
        lm = tmp_path / "loop-mode.json"
        lm.write_text("not valid json {{{")
        monkeypatch.setattr(mod, "LOOP_MODE_PATH", str(lm))
        assert mod._read_loop_mode() is None

    def test_non_dict_returns_none(self, tmp_path, monkeypatch):
        mod = _load_module()
        lm = tmp_path / "loop-mode.json"
        lm.write_text(json.dumps([1, 2, 3]))
        monkeypatch.setattr(mod, "LOOP_MODE_PATH", str(lm))
        assert mod._read_loop_mode() is None


# ---------------------------------------------------------------------------
# _held_baton_turns
# ---------------------------------------------------------------------------

class TestHeldBatonTurns:
    def _write_baton(self, path: Path, project: str, turn: str) -> None:
        content = f"---\nproject: {project}\nturn: {turn}\nstatus: in-progress\n---\nbody\n"
        path.write_text(content)

    def test_empty_agent_name_returns_empty(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "PROJECTS_DIR", str(tmp_path))
        self._write_baton(tmp_path / "PR-1.md", "PR-1", "ariadne")
        assert mod._held_baton_turns("") == []

    def test_held_baton_returned(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "PROJECTS_DIR", str(tmp_path))
        self._write_baton(tmp_path / "PR-1.md", "PR-1", "ariadne")
        result = mod._held_baton_turns("ariadne")
        assert "PR-1" in result

    def test_other_turn_not_returned(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "PROJECTS_DIR", str(tmp_path))
        self._write_baton(tmp_path / "PR-1.md", "PR-1", "borges")
        result = mod._held_baton_turns("ariadne")
        assert result == []

    def test_multiple_batons(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "PROJECTS_DIR", str(tmp_path))
        self._write_baton(tmp_path / "PR-1.md", "PR-1", "ariadne")
        self._write_baton(tmp_path / "PR-2.md", "PR-2", "borges")
        self._write_baton(tmp_path / "PR-3.md", "PR-3", "ariadne")
        result = mod._held_baton_turns("ariadne")
        assert sorted(result) == ["PR-1", "PR-3"]

    def test_no_project_field_uses_stem(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "PROJECTS_DIR", str(tmp_path))
        # Baton without a project: field
        content = "---\nturn: ariadne\nstatus: in-progress\n---\nbody\n"
        (tmp_path / "pool-x.md").write_text(content)
        result = mod._held_baton_turns("ariadne")
        assert "pool-x" in result

    def test_missing_projects_dir_returns_empty(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "PROJECTS_DIR", str(tmp_path / "nonexistent"))
        result = mod._held_baton_turns("ariadne")
        assert result == []


# ---------------------------------------------------------------------------
# derive_own_status — state derivation
# ---------------------------------------------------------------------------

class TestDeriveOwnStatus:
    def _setup(self, mod, tmp_path, monkeypatch, loop_data=None, baton_turns=None):
        """Configure module paths and write optional fixtures."""
        lm_path = tmp_path / "loop-mode.json"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        monkeypatch.setattr(mod, "LOOP_MODE_PATH", str(lm_path))
        monkeypatch.setattr(mod, "PROJECTS_DIR", str(projects_dir))
        # Hermeticity: point last-user-activity at a non-existent tmp path so
        # _recently_engaged() reliably returns False and never reads the real
        # ~/.engram/last-user-activity (which would make these tests flaky on a
        # machine where that file exists and is recent).
        monkeypatch.setattr(
            mod, "LAST_USER_ACTIVITY_PATH", str(tmp_path / "last-user-activity")
        )

        if loop_data is not None:
            lm_path.write_text(json.dumps(loop_data))

        if baton_turns:
            for project_id in baton_turns:
                content = (
                    f"---\nproject: {project_id}\nturn: ariadne\n"
                    f"status: in-progress\n---\nbody\n"
                )
                (projects_dir / f"{project_id}.md").write_text(content)

    # ── state logic ──────────────────────────────────────────────────────────

    def test_loop_marker_present_state_working(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch, loop_data={"topic": "testing"})
        state, activity, queue, cadence = mod.derive_own_status("ariadne")
        assert state == "working"

    def test_held_baton_no_loop_state_working(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch, baton_turns=["PR-5"])
        state, activity, queue, cadence = mod.derive_own_status("ariadne")
        assert state == "working"

    def test_neither_loop_nor_baton_state_idle(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch)
        state, activity, queue, cadence = mod.derive_own_status("ariadne")
        assert state == "idle"

    def test_override_state_wins(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch, loop_data={"topic": "testing"})
        state, _, _, _ = mod.derive_own_status("ariadne", override_state="sleeping")
        assert state == "sleeping"

    # ── activity logic ───────────────────────────────────────────────────────

    def test_activity_from_loop_topic(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch,
                    loop_data={"topic": "building status board"})
        _, activity, _, _ = mod.derive_own_status("ariadne")
        assert activity == "building status board"

    def test_activity_truncated_at_120(self, tmp_path, monkeypatch):
        mod = _load_module()
        long_topic = "x" * 200
        self._setup(mod, tmp_path, monkeypatch, loop_data={"topic": long_topic})
        _, activity, _, _ = mod.derive_own_status("ariadne")
        assert len(activity) == 120

    def test_activity_none_when_no_loop(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch)
        _, activity, _, _ = mod.derive_own_status("ariadne")
        assert activity is None

    def test_override_activity_wins(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch, loop_data={"topic": "loop topic"})
        _, activity, _, _ = mod.derive_own_status(
            "ariadne", override_activity="custom activity"
        )
        assert activity == "custom activity"

    # ── queue logic ──────────────────────────────────────────────────────────

    def test_queue_returns_held_batons(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch, baton_turns=["PR-7", "PR-9"])
        _, _, queue, _ = mod.derive_own_status("ariadne")
        assert sorted(queue) == ["PR-7", "PR-9"]

    def test_queue_empty_when_no_batons(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch)
        _, _, queue, _ = mod.derive_own_status("ariadne")
        assert queue == []

    # ── cadence logic ────────────────────────────────────────────────────────

    def test_cadence_from_cadence_seconds(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch,
                    loop_data={"cadence_seconds": 270})
        _, _, _, cadence = mod.derive_own_status("ariadne")
        assert cadence == 270

    def test_cadence_monitor_pacer_returns_sentinel(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch,
                    loop_data={"pacer": "monitor", "cadence_seconds": 0})
        _, _, _, cadence = mod.derive_own_status("ariadne")
        assert cadence == mod.ON_CALL_SENTINEL

    def test_cadence_zero_returns_sentinel(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch,
                    loop_data={"cadence_seconds": 0})
        _, _, _, cadence = mod.derive_own_status("ariadne")
        assert cadence == mod.ON_CALL_SENTINEL

    def test_cadence_none_when_no_loop(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch)
        _, _, _, cadence = mod.derive_own_status("ariadne")
        assert cadence is None

    def test_on_call_flag_overrides_to_sentinel(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch,
                    loop_data={"cadence_seconds": 270})
        _, _, _, cadence = mod.derive_own_status("ariadne", on_call=True)
        assert cadence == mod.ON_CALL_SENTINEL

    def test_override_cadence_wins(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch,
                    loop_data={"cadence_seconds": 270})
        _, _, _, cadence = mod.derive_own_status(
            "ariadne", override_cadence=600
        )
        assert cadence == 600

    def test_override_cadence_none_wins(self, tmp_path, monkeypatch):
        """override_cadence=None (explicitly passed) should override the loop value."""
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch,
                    loop_data={"cadence_seconds": 270})
        _, _, _, cadence = mod.derive_own_status(
            "ariadne", override_cadence=None
        )
        assert cadence is None


# ---------------------------------------------------------------------------
# _read_engaged_window
# ---------------------------------------------------------------------------

class TestReadEngagedWindow:
    def test_default_when_no_config(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        assert mod._read_engaged_window() == 360

    def test_reads_from_config(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        cfg = {"cadence": {"engaged_window_seconds": 120}}
        (tmp_path / "config.json").write_text(json.dumps(cfg))
        assert mod._read_engaged_window() == 120

    def test_default_on_malformed_config(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text("not json {{{")
        assert mod._read_engaged_window() == 360

    def test_default_when_cadence_key_absent(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text(json.dumps({"other": "value"}))
        assert mod._read_engaged_window() == 360

    def test_default_on_zero_value(self, tmp_path, monkeypatch):
        """Zero is not a valid window — default."""
        mod = _load_module()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        cfg = {"cadence": {"engaged_window_seconds": 0}}
        (tmp_path / "config.json").write_text(json.dumps(cfg))
        assert mod._read_engaged_window() == 360


# ---------------------------------------------------------------------------
# _recently_engaged
# ---------------------------------------------------------------------------

class TestRecentlyEngaged:
    def _write_stamp(self, tmp_path: Path, age_seconds: float) -> None:
        """Write a stamp with the given age (seconds in the past)."""
        import time as _time
        epoch = _time.time() - age_seconds
        (tmp_path / "last-user-activity").write_text(str(int(epoch)))

    def test_engaged_when_stamp_recent(self, tmp_path, monkeypatch):
        """Spec case 1: stamp is now-10s (within default 360s window)."""
        mod = _load_module()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        monkeypatch.setattr(mod, "LAST_USER_ACTIVITY_PATH",
                            str(tmp_path / "last-user-activity"))
        self._write_stamp(tmp_path, 10)
        assert mod._recently_engaged() is True

    def test_not_engaged_when_stamp_stale(self, tmp_path, monkeypatch):
        """Spec case 2: stamp is now-9999s (well outside default 360s window)."""
        mod = _load_module()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        monkeypatch.setattr(mod, "LAST_USER_ACTIVITY_PATH",
                            str(tmp_path / "last-user-activity"))
        self._write_stamp(tmp_path, 9999)
        assert mod._recently_engaged() is False

    def test_not_engaged_when_stamp_absent(self, tmp_path, monkeypatch):
        """Spec case 3: stamp file absent → fail-open (not engaged)."""
        mod = _load_module()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        monkeypatch.setattr(mod, "LAST_USER_ACTIVITY_PATH",
                            str(tmp_path / "last-user-activity"))
        # No file written.
        assert mod._recently_engaged() is False

    def test_not_engaged_when_stamp_unparseable(self, tmp_path, monkeypatch):
        """Spec case 4a: unparseable stamp → fail-open."""
        mod = _load_module()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        monkeypatch.setattr(mod, "LAST_USER_ACTIVITY_PATH",
                            str(tmp_path / "last-user-activity"))
        (tmp_path / "last-user-activity").write_text("not-a-number")
        assert mod._recently_engaged() is False

    def test_not_engaged_when_stamp_future_dated(self, tmp_path, monkeypatch):
        """Spec case 4b: future-dated stamp → fail-open (never stuck-engaged)."""
        import time as _time
        mod = _load_module()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        monkeypatch.setattr(mod, "LAST_USER_ACTIVITY_PATH",
                            str(tmp_path / "last-user-activity"))
        future_epoch = int(_time.time()) + 9999
        (tmp_path / "last-user-activity").write_text(str(future_epoch))
        assert mod._recently_engaged() is False

    def test_custom_window_respected(self, tmp_path, monkeypatch):
        """Spec case 8: engaged_window_seconds=60 → stamp at now-120s is NOT engaged."""
        mod = _load_module()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        monkeypatch.setattr(mod, "LAST_USER_ACTIVITY_PATH",
                            str(tmp_path / "last-user-activity"))
        cfg = {"cadence": {"engaged_window_seconds": 60}}
        (tmp_path / "config.json").write_text(json.dumps(cfg))
        self._write_stamp(tmp_path, 120)
        assert mod._recently_engaged() is False

    def test_custom_window_engaged_within(self, tmp_path, monkeypatch):
        """Custom window: stamp at now-30s is engaged when window=60."""
        mod = _load_module()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        monkeypatch.setattr(mod, "LAST_USER_ACTIVITY_PATH",
                            str(tmp_path / "last-user-activity"))
        cfg = {"cadence": {"engaged_window_seconds": 60}}
        (tmp_path / "config.json").write_text(json.dumps(cfg))
        self._write_stamp(tmp_path, 30)
        assert mod._recently_engaged() is True


# ---------------------------------------------------------------------------
# derive_own_status — engaged state integration
# ---------------------------------------------------------------------------

class TestDeriveOwnStatusEngaged:
    """Test the 'engaged' state in the derive_own_status state ladder."""

    def _setup_full(self, mod, tmp_path, monkeypatch,
                    loop_data=None, baton_turns=None,
                    stamp_age=None, config=None):
        """Configure all module paths; write optional fixtures."""
        lm_path = tmp_path / "loop-mode.json"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        activity_path = tmp_path / "last-user-activity"

        monkeypatch.setattr(mod, "LOOP_MODE_PATH", str(lm_path))
        monkeypatch.setattr(mod, "PROJECTS_DIR", str(projects_dir))
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        monkeypatch.setattr(mod, "LAST_USER_ACTIVITY_PATH", str(activity_path))

        if loop_data is not None:
            lm_path.write_text(json.dumps(loop_data))

        if baton_turns:
            for project_id in baton_turns:
                content = (
                    f"---\nproject: {project_id}\nturn: ariadne\n"
                    f"status: in-progress\n---\nbody\n"
                )
                (projects_dir / f"{project_id}.md").write_text(content)

        if stamp_age is not None:
            import time as _time
            epoch = int(_time.time()) - stamp_age
            activity_path.write_text(str(epoch))

        if config is not None:
            (tmp_path / "config.json").write_text(json.dumps(config))

    def test_engaged_when_recent_stamp(self, tmp_path, monkeypatch):
        """Spec case 1: recent stamp (now-10s) → state=='engaged'."""
        mod = _load_module()
        self._setup_full(mod, tmp_path, monkeypatch, stamp_age=10)
        state, _, _, _ = mod.derive_own_status("ariadne")
        assert state == "engaged"

    def test_not_engaged_when_stamp_stale(self, tmp_path, monkeypatch):
        """Spec case 2: stale stamp (now-9999s) → falls through to working/idle."""
        mod = _load_module()
        # With a loop marker also present, state should be 'working' (not engaged).
        self._setup_full(mod, tmp_path, monkeypatch,
                         loop_data={"topic": "testing"}, stamp_age=9999)
        state, _, _, _ = mod.derive_own_status("ariadne")
        assert state == "working"

    def test_not_engaged_when_stamp_absent(self, tmp_path, monkeypatch):
        """Spec case 3: no stamp file → fail-open → idle (no loop/baton)."""
        mod = _load_module()
        self._setup_full(mod, tmp_path, monkeypatch)
        state, _, _, _ = mod.derive_own_status("ariadne")
        assert state == "idle"

    def test_not_engaged_when_stamp_future_or_unparseable(self, tmp_path, monkeypatch):
        """Spec case 4: future-dated stamp → fail-open → idle."""
        import time as _time
        mod = _load_module()
        self._setup_full(mod, tmp_path, monkeypatch)
        activity_path = tmp_path / "last-user-activity"
        activity_path.write_text(str(int(_time.time()) + 9999))
        state, _, _, _ = mod.derive_own_status("ariadne")
        assert state == "idle"

    def test_engaged_outranks_working(self, tmp_path, monkeypatch):
        """Spec case 5: loop marker present AND recent stamp → engaged (not working)."""
        mod = _load_module()
        self._setup_full(mod, tmp_path, monkeypatch,
                         loop_data={"topic": "building"}, stamp_age=10)
        state, _, _, _ = mod.derive_own_status("ariadne")
        assert state == "engaged"

    def test_override_state_beats_engaged(self, tmp_path, monkeypatch):
        """Spec case 6: override_state wins even when stamp is recent."""
        mod = _load_module()
        self._setup_full(mod, tmp_path, monkeypatch, stamp_age=10)
        state, _, _, _ = mod.derive_own_status("ariadne", override_state="sleeping")
        assert state == "sleeping"

    def test_engaged_window_config_respected(self, tmp_path, monkeypatch):
        """Spec case 8: custom window=60 → stamp at now-120s is NOT engaged."""
        mod = _load_module()
        self._setup_full(
            mod, tmp_path, monkeypatch,
            stamp_age=120,
            config={"cadence": {"engaged_window_seconds": 60}},
        )
        state, _, _, _ = mod.derive_own_status("ariadne")
        assert state == "idle"
