"""Tests for loop_gate_decision() in tools/_status_derive.py
and the engram-loop-gate.py CLI wrapper.

Covers spec #1456 v1 test cases:
  1. engaged → defer (stamp recent, N in [60, 360])
  2. clear (old stamp) → proceed
  3. missing stamp → proceed (fail-open)
  4. future-dated stamp → proceed (fail-open)
  5. config read error + missing stamp → proceed (fail-open)
  6. min-defer clamp: stamp near window edge → defer clamped to _LOOP_GATE_MIN_DEFER
  7. CLI: plain text + --json output, exit 0 in both cases

Also covers the loop-suspend-enactment addition (#1469 + presence wiring):
  8. presence mode=user (derived) → ("defer", N, "presence-derived-user")
  9. presence override-user (stale stamp, no TTL) → ("defer", cooldown, "presence-override-user")
  10. presence override-user (with TTL) → defer from remaining TTL
  11. presence override-auto with fresh stamp → ("proceed", 0, "presence-override-auto")
  12. presence absent → identical to #1469 user-engaged behavior
  13. presence derived-auto → falls through to #1469 proceed

Uses temp directories and monkeypatching to avoid touching any real
~/.engram paths. Follows the fixture pattern of test_status_derive.py.
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module + CLI paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent

MODULE_PATH = REPO_ROOT / "tools" / "_status_derive.py"

CLI_PATH = REPO_ROOT / "tools" / "engram-loop-gate.py"

# Symlink entry-point: tools/engram-loop-gate.py → src/engram/tools/engram-loop-gate.py
# This is the path SKILL.md documents for bare-exec invocation.
SYMLINK_CLI_PATH = REPO_ROOT / "tools" / "engram-loop-gate.py"


def _load_module():
    """Load _status_derive as a fresh module instance."""
    spec = importlib.util.spec_from_file_location("_status_derive", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

def _write_stamp(tmp_path: Path, age_seconds: float) -> Path:
    """Write last-user-activity with given age (seconds in the past)."""
    epoch = time.time() - age_seconds
    stamp_file = tmp_path / "last-user-activity"
    stamp_file.write_text(str(int(epoch)))
    return stamp_file


def _setup(mod, tmp_path, monkeypatch, stamp_age=None, config=None):
    """Configure module paths and write optional fixtures.

    Sets LAST_USER_ACTIVITY_PATH, ENGRAM_HOME to tmp_path so tests are
    hermetic.  config_age and stamp_age are optional.
    """
    activity_path = tmp_path / "last-user-activity"
    monkeypatch.setattr(mod, "LAST_USER_ACTIVITY_PATH", str(activity_path))
    monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))

    if stamp_age is not None:
        _write_stamp(tmp_path, stamp_age)

    if config is not None:
        (tmp_path / "config.json").write_text(json.dumps(config))


# ---------------------------------------------------------------------------
# Test case 1 — engaged → defer
# ---------------------------------------------------------------------------

class TestLoopGateDecisionEngagedDefer:
    """Spec case 1: recent stamp → ("defer", N, ...) with N in [60, 360]."""

    def test_engaged_returns_defer(self, tmp_path, monkeypatch):
        """stamp = now-60, default window=360 → defer, N in [60, 360]."""
        mod = _load_module()
        _setup(mod, tmp_path, monkeypatch, stamp_age=60)

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "defer"
        assert reason == "user-engaged"
        # remaining ≈ 300, defer_raw ≈ 330 — within [_LOOP_GATE_MIN_DEFER, window]
        assert defer_seconds >= mod._LOOP_GATE_MIN_DEFER
        assert defer_seconds <= 360  # default window

    def test_defer_seconds_approximately_remaining_plus_margin(self, tmp_path, monkeypatch):
        """N ≈ (window - elapsed) + margin = (360 - 60) + 30 = 330."""
        mod = _load_module()
        _setup(mod, tmp_path, monkeypatch, stamp_age=60)

        _, defer_seconds, _ = mod.loop_gate_decision()

        # Allow a few seconds of clock drift in the test runner.
        assert defer_seconds >= 290  # 330 - 40 slack
        assert defer_seconds <= 360

    def test_custom_window_respected(self, tmp_path, monkeypatch):
        """Custom window=120, stamp at now-30 → defer N in [60, 120]."""
        mod = _load_module()
        _setup(
            mod, tmp_path, monkeypatch,
            stamp_age=30,
            config={"cadence": {"engaged_window_seconds": 120}},
        )

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "defer"
        assert defer_seconds >= mod._LOOP_GATE_MIN_DEFER
        assert defer_seconds <= 120


# ---------------------------------------------------------------------------
# Test case 2 — clear (old stamp) → proceed
# ---------------------------------------------------------------------------

class TestLoopGateDecisionClear:
    def test_old_stamp_returns_proceed(self, tmp_path, monkeypatch):
        """stamp = now-10_000 (well outside 360s window) → proceed."""
        mod = _load_module()
        _setup(mod, tmp_path, monkeypatch, stamp_age=10_000)

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "proceed"
        assert defer_seconds == 0


# ---------------------------------------------------------------------------
# Test case 3 — missing stamp → proceed (fail-open)
# ---------------------------------------------------------------------------

class TestLoopGateDecisionMissingStamp:
    def test_missing_stamp_returns_proceed(self, tmp_path, monkeypatch):
        """No stamp file → fail-open → proceed."""
        mod = _load_module()
        _setup(mod, tmp_path, monkeypatch)  # no stamp_age → no file written

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "proceed"
        assert defer_seconds == 0


# ---------------------------------------------------------------------------
# Test case 4 — future-dated stamp → proceed (fail-open)
# ---------------------------------------------------------------------------

class TestLoopGateDecisionFutureDated:
    def test_future_stamp_returns_proceed(self, tmp_path, monkeypatch):
        """Future-dated stamp → fail-open (matches _recently_engaged) → proceed."""
        mod = _load_module()
        activity_path = tmp_path / "last-user-activity"
        monkeypatch.setattr(mod, "LAST_USER_ACTIVITY_PATH", str(activity_path))
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        future_epoch = int(time.time()) + 9999
        activity_path.write_text(str(future_epoch))

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "proceed"
        assert defer_seconds == 0


# ---------------------------------------------------------------------------
# Test case 5 — config read error → proceed (fail-open)
# ---------------------------------------------------------------------------

class TestLoopGateDecisionConfigError:
    def test_bad_config_and_missing_stamp_returns_proceed(self, tmp_path, monkeypatch):
        """Config at a non-existent path + no stamp → fail-open → proceed.

        Both ENGRAM_HOME (for config.json lookup) and LAST_USER_ACTIVITY_PATH
        are pointed at a non-existent directory so the config read fails and
        no stamp exists.  _recently_engaged() fails open (False) → proceed.
        """
        mod = _load_module()
        bad_dir = tmp_path / "nonexistent"
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(bad_dir))
        monkeypatch.setattr(
            mod, "LAST_USER_ACTIVITY_PATH", str(bad_dir / "last-user-activity")
        )

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "proceed"
        assert defer_seconds == 0

    def test_gate_error_path_returns_proceed(self, tmp_path, monkeypatch):
        """Inject an exception from _recently_engaged → outer except → proceed."""
        mod = _load_module()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        monkeypatch.setattr(
            mod, "LAST_USER_ACTIVITY_PATH", str(tmp_path / "last-user-activity")
        )
        # Force an exception from _recently_engaged by making it raise.
        monkeypatch.setattr(mod, "_recently_engaged", lambda: (_ for _ in ()).throw(RuntimeError("injected")))

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "proceed"
        assert defer_seconds == 0
        assert reason == "gate-error"


# ---------------------------------------------------------------------------
# Test case 6 — min-defer clamp
# ---------------------------------------------------------------------------

class TestLoopGateDecisionMinDeferClamp:
    def test_near_window_edge_clamped_to_min_defer(self, tmp_path, monkeypatch):
        """stamp = now-359 (1s of 360s window left) → defer clamped to _LOOP_GATE_MIN_DEFER.

        remaining ≈ 1, defer_raw = 1 + 30 = 31 < _LOOP_GATE_MIN_DEFER (60) → 60.
        """
        mod = _load_module()
        _setup(mod, tmp_path, monkeypatch, stamp_age=359)

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "defer"
        assert defer_seconds == mod._LOOP_GATE_MIN_DEFER
        assert reason == "user-engaged"

    def test_min_defer_constant_is_60(self, tmp_path, monkeypatch):
        """Verify _LOOP_GATE_MIN_DEFER == 60 as specified."""
        mod = _load_module()
        assert mod._LOOP_GATE_MIN_DEFER == 60


# ---------------------------------------------------------------------------
# Test case — user-engaged-fallback (reviewer suggestion: PR #1469)
# ---------------------------------------------------------------------------

class TestLoopGateDecisionEngagedFallback:
    """Inner read race: _recently_engaged() True but second open fails.

    Reviewer suggestion (PR #1469): cover the inner-except path where
    _recently_engaged() succeeds but the subsequent stamp re-read raises.
    The function must return ("defer", window, "user-engaged-fallback") —
    a full-window conservative defer, NOT proceed.
    """

    def test_read_race_returns_full_window_defer(self, tmp_path, monkeypatch):
        """_recently_engaged() stub → True; second open absent → fallback defer.

        Stub _recently_engaged to True so the outer gate passes, then point
        LAST_USER_ACTIVITY_PATH at a non-existent file so the inner open()
        raises FileNotFoundError.  The except clause must return the
        full-window conservative defer, not proceed.
        """
        mod = _load_module()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        # File not written → inner open() raises FileNotFoundError.
        monkeypatch.setattr(
            mod, "LAST_USER_ACTIVITY_PATH", str(tmp_path / "last-user-activity")
        )
        # Bypass _recently_engaged() so only the inner read races.
        monkeypatch.setattr(mod, "_recently_engaged", lambda: True)

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "defer"
        assert reason == "user-engaged-fallback"
        # No config.json in tmp_path → _read_engaged_window returns the default.
        assert defer_seconds == mod._DEFAULT_ENGAGED_WINDOW


# ---------------------------------------------------------------------------
# Presence integration tests (loop-suspend-enactment)
# ---------------------------------------------------------------------------

def _make_presence_mock(
    *,
    mode: str,
    loop_suspended: bool,
    override_dict=None,
    cooldown: int = 600,
) -> types.ModuleType:
    """Build a minimal presence mock module for injection into sys.modules.

    Parameters
    ----------
    mode:
        The mode string get_mode() should return ('user' or 'auto').
    loop_suspended:
        The loop_suspended value in the bundle returned by get_mode().
    override_dict:
        What active_override() should return (None or a dict).
    cooldown:
        What cooldown_seconds() should return.
    """
    import datetime

    mock = types.ModuleType("presence")
    # Accept but ignore the override kwarg — the mock has a fixed mode; the
    # caller (_status_derive.loop_gate_decision) now passes override=<dict|None>
    # to avoid a double active_override() read per call (suggestion 2 round-2).
    mock.get_mode = lambda override=None: (mode, {"loop_suspended": loop_suspended, "feed": "all"})
    mock.active_override = lambda: override_dict
    mock.cooldown_seconds = lambda: cooldown

    def _parse_iso(iso_str: str) -> float:
        dt = datetime.datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()

    mock.parse_iso = _parse_iso
    return mock


class TestLoopGatePresence:
    """Presence integration for loop_gate_decision() (loop-suspend-enactment).

    All tests inject a presence mock via sys.modules so the lazy
    ``import presence`` inside loop_gate_decision finds the mock rather than
    searching sys.path (presence.py is not on the test sys.path).
    Tests for the 'presence absent' path explicitly do NOT inject a mock.
    """

    # -----------------------------------------------------------------------
    # Case 8 — derived user (recent stamp) → presence-derived-user defer
    # -----------------------------------------------------------------------

    def test_presence_derived_user_defers(self, tmp_path, monkeypatch):
        """mode=user (derived), no override, fresh stamp → presence-derived-user.

        N is clamped to [_LOOP_GATE_MIN_DEFER, cooldown] and reflects the
        remaining time in the cooldown window plus the margin.
        """
        mod = _load_module()
        cooldown = 600
        stamp_age = 60  # 60s old → 540s remaining in cooldown
        _setup(mod, tmp_path, monkeypatch, stamp_age=stamp_age)

        presence_mock = _make_presence_mock(
            mode="user",
            loop_suspended=True,
            override_dict=None,
            cooldown=cooldown,
        )
        monkeypatch.setitem(sys.modules, "presence", presence_mock)

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "defer"
        assert reason == "presence-derived-user"
        # remaining ≈ 540, defer_raw ≈ 570 → clamped to cooldown (600)
        assert defer_seconds >= mod._LOOP_GATE_MIN_DEFER
        assert defer_seconds <= cooldown

    def test_presence_derived_user_clamp_at_min_defer(self, tmp_path, monkeypatch):
        """mode=user, stamp near cooldown edge → defer clamped to _LOOP_GATE_MIN_DEFER.

        stamp_age=599 → remaining=1, defer_raw=1+30=31 < 60 → clamped to 60.
        """
        mod = _load_module()
        cooldown = 600
        _setup(mod, tmp_path, monkeypatch, stamp_age=599)

        presence_mock = _make_presence_mock(
            mode="user", loop_suspended=True, override_dict=None, cooldown=cooldown
        )
        monkeypatch.setitem(sys.modules, "presence", presence_mock)

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "defer"
        assert reason == "presence-derived-user"
        assert defer_seconds == mod._LOOP_GATE_MIN_DEFER

    def test_presence_derived_user_stamp_missing_falls_back_to_cooldown(self, tmp_path, monkeypatch):
        """mode=user, no stamp file → derived-user with defer=cooldown (inner except)."""
        mod = _load_module()
        cooldown = 600
        _setup(mod, tmp_path, monkeypatch)  # no stamp written

        presence_mock = _make_presence_mock(
            mode="user", loop_suspended=True, override_dict=None, cooldown=cooldown
        )
        monkeypatch.setitem(sys.modules, "presence", presence_mock)

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "defer"
        assert reason == "presence-derived-user"
        assert defer_seconds == cooldown  # inner-except fallback

    # -----------------------------------------------------------------------
    # Case 9 — override user (no TTL / stale stamp) → presence-override-user
    # -----------------------------------------------------------------------

    def test_presence_override_user_no_expiry(self, tmp_path, monkeypatch):
        """Explicit user override with no TTL (stale stamp) → presence-override-user.

        defer = cooldown (bounded re-check for no-expiry overrides).
        """
        mod = _load_module()
        cooldown = 600
        # Stale stamp so derived would be 'auto', but explicit override wins.
        _setup(mod, tmp_path, monkeypatch, stamp_age=9999)

        override_dict = {"mode": "user", "since": "2026-06-26T00:00:00+00:00", "expires_at": None}
        presence_mock = _make_presence_mock(
            mode="user",
            loop_suspended=True,
            override_dict=override_dict,
            cooldown=cooldown,
        )
        monkeypatch.setitem(sys.modules, "presence", presence_mock)

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "defer"
        assert reason == "presence-override-user"
        assert defer_seconds == cooldown

    # -----------------------------------------------------------------------
    # Case 10 — override user with TTL → defer from remaining TTL
    # -----------------------------------------------------------------------

    def test_presence_override_user_with_ttl(self, tmp_path, monkeypatch):
        """Explicit user override with future expires_at → defer from remaining TTL.

        TTL = 120s from now; remaining ≈ 120; defer = int(max(60, min(150, 600))) = 150.
        """
        import datetime

        mod = _load_module()
        cooldown = 600
        _setup(mod, tmp_path, monkeypatch)

        # expires_at = 120s from now
        future = datetime.datetime.now(tz=datetime.timezone.utc) + datetime.timedelta(seconds=120)
        expires_at_str = future.isoformat()
        override_dict = {"mode": "user", "since": "2026-06-26T00:00:00+00:00", "expires_at": expires_at_str}
        presence_mock = _make_presence_mock(
            mode="user",
            loop_suspended=True,
            override_dict=override_dict,
            cooldown=cooldown,
        )
        monkeypatch.setitem(sys.modules, "presence", presence_mock)

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "defer"
        assert reason == "presence-override-user"
        # remaining ≈ 120, defer_raw = 120 + 30 = 150 → clamped to [60, 600] → 150
        assert defer_seconds >= mod._LOOP_GATE_MIN_DEFER
        assert defer_seconds <= cooldown
        # Should be close to remaining + margin (allow clock drift).
        assert 100 <= defer_seconds <= 200

    # -----------------------------------------------------------------------
    # Case 11 — override auto with fresh stamp → presence-override-auto (proceed)
    # -----------------------------------------------------------------------

    def test_presence_override_auto_beats_engaged_defer(self, tmp_path, monkeypatch):
        """Explicit auto override with a fresh stamp → proceed (override-auto wins).

        Without presence, a fresh stamp would trigger engaged-defer.
        With an explicit auto override, the loop must proceed.
        """
        mod = _load_module()
        # Fresh stamp: without presence this would trigger user-engaged defer.
        _setup(mod, tmp_path, monkeypatch, stamp_age=60)

        override_dict = {"mode": "auto", "since": "2026-06-26T00:00:00+00:00", "expires_at": None}
        presence_mock = _make_presence_mock(
            mode="auto",
            loop_suspended=False,
            override_dict=override_dict,
        )
        monkeypatch.setitem(sys.modules, "presence", presence_mock)

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "proceed"
        assert defer_seconds == 0
        assert reason == "presence-override-auto"

    # -----------------------------------------------------------------------
    # Case 12 — presence absent → identical to #1469 behavior
    # -----------------------------------------------------------------------

    def test_presence_absent_recent_stamp_falls_through_to_user_engaged(self, tmp_path, monkeypatch):
        """When presence is NOT importable, recent stamp → user-engaged (unchanged #1469).

        Absence of a 'presence' entry in sys.modules means the lazy import
        raises ImportError → outer except → fall through to engaged-defer.
        """
        mod = _load_module()
        _setup(mod, tmp_path, monkeypatch, stamp_age=60)
        # Ensure presence is absent from sys.modules (do not inject a mock).
        # setitem-None (not delitem): delitem only clears the cache — if an
        # earlier test made a real `presence` importable (sys.path pollution),
        # the gate re-imports it and the "not importable" premise silently
        # breaks under full-suite ordering. None poisons the slot so `import
        # presence` raises ImportError deterministically.
        monkeypatch.setitem(sys.modules, "presence", None)

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "defer"
        assert reason == "user-engaged"  # existing #1469 path
        assert defer_seconds >= mod._LOOP_GATE_MIN_DEFER

    def test_presence_absent_no_stamp_proceeds(self, tmp_path, monkeypatch):
        """When presence is NOT importable, no stamp → not-engaged (unchanged #1469)."""
        mod = _load_module()
        _setup(mod, tmp_path, monkeypatch)  # no stamp
        # setitem-None (not delitem): delitem only clears the cache — if an
        # earlier test made a real `presence` importable (sys.path pollution),
        # the gate re-imports it and the "not importable" premise silently
        # breaks under full-suite ordering. None poisons the slot so `import
        # presence` raises ImportError deterministically.
        monkeypatch.setitem(sys.modules, "presence", None)

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "proceed"
        assert defer_seconds == 0
        assert reason == "not-engaged"

    def test_presence_error_falls_through_to_existing_path(self, tmp_path, monkeypatch):
        """presence.get_mode() raising → outer except → existing engaged-defer path.

        Verifies that a bug inside the presence module never deadlocks the loop.
        """
        mod = _load_module()
        _setup(mod, tmp_path, monkeypatch, stamp_age=60)

        broken_presence = types.ModuleType("presence")
        broken_presence.active_override = lambda: None
        broken_presence.get_mode = lambda: (_ for _ in ()).throw(RuntimeError("simulated presence failure"))
        monkeypatch.setitem(sys.modules, "presence", broken_presence)

        decision, defer_seconds, reason = mod.loop_gate_decision()

        # Must fall through to the engaged-defer (not gate-error), because the
        # outer except only covers the presence block; the #1469 block is separate.
        assert decision == "defer"
        assert reason == "user-engaged"

    # -----------------------------------------------------------------------
    # Case 13 — derived auto (no activity) → falls through, proceeds
    # -----------------------------------------------------------------------

    def test_presence_derived_auto_no_stamp_proceeds(self, tmp_path, monkeypatch):
        """mode=auto (derived), no override, no stamp → fall through → not-engaged."""
        mod = _load_module()
        _setup(mod, tmp_path, monkeypatch)  # no stamp

        presence_mock = _make_presence_mock(
            mode="auto", loop_suspended=False, override_dict=None
        )
        monkeypatch.setitem(sys.modules, "presence", presence_mock)

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "proceed"
        assert defer_seconds == 0
        assert reason == "not-engaged"

    def test_presence_derived_auto_old_stamp_proceeds(self, tmp_path, monkeypatch):
        """mode=auto (derived), no override, stale stamp → fall through → not-engaged."""
        mod = _load_module()
        _setup(mod, tmp_path, monkeypatch, stamp_age=9999)

        presence_mock = _make_presence_mock(
            mode="auto", loop_suspended=False, override_dict=None
        )
        monkeypatch.setitem(sys.modules, "presence", presence_mock)

        decision, defer_seconds, reason = mod.loop_gate_decision()

        assert decision == "proceed"
        assert defer_seconds == 0
        assert reason == "not-engaged"


# ---------------------------------------------------------------------------
# Test case 7 — CLI: plain text and --json output
# ---------------------------------------------------------------------------

class TestLoopGateCLI:
    """Spec case 7: CLI prints 'defer <int>' or 'proceed'; --json is parseable."""

    def _run_cli(self, tmp_path, stamp_age=None, extra_args=None):
        """Run engram-loop-gate.py as a subprocess with ENGRAM_HOME in tmp_path."""
        env = os.environ.copy()
        env["ENGRAM_HOME"] = str(tmp_path)

        if stamp_age is not None:
            stamp_file = tmp_path / "last-user-activity"
            stamp_file.write_text(str(int(time.time() - stamp_age)))

        cmd = [sys.executable, str(CLI_PATH)] + (extra_args or [])
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
        )
        return result

    def test_engaged_plain_output(self, tmp_path):
        """Recent stamp → 'defer <int>' on stdout, exit 0."""
        result = self._run_cli(tmp_path, stamp_age=60)
        assert result.returncode == 0
        output = result.stdout.strip()
        assert output.startswith("defer ")
        # The integer after 'defer ' must be parseable.
        parts = output.split()
        assert len(parts) == 2
        assert int(parts[1]) >= 60  # at least _LOOP_GATE_MIN_DEFER

    def test_clear_plain_output(self, tmp_path):
        """Old stamp → 'proceed' on stdout, exit 0."""
        result = self._run_cli(tmp_path, stamp_age=10_000)
        assert result.returncode == 0
        assert result.stdout.strip() == "proceed"

    def test_missing_stamp_plain_output(self, tmp_path):
        """No stamp → 'proceed' on stdout, exit 0 (fail-open)."""
        result = self._run_cli(tmp_path)  # no stamp_age
        assert result.returncode == 0
        assert result.stdout.strip() == "proceed"

    def test_engaged_json_output(self, tmp_path):
        """Recent stamp + --json → parseable JSON with decision=defer, exit 0."""
        result = self._run_cli(tmp_path, stamp_age=60, extra_args=["--json"])
        assert result.returncode == 0
        data = json.loads(result.stdout.strip())
        assert data["decision"] == "defer"
        assert isinstance(data["defer_seconds"], int)
        assert data["defer_seconds"] >= 60
        assert "reason" in data

    def test_clear_json_output(self, tmp_path):
        """Old stamp + --json → parseable JSON with decision=proceed, exit 0."""
        result = self._run_cli(tmp_path, stamp_age=10_000, extra_args=["--json"])
        assert result.returncode == 0
        data = json.loads(result.stdout.strip())
        assert data["decision"] == "proceed"
        assert data["defer_seconds"] == 0
        assert "reason" in data

    # -----------------------------------------------------------------------
    # Bare-exec entry-point guard (Ariadne's blocker follow-up, PR #1469)
    # -----------------------------------------------------------------------

    def _run_cli_bare(self, tmp_path, stamp_age=None, extra_args=None):
        """Run tools/engram-loop-gate.py bare-exec — no sys.executable prefix.

        This is the invocation form documented in the SKILL.md:
          "$CLAUDE_PLUGIN_ROOT/tools/engram-loop-gate.py"
        Guards the exec bit: if it's lost, subprocess raises PermissionError
        (or returns a non-zero exit code) while the sys.executable tests pass.
        """
        env = os.environ.copy()
        env["ENGRAM_HOME"] = str(tmp_path)

        if stamp_age is not None:
            stamp_file = tmp_path / "last-user-activity"
            stamp_file.write_text(str(int(time.time() - stamp_age)))

        cmd = [str(SYMLINK_CLI_PATH)] + (extra_args or [])
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
        )
        return result

    def test_bare_exec_entrypoint(self, tmp_path):
        """CLI runs bare-exec (no sys.executable prefix) and prints proceed or defer <N>.

        Invokes tools/engram-loop-gate.py without a python3 prefix — same as
        the SKILL.md documented entry-point.  If the exec bit is lost, this
        test fails; the sys.executable-prefixed tests above would still pass,
        leaving the regression silent.  No stamp → fail-open → 'proceed'.
        """
        result = self._run_cli_bare(tmp_path)  # no stamp → fail-open → proceed
        assert result.returncode == 0
        output = result.stdout.strip()
        # Must print either "proceed" or "defer <int>" — same output contract
        # as the documented SKILL.md entry-point.
        assert output == "proceed" or (
            output.startswith("defer ") and output.split()[1].isdigit()
        )
