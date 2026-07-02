"""Tests for tools/_status_derive.py.

Covers:
  1. derive_own_status — all signal combinations.
  2. _read_loop_mode — present / absent / malformed.
  3. _held_baton_turns / _fetch_held_batons — turn-matching logic against a
     mocked forum HTTP endpoint (#1608 — repointed from a local-file glob).
  4. _resolve_forum_url — config/env/default precedence.

Uses temp directories to avoid touching any real ~/.engram or /home/agents-shared
paths. The module's path constants are monkeypatched for each test that touches
them. The forum HTTP call is mocked via ``urllib.request.urlopen`` — no real
server is ever hit.
"""

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

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
# Forum HTTP mock helpers
# ---------------------------------------------------------------------------

class _FakeHTTPResponse:
    """Minimal stand-in for the context-manager object urlopen() returns."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _projects_payload(projects: list) -> bytes:
    return json.dumps({"projects": projects}).encode("utf-8")


def _project(project_id: str, turn: str, *, title: str = "", turn_reason: str = "",
             status: str = "in-progress", seq: int = 1) -> dict:
    """Build one /api/projects-shaped record."""
    return {
        "project_id": project_id,
        "title": title,
        "status": status,
        "turn": turn,
        "turn_since": "2026-06-30T00:00:00Z",
        "turn_reason": turn_reason,
        "participants": [turn],
        "seq": seq,
    }


def _mock_urlopen_returning(mod, monkeypatch, projects: list):
    """Patch mod.urllib.request.urlopen to return a fixed /api/projects payload."""
    fake = _FakeHTTPResponse(_projects_payload(projects))

    def _fake_urlopen(req, timeout=None):
        return fake

    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen)


def _mock_urlopen_raising(mod, monkeypatch, exc: Exception):
    def _fake_urlopen(req, timeout=None):
        raise exc

    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen)


def _mock_urlopen_raw(mod, monkeypatch, raw: bytes):
    """Patch urlopen to return arbitrary raw bytes (for malformed-JSON cases)."""
    fake = _FakeHTTPResponse(raw)

    def _fake_urlopen(req, timeout=None):
        return fake

    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen)


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
# _resolve_forum_url — config / env / default precedence
# ---------------------------------------------------------------------------

class TestResolveForumUrl:
    def test_default_when_no_config_no_env(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        monkeypatch.delenv("FORUM_URL", raising=False)
        assert mod._resolve_forum_url() == "http://localhost:5002"

    def test_config_wins(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        monkeypatch.setenv("FORUM_URL", "http://env-should-lose:9999")
        cfg = {"forum": {"url": "http://from-config:5002/"}}
        (tmp_path / "config.json").write_text(json.dumps(cfg))
        assert mod._resolve_forum_url() == "http://from-config:5002"

    def test_env_wins_over_default(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        monkeypatch.setenv("FORUM_URL", "http://from-env:5002")
        assert mod._resolve_forum_url() == "http://from-env:5002"

    def test_malformed_config_falls_back_to_env(self, tmp_path, monkeypatch):
        mod = _load_module()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        monkeypatch.setenv("FORUM_URL", "http://from-env:5002")
        (tmp_path / "config.json").write_text("not json {{{")
        assert mod._resolve_forum_url() == "http://from-env:5002"


# ---------------------------------------------------------------------------
# _held_baton_turns / _fetch_held_batons — mocked HTTP endpoint (#1608)
# ---------------------------------------------------------------------------

class TestHeldBatonTurns:
    def _setup_env(self, mod, tmp_path, monkeypatch):
        """Hermetic ENGRAM_HOME so _resolve_forum_url never reads a real config."""
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        monkeypatch.delenv("FORUM_URL", raising=False)

    def test_empty_agent_name_returns_empty_no_http_call(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup_env(mod, tmp_path, monkeypatch)
        called = []

        def _fake_urlopen(req, timeout=None):
            called.append(req)
            raise AssertionError("must not be called for an empty agent name")

        monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen)
        assert mod._held_baton_turns("") == []
        assert called == []

    def test_turn_matches_agent_returns_project_id(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup_env(mod, tmp_path, monkeypatch)
        _mock_urlopen_returning(mod, monkeypatch, [_project("PR-1", "ariadne")])
        result = mod._held_baton_turns("ariadne")
        assert "PR-1" in result

    def test_other_turn_not_returned(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup_env(mod, tmp_path, monkeypatch)
        _mock_urlopen_returning(mod, monkeypatch, [_project("PR-1", "borges")])
        result = mod._held_baton_turns("ariadne")
        assert result == []

    def test_multiple_projects_filtered_to_turn(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup_env(mod, tmp_path, monkeypatch)
        _mock_urlopen_returning(mod, monkeypatch, [
            _project("PR-1", "ariadne"),
            _project("PR-2", "borges"),
            _project("PR-3", "ariadne"),
        ])
        result = mod._held_baton_turns("ariadne")
        assert sorted(result) == ["PR-1", "PR-3"]

    def test_turn_match_is_case_insensitive(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup_env(mod, tmp_path, monkeypatch)
        _mock_urlopen_returning(mod, monkeypatch, [_project("PR-1", "Ariadne")])
        result = mod._held_baton_turns("ARIADNE")
        assert "PR-1" in result

    def test_forum_unreachable_returns_empty_list(self, tmp_path, monkeypatch):
        """Fail-soft (spec case): forum unreachable → empty queue, never raises."""
        mod = _load_module()
        self._setup_env(mod, tmp_path, monkeypatch)
        import urllib.error as _urlerror
        _mock_urlopen_raising(mod, monkeypatch, _urlerror.URLError("connection refused"))
        assert mod._held_baton_turns("ariadne") == []

    def test_timeout_returns_empty_list(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup_env(mod, tmp_path, monkeypatch)
        import socket
        _mock_urlopen_raising(mod, monkeypatch, socket.timeout("timed out"))
        assert mod._held_baton_turns("ariadne") == []

    def test_malformed_json_returns_empty_list(self, tmp_path, monkeypatch):
        """Fail-soft (spec case): malformed JSON → empty queue, never raises."""
        mod = _load_module()
        self._setup_env(mod, tmp_path, monkeypatch)
        _mock_urlopen_raw(mod, monkeypatch, b"not valid json {{{")
        assert mod._held_baton_turns("ariadne") == []

    def test_non_dict_response_returns_empty_list(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup_env(mod, tmp_path, monkeypatch)
        _mock_urlopen_raw(mod, monkeypatch, json.dumps([1, 2, 3]).encode())
        assert mod._held_baton_turns("ariadne") == []

    def test_missing_projects_key_returns_empty_list(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup_env(mod, tmp_path, monkeypatch)
        _mock_urlopen_raw(mod, monkeypatch, json.dumps({"not_projects": []}).encode())
        assert mod._held_baton_turns("ariadne") == []

    def test_no_matching_projects_returns_empty_list(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup_env(mod, tmp_path, monkeypatch)
        _mock_urlopen_returning(mod, monkeypatch, [])
        assert mod._held_baton_turns("ariadne") == []

    def test_non_dict_project_item_skipped(self, tmp_path, monkeypatch):
        """A malformed individual item (not a dict) is skipped, not fatal."""
        mod = _load_module()
        self._setup_env(mod, tmp_path, monkeypatch)
        _mock_urlopen_raw(
            mod, monkeypatch,
            json.dumps({"projects": ["not-a-dict", _project("PR-1", "ariadne")]}).encode(),
        )
        assert mod._held_baton_turns("ariadne") == ["PR-1"]


# ---------------------------------------------------------------------------
# derive_own_status — state derivation
# ---------------------------------------------------------------------------

class TestDeriveOwnStatus:
    def _setup(self, mod, tmp_path, monkeypatch, loop_data=None, held_projects=None):
        """Configure module paths and mock the forum HTTP endpoint.

        held_projects: optional list of (project_id, turn) tuples, or full
        _project()-shaped dicts, standing in for "batons in ariadne's court".
        """
        lm_path = tmp_path / "loop-mode.json"
        monkeypatch.setattr(mod, "LOOP_MODE_PATH", str(lm_path))
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        monkeypatch.delenv("FORUM_URL", raising=False)
        # Hermeticity: point last-user-activity at a non-existent tmp path so
        # _recently_engaged() reliably returns False and never reads the real
        # ~/.engram/last-user-activity (which would make these tests flaky on a
        # machine where that file exists and is recent).
        monkeypatch.setattr(
            mod, "LAST_USER_ACTIVITY_PATH", str(tmp_path / "last-user-activity")
        )

        if loop_data is not None:
            lm_path.write_text(json.dumps(loop_data))

        projects = []
        for p in (held_projects or []):
            if isinstance(p, dict):
                projects.append(p)
            else:
                project_id, turn = p
                projects.append(_project(project_id, turn))
        _mock_urlopen_returning(mod, monkeypatch, projects)

    # ── state logic ──────────────────────────────────────────────────────────

    def test_loop_marker_present_state_working(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch, loop_data={"topic": "testing"})
        state, activity, queue, cadence = mod.derive_own_status("ariadne")
        assert state == "working"

    def test_held_baton_no_loop_state_working(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch, held_projects=[("PR-5", "ariadne")])
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

    def test_activity_none_when_no_loop_no_baton(self, tmp_path, monkeypatch):
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

    # ── activity-from-held-baton fallback (#1608, F2) ──────────────────────

    def test_activity_falls_back_to_held_baton_title(self, tmp_path, monkeypatch):
        """No loop marker, but a held baton → activity = its title."""
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch, held_projects=[
            _project("PR-9", "ariadne", title="Fix the board reader",
                     turn_reason="claimed from pool"),
        ])
        _, activity, _, _ = mod.derive_own_status("ariadne")
        assert activity == "Fix the board reader"

    def test_activity_falls_back_to_turn_reason_when_title_blank(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch, held_projects=[
            _project("PR-9", "ariadne", title="", turn_reason="claimed from pool"),
        ])
        _, activity, _, _ = mod.derive_own_status("ariadne")
        assert activity == "claimed from pool"

    def test_loop_topic_beats_held_baton_activity(self, tmp_path, monkeypatch):
        """A loop marker's topic still wins over the held-baton fallback."""
        mod = _load_module()
        self._setup(
            mod, tmp_path, monkeypatch,
            loop_data={"topic": "looping on something else"},
            held_projects=[_project("PR-9", "ariadne", title="Fix the board reader")],
        )
        _, activity, _, _ = mod.derive_own_status("ariadne")
        assert activity == "looping on something else"

    def test_override_activity_beats_held_baton_activity(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch, held_projects=[
            _project("PR-9", "ariadne", title="Fix the board reader"),
        ])
        _, activity, _, _ = mod.derive_own_status(
            "ariadne", override_activity="custom activity"
        )
        assert activity == "custom activity"

    def test_activity_held_baton_truncated_at_120(self, tmp_path, monkeypatch):
        mod = _load_module()
        long_title = "y" * 200
        self._setup(mod, tmp_path, monkeypatch, held_projects=[
            _project("PR-9", "ariadne", title=long_title),
        ])
        _, activity, _, _ = mod.derive_own_status("ariadne")
        assert len(activity) == 120

    def test_activity_none_when_held_baton_has_no_title_or_reason(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch, held_projects=[
            _project("PR-9", "ariadne", title="", turn_reason=""),
        ])
        _, activity, _, _ = mod.derive_own_status("ariadne")
        assert activity is None

    def test_activity_uses_top_held_baton_when_multiple(self, tmp_path, monkeypatch):
        """'Top' = the API's own response order (first item)."""
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch, held_projects=[
            _project("PR-1", "ariadne", title="First in line"),
            _project("PR-2", "ariadne", title="Second in line"),
        ])
        _, activity, _, _ = mod.derive_own_status("ariadne")
        assert activity == "First in line"

    # ── queue logic ──────────────────────────────────────────────────────────

    def test_queue_returns_held_batons(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch, held_projects=[
            ("PR-7", "ariadne"), ("PR-9", "ariadne"),
        ])
        _, _, queue, _ = mod.derive_own_status("ariadne")
        assert sorted(queue) == ["PR-7", "PR-9"]

    def test_queue_empty_when_no_batons(self, tmp_path, monkeypatch):
        mod = _load_module()
        self._setup(mod, tmp_path, monkeypatch)
        _, _, queue, _ = mod.derive_own_status("ariadne")
        assert queue == []

    def test_queue_forum_unreachable_degrades_to_empty(self, tmp_path, monkeypatch):
        """derive_own_status must never raise when the forum is unreachable."""
        mod = _load_module()
        lm_path = tmp_path / "loop-mode.json"
        monkeypatch.setattr(mod, "LOOP_MODE_PATH", str(lm_path))
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        monkeypatch.setattr(
            mod, "LAST_USER_ACTIVITY_PATH", str(tmp_path / "last-user-activity")
        )
        monkeypatch.delenv("FORUM_URL", raising=False)
        import urllib.error as _urlerror
        _mock_urlopen_raising(mod, monkeypatch, _urlerror.URLError("connection refused"))
        state, activity, queue, cadence = mod.derive_own_status("ariadne")
        assert queue == []
        assert state == "idle"

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
                    loop_data=None, held_projects=None,
                    stamp_age=None, config=None):
        """Configure all module paths; mock the forum HTTP endpoint."""
        lm_path = tmp_path / "loop-mode.json"
        activity_path = tmp_path / "last-user-activity"

        monkeypatch.setattr(mod, "LOOP_MODE_PATH", str(lm_path))
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        monkeypatch.setattr(mod, "LAST_USER_ACTIVITY_PATH", str(activity_path))
        monkeypatch.delenv("FORUM_URL", raising=False)

        if loop_data is not None:
            lm_path.write_text(json.dumps(loop_data))

        projects = [_project(pid, turn) for pid, turn in (held_projects or [])]
        _mock_urlopen_returning(mod, monkeypatch, projects)

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
