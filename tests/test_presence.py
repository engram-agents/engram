"""Tests for tools/presence.py and the derive_own_status presence integration.

Covers:
  1. get_mode() — derived user/auto from last-user-activity stamp.
  2. get_mode() — explicit override wins (set_mode); TTL expiry reverts.
  3. Fail-open: corrupt override file / bad config → ("auto", ...).
  4. set_mode() / clear_mode() round-trip.
  5. Bundle consistency — returned bundle is always MODE_BUNDLES[mode].
  6. derive_own_status integration: presence mode user → "engaged" state;
     auto → existing fallback; explicit override_state still wins.
  7. CLI: engram-mode show --json parses; user/clear round-trip.

Uses temp directories to avoid touching any real ~/.engram path.
Module constants are monkeypatched for each test that touches the filesystem,
mirroring the pattern used in test_status_derive.py.
"""

from __future__ import annotations

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
# Module loaders
# ---------------------------------------------------------------------------

_TOOLS_DIR = Path(__file__).parent.parent / "tools"

PRESENCE_MODULE_PATH = _TOOLS_DIR / "presence.py"
SD_MODULE_PATH = _TOOLS_DIR / "_status_derive.py"
CLI_PATH = _TOOLS_DIR / "engram-mode.py"

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_presence():
    """Load presence as a fresh module instance (avoids sys.modules caching)."""
    spec = importlib.util.spec_from_file_location("presence_fresh", PRESENCE_MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_sd():
    """Load _status_derive as a fresh module instance."""
    spec = importlib.util.spec_from_file_location("_status_derive_fresh", SD_MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_stamp(tmp_path: Path, age_seconds: float) -> None:
    """Write a last-user-activity stamp with the given age (seconds in the past)."""
    epoch = int(time.time() - age_seconds)
    (tmp_path / "last-user-activity").write_text(str(epoch))


def _write_override(tmp_path: Path, mode: str, expires_at=None) -> None:
    """Write a presence-mode.json override file."""
    payload = {"mode": mode, "since": "2026-06-26T00:00:00+00:00", "expires_at": expires_at}
    (tmp_path / "presence-mode.json").write_text(json.dumps(payload))


def _setup_presence(mod, tmp_path: Path, monkeypatch) -> None:
    """Point presence module paths at tmp_path."""
    monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
    monkeypatch.setattr(mod, "LAST_USER_ACTIVITY_PATH",
                        str(tmp_path / "last-user-activity"))
    monkeypatch.setattr(mod, "PRESENCE_MODE_PATH",
                        str(tmp_path / "presence-mode.json"))


# ---------------------------------------------------------------------------
# get_mode — derived path
# ---------------------------------------------------------------------------

class TestGetModeDerived:
    def test_user_when_stamp_within_cooldown(self, tmp_path, monkeypatch):
        """Stamp at now-10s (within default 600s cooldown) → mode 'user'."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        _write_stamp(tmp_path, 10)
        mode, bundle = mod.get_mode()
        assert mode == "user"
        assert bundle is mod.MODE_BUNDLES["user"]

    def test_auto_when_stamp_stale(self, tmp_path, monkeypatch):
        """Stamp at now-9999s (far outside cooldown) → mode 'auto'."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        _write_stamp(tmp_path, 9999)
        mode, bundle = mod.get_mode()
        assert mode == "auto"
        assert bundle is mod.MODE_BUNDLES["auto"]

    def test_auto_when_stamp_missing(self, tmp_path, monkeypatch):
        """Missing stamp → fail-open → 'auto'."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        # No stamp written.
        mode, bundle = mod.get_mode()
        assert mode == "auto"

    def test_auto_when_stamp_future_dated(self, tmp_path, monkeypatch):
        """Future-dated stamp → fail-open → 'auto' (never stuck-user)."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        future = int(time.time()) + 9999
        (tmp_path / "last-user-activity").write_text(str(future))
        mode, _ = mod.get_mode()
        assert mode == "auto"

    def test_auto_when_stamp_unparseable(self, tmp_path, monkeypatch):
        """Unparseable stamp → fail-open → 'auto'."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        (tmp_path / "last-user-activity").write_text("not-a-number")
        mode, _ = mod.get_mode()
        assert mode == "auto"

    def test_custom_cooldown_respected(self, tmp_path, monkeypatch):
        """cooldown_seconds=60 → stamp at now-120s → 'auto'."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        cfg = {"cadence": {"cooldown_seconds": 60}}
        (tmp_path / "config.json").write_text(json.dumps(cfg))
        _write_stamp(tmp_path, 120)
        mode, _ = mod.get_mode()
        assert mode == "auto"

    def test_custom_cooldown_within(self, tmp_path, monkeypatch):
        """cooldown_seconds=60 → stamp at now-30s → 'user'."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        cfg = {"cadence": {"cooldown_seconds": 60}}
        (tmp_path / "config.json").write_text(json.dumps(cfg))
        _write_stamp(tmp_path, 30)
        mode, _ = mod.get_mode()
        assert mode == "user"


# ---------------------------------------------------------------------------
# get_mode — override path
# ---------------------------------------------------------------------------

class TestGetModeOverride:
    def test_set_mode_user_wins_over_stale_stamp(self, tmp_path, monkeypatch):
        """Explicit 'user' override wins even when stamp is stale (no auto-derive)."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        _write_stamp(tmp_path, 9999)           # derived would be 'auto'
        mod.set_mode("user")
        mode, bundle = mod.get_mode()
        assert mode == "user"
        assert bundle is mod.MODE_BUNDLES["user"]

    def test_set_mode_auto_wins_over_fresh_stamp(self, tmp_path, monkeypatch):
        """Explicit 'auto' override wins even when stamp is fresh (derived → 'user')."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        _write_stamp(tmp_path, 10)             # derived would be 'user'
        mod.set_mode("auto")
        mode, _ = mod.get_mode()
        assert mode == "auto"

    def test_ttl_override_active_while_fresh(self, tmp_path, monkeypatch):
        """TTL override is respected while within the expiry window."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        mod.set_mode("user", ttl_seconds=60)
        mode, _ = mod.get_mode()
        assert mode == "user"

    def test_ttl_override_expires(self, tmp_path, monkeypatch):
        """set_mode with ttl_seconds=1 → after expiry, reverts to derived."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        _write_stamp(tmp_path, 9999)           # derived would be 'auto'
        mod.set_mode("user", ttl_seconds=1)
        # Verify it's active immediately.
        mode, _ = mod.get_mode()
        assert mode == "user"
        # #4017: advance the clock get_mode reads instead of sleeping on the wall clock.
        real_time = time.time
        monkeypatch.setattr(
            mod, "time", types.SimpleNamespace(time=lambda: real_time() + 5.0)
        )
        mode, _ = mod.get_mode()
        assert mode == "auto"

    def test_override_no_expiry(self, tmp_path, monkeypatch):
        """expires_at=None in override file → permanent (never expires)."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        mod.set_mode("auto")
        # Read the written file to confirm expires_at is null.
        data = json.loads((tmp_path / "presence-mode.json").read_text())
        assert data["expires_at"] is None
        mode, _ = mod.get_mode()
        assert mode == "auto"

    def test_override_file_written_atomically(self, tmp_path, monkeypatch):
        """set_mode uses tmp + os.replace — no .tmp file left behind."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        mod.set_mode("user")
        assert not (tmp_path / "presence-mode.json.tmp").exists()
        assert (tmp_path / "presence-mode.json").exists()


# ---------------------------------------------------------------------------
# clear_mode
# ---------------------------------------------------------------------------

class TestClearMode:
    def test_clear_reverts_to_derived(self, tmp_path, monkeypatch):
        """clear_mode() removes override; get_mode() reverts to stamp-derived result."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        _write_stamp(tmp_path, 9999)           # derived → 'auto'
        mod.set_mode("user")
        assert mod.get_mode()[0] == "user"
        mod.clear_mode()
        assert mod.get_mode()[0] == "auto"

    def test_clear_idempotent_on_missing_file(self, tmp_path, monkeypatch):
        """clear_mode() on a non-existent file is a no-op (no exception)."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        # No override file written.
        mod.clear_mode()   # must not raise
        mod.clear_mode()   # second call also must not raise


# ---------------------------------------------------------------------------
# Fail-open paths
# ---------------------------------------------------------------------------

class TestFailOpen:
    def test_corrupt_override_file(self, tmp_path, monkeypatch):
        """Corrupt presence-mode.json → fail-open → ('auto', ...)."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        (tmp_path / "presence-mode.json").write_text("not valid json {{{")
        mode, bundle = mod.get_mode()
        assert mode == "auto"
        assert bundle is mod.MODE_BUNDLES["auto"]

    def test_override_with_unknown_mode(self, tmp_path, monkeypatch):
        """Override file with unknown mode → fail-open → derived."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        _write_stamp(tmp_path, 9999)
        _write_override(tmp_path, "bogus-mode")
        mode, _ = mod.get_mode()
        assert mode == "auto"

    def test_bad_config_falls_back_to_default_cooldown(self, tmp_path, monkeypatch):
        """Malformed config.json → default cooldown (600s); stamp within default → 'user'."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        (tmp_path / "config.json").write_text("not json {{{")
        _write_stamp(tmp_path, 10)             # 10s < 600s default
        mode, _ = mod.get_mode()
        assert mode == "user"

    def test_missing_config_falls_back_to_default(self, tmp_path, monkeypatch):
        """Missing config.json → default cooldown."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        _write_stamp(tmp_path, 10)
        mode, _ = mod.get_mode()
        assert mode == "user"


# ---------------------------------------------------------------------------
# set_mode validation
# ---------------------------------------------------------------------------

class TestSetModeValidation:
    def test_bogus_mode_raises_value_error(self, tmp_path, monkeypatch):
        """set_mode('bogus') raises ValueError."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="bogus"):
            mod.set_mode("bogus")

    def test_valid_modes_accepted(self, tmp_path, monkeypatch):
        """set_mode accepts all keys in MODE_BUNDLES."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        for mode_name in mod.MODE_BUNDLES:
            mod.set_mode(mode_name)   # must not raise


# ---------------------------------------------------------------------------
# Bundle consistency
# ---------------------------------------------------------------------------

class TestBundleConsistency:
    def test_returned_bundle_is_mode_bundles_entry(self, tmp_path, monkeypatch):
        """get_mode()[1] is MODE_BUNDLES[get_mode()[0]] — the table is the one source."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        # Derived path.
        _write_stamp(tmp_path, 10)
        mode_name, bundle = mod.get_mode()
        assert bundle is mod.MODE_BUNDLES[mode_name]

    def test_override_bundle_is_mode_bundles_entry(self, tmp_path, monkeypatch):
        """Override path: returned bundle still comes from MODE_BUNDLES."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        mod.set_mode("auto")
        mode_name, bundle = mod.get_mode()
        assert mode_name == "auto"
        assert bundle is mod.MODE_BUNDLES["auto"]

    def test_all_bundles_have_required_keys(self):
        """Every MODE_BUNDLES entry has feed, status, loop_suspended."""
        mod = _load_presence()
        for name, bundle in mod.MODE_BUNDLES.items():
            assert "feed" in bundle, f"MODE_BUNDLES[{name!r}] missing 'feed'"
            assert "status" in bundle, f"MODE_BUNDLES[{name!r}] missing 'status'"
            assert "loop_suspended" in bundle, f"MODE_BUNDLES[{name!r}] missing 'loop_suspended'"


# ---------------------------------------------------------------------------
# _read_cooldown_seconds
# ---------------------------------------------------------------------------

class TestReadCooldownSeconds:
    def test_default_when_no_config(self, tmp_path, monkeypatch):
        mod = _load_presence()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        assert mod._read_cooldown_seconds() == mod._DEFAULT_COOLDOWN_SECONDS

    def test_reads_from_config(self, tmp_path, monkeypatch):
        mod = _load_presence()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        cfg = {"cadence": {"cooldown_seconds": 120}}
        (tmp_path / "config.json").write_text(json.dumps(cfg))
        assert mod._read_cooldown_seconds() == 120

    def test_default_on_malformed_config(self, tmp_path, monkeypatch):
        mod = _load_presence()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text("not json {{{")
        assert mod._read_cooldown_seconds() == mod._DEFAULT_COOLDOWN_SECONDS

    def test_default_on_zero_value(self, tmp_path, monkeypatch):
        """Zero is not a valid cooldown — default."""
        mod = _load_presence()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        cfg = {"cadence": {"cooldown_seconds": 0}}
        (tmp_path / "config.json").write_text(json.dumps(cfg))
        assert mod._read_cooldown_seconds() == mod._DEFAULT_COOLDOWN_SECONDS


# ---------------------------------------------------------------------------
# derive_own_status — presence integration
# ---------------------------------------------------------------------------

class TestDeriveOwnStatusPresenceIntegration:
    """Integration tests for the presence check in _status_derive.derive_own_status."""

    def _setup_sd(self, mod, tmp_path: Path, monkeypatch) -> None:
        """Configure _status_derive module paths at tmp_path.

        #1608: _held_baton_turns/_fetch_held_batons now hit the live forum HTTP
        API rather than a local baton-projects directory — mock urlopen to an
        empty project list so these presence-integration tests stay hermetic
        (no real network call, no dependency on a forum server happening to
        be reachable) and so "no baton held" continues to mean what these
        tests assert. #2316 removed the now-dead PROJECTS_DIR constant this
        fixture used to monkeypatch defensively (nothing has read it since
        #1608); nothing here depended on the actual directory existing.
        """
        lm_path = tmp_path / "loop-mode.json"
        activity_path = tmp_path / "last-user-activity"
        monkeypatch.setattr(mod, "LOOP_MODE_PATH", str(lm_path))
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        monkeypatch.setattr(mod, "LAST_USER_ACTIVITY_PATH", str(activity_path))
        monkeypatch.delenv("FORUM_URL", raising=False)

        class _EmptyProjectsResponse:
            def read(self):
                return json.dumps({"projects": []}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        monkeypatch.setattr(
            mod, "forum_urlopen",
            lambda req, timeout=None: _EmptyProjectsResponse(),
        )

    def _inject_presence_mock(self, mode: str, monkeypatch) -> None:
        """Inject a presence mock into sys.modules so the lazy import finds it."""
        presence_mock = types.ModuleType("presence")
        presence_mock.get_mode = lambda: (
            mode,
            {"feed": "direct-only" if mode == "user" else "all",
             "loop_suspended": mode == "user"},
        )
        monkeypatch.setitem(sys.modules, "presence", presence_mock)

    def test_mode_user_yields_engaged(self, tmp_path, monkeypatch):
        """When presence mode is 'user', state is 'engaged'."""
        mod = _load_sd()
        self._setup_sd(mod, tmp_path, monkeypatch)
        self._inject_presence_mock("user", monkeypatch)
        state, _, _, _ = mod.derive_own_status("ariadne")
        assert state == "engaged"

    def test_explicit_override_state_beats_presence_user(self, tmp_path, monkeypatch):
        """override_state wins even when presence mode is 'user'."""
        mod = _load_sd()
        self._setup_sd(mod, tmp_path, monkeypatch)
        self._inject_presence_mock("user", monkeypatch)
        state, _, _, _ = mod.derive_own_status("ariadne", override_state="sleeping")
        assert state == "sleeping"

    def test_mode_auto_uses_existing_fallback_idle(self, tmp_path, monkeypatch):
        """mode 'auto' → existing logic; no loop/baton/stamp → 'idle'."""
        mod = _load_sd()
        self._setup_sd(mod, tmp_path, monkeypatch)
        self._inject_presence_mock("auto", monkeypatch)
        state, _, _, _ = mod.derive_own_status("ariadne")
        assert state == "idle"

    def test_mode_auto_uses_existing_fallback_working(self, tmp_path, monkeypatch):
        """mode 'auto' → existing logic; loop marker present → 'working'."""
        mod = _load_sd()
        self._setup_sd(mod, tmp_path, monkeypatch)
        self._inject_presence_mock("auto", monkeypatch)
        (tmp_path / "loop-mode.json").write_text(json.dumps({"topic": "testing"}))
        state, _, _, _ = mod.derive_own_status("ariadne")
        assert state == "working"

    def test_presence_error_falls_back_to_existing_logic(self, tmp_path, monkeypatch):
        """If presence.get_mode() raises, derive_own_status falls back to existing logic.

        Pins the failure mode via sys.modules injection so the test is robust
        against conftest changes that add tools/ to sys.path — a real presence.py
        would satisfy the import without triggering the except-clause.  A broken
        presence layer must never trap the agent in 'user' state.
        """
        mod = _load_sd()
        self._setup_sd(mod, tmp_path, monkeypatch)
        # Inject a presence stub whose get_mode always raises.  Python's import
        # machinery checks sys.modules before sys.path, so this takes precedence
        # over any real presence.py regardless of sys.path state.
        # monkeypatch restores sys.modules automatically after the test.
        broken_presence = types.ModuleType("presence")

        def _get_mode_raises():
            raise RuntimeError("presence unavailable")

        broken_presence.get_mode = _get_mode_raises
        monkeypatch.setitem(sys.modules, "presence", broken_presence)
        # No loop, no baton, no stamp → existing fallback → idle.
        state, _, _, _ = mod.derive_own_status("ariadne")
        assert state == "idle"


# ---------------------------------------------------------------------------
# CLI — engram-mode
# ---------------------------------------------------------------------------

class TestCLI:
    """Invoke the CLI as a subprocess to test exec-bit and argument handling."""

    def _run(self, *args: str, tmp_engram: Path) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["ENGRAM_HOME"] = str(tmp_engram)
        return subprocess.run(
            [str(CLI_PATH)] + list(args),
            capture_output=True,
            text=True,
            env=env,
        )

    def test_show_json_parses(self, tmp_path):
        """engram-mode show --json produces valid JSON with 'mode' and 'bundle'."""
        result = self._run("show", "--json", tmp_engram=tmp_path)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout.strip())
        assert "mode" in data
        assert "bundle" in data

    def test_user_set_and_clear_round_trip(self, tmp_path):
        """engram-mode user; engram-mode show; engram-mode clear round-trip."""
        # Set user mode.
        r = self._run("user", tmp_engram=tmp_path)
        assert r.returncode == 0
        # Show should reflect user mode.
        r = self._run("show", "--json", tmp_engram=tmp_path)
        data = json.loads(r.stdout.strip())
        assert data["mode"] == "user"
        # Clear.
        r = self._run("clear", tmp_engram=tmp_path)
        assert r.returncode == 0
        # Override file removed; derived from (absent) stamp → auto.
        r = self._run("show", "--json", tmp_engram=tmp_path)
        data = json.loads(r.stdout.strip())
        assert data["mode"] == "auto"

    def test_show_plain_text(self, tmp_path):
        """engram-mode show (no --json) prints 'mode: ...' line."""
        result = self._run("show", tmp_engram=tmp_path)
        assert result.returncode == 0
        assert "mode:" in result.stdout

    def test_exec_bit_bare_invocation(self, tmp_path):
        """CLI is directly executable (exec bit set)."""
        assert os.access(str(CLI_PATH), os.X_OK), (
            f"{CLI_PATH} is not executable; chmod +x was not applied"
        )


# ---------------------------------------------------------------------------
# active_override — new helper (loop-suspend-enactment)
# ---------------------------------------------------------------------------

class TestActiveOverride:
    """Tests for presence.active_override() -> Optional[dict]."""

    def test_returns_dict_for_live_permanent_override(self, tmp_path, monkeypatch):
        """set_mode('user') with no TTL → active_override() returns the dict."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        mod.set_mode("user")
        result = mod.active_override()
        assert result is not None
        assert isinstance(result, dict)
        assert result["mode"] == "user"
        assert result["expires_at"] is None

    def test_returns_dict_for_live_ttl_override(self, tmp_path, monkeypatch):
        """set_mode('user', ttl_seconds=60) → active_override() returns dict while fresh."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        mod.set_mode("user", ttl_seconds=60)
        result = mod.active_override()
        assert result is not None
        assert result["mode"] == "user"
        assert result["expires_at"] is not None

    def test_returns_none_when_no_file(self, tmp_path, monkeypatch):
        """No override file → active_override() returns None."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        # No file written.
        assert mod.active_override() is None

    def test_returns_none_for_expired_override(self, tmp_path, monkeypatch):
        """set_mode('user', ttl_seconds=1) → after expiry active_override() returns None."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        mod.set_mode("user", ttl_seconds=1)
        # Confirm active while fresh.
        assert mod.active_override() is not None
        time.sleep(1.1)
        assert mod.active_override() is None

    def test_returns_none_for_corrupt_file(self, tmp_path, monkeypatch):
        """Corrupt JSON in override file → active_override() returns None (fail-open)."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        (tmp_path / "presence-mode.json").write_text("not valid json {{{")
        assert mod.active_override() is None

    def test_returns_none_for_unknown_mode(self, tmp_path, monkeypatch):
        """Override with unknown mode name → active_override() returns None."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        _write_override(tmp_path, "bogus-mode")
        assert mod.active_override() is None

    def test_auto_override_returns_dict(self, tmp_path, monkeypatch):
        """set_mode('auto') → active_override() returns dict with mode='auto'."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        mod.set_mode("auto")
        result = mod.active_override()
        assert result is not None
        assert result["mode"] == "auto"

    def test_returns_none_after_clear(self, tmp_path, monkeypatch):
        """After clear_mode(), active_override() returns None."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        mod.set_mode("user")
        assert mod.active_override() is not None
        mod.clear_mode()
        assert mod.active_override() is None


# ---------------------------------------------------------------------------
# cooldown_seconds — new public wrapper (loop-suspend-enactment)
# ---------------------------------------------------------------------------

class TestCooldownSeconds:
    """Tests for presence.cooldown_seconds() -> int (public wrapper)."""

    def test_returns_default_when_no_config(self, tmp_path, monkeypatch):
        """No config.json → cooldown_seconds() == _DEFAULT_COOLDOWN_SECONDS."""
        mod = _load_presence()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        assert mod.cooldown_seconds() == mod._DEFAULT_COOLDOWN_SECONDS

    def test_returns_configured_value(self, tmp_path, monkeypatch):
        """config.json with cooldown_seconds=120 → cooldown_seconds() == 120."""
        mod = _load_presence()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text(json.dumps({"cadence": {"cooldown_seconds": 120}}))
        assert mod.cooldown_seconds() == 120

    def test_returns_default_on_bad_config(self, tmp_path, monkeypatch):
        """Malformed config.json → cooldown_seconds() falls back to default."""
        mod = _load_presence()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text("not json {{{")
        assert mod.cooldown_seconds() == mod._DEFAULT_COOLDOWN_SECONDS

    def test_equals_read_cooldown_seconds(self, tmp_path, monkeypatch):
        """cooldown_seconds() == _read_cooldown_seconds() (thin wrapper contract)."""
        mod = _load_presence()
        monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
        # With a config value set.
        (tmp_path / "config.json").write_text(json.dumps({"cadence": {"cooldown_seconds": 300}}))
        assert mod.cooldown_seconds() == mod._read_cooldown_seconds()


# ---------------------------------------------------------------------------
# get_mode regression guard post-refactor (loop-suspend-enactment)
# ---------------------------------------------------------------------------

class TestGetModeRegressionAfterRefactor:
    """Regression guard: get_mode() behavior must be unchanged after the
    active_override() refactor.  These cases mirror TestGetModeDerived and
    TestGetModeOverride; running them in the same session verifies the
    refactored code path produces identical results.
    """

    def test_derived_user_still_works(self, tmp_path, monkeypatch):
        """Derived path: recent stamp → mode 'user'."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        _write_stamp(tmp_path, 10)
        mode, bundle = mod.get_mode()
        assert mode == "user"
        assert bundle is mod.MODE_BUNDLES["user"]

    def test_derived_auto_still_works(self, tmp_path, monkeypatch):
        """Derived path: stale stamp → mode 'auto'."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        _write_stamp(tmp_path, 9999)
        mode, bundle = mod.get_mode()
        assert mode == "auto"
        assert bundle is mod.MODE_BUNDLES["auto"]

    def test_override_user_still_wins_over_stale_stamp(self, tmp_path, monkeypatch):
        """Override path: explicit 'user' wins over stale stamp."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        _write_stamp(tmp_path, 9999)
        mod.set_mode("user")
        mode, bundle = mod.get_mode()
        assert mode == "user"
        assert bundle is mod.MODE_BUNDLES["user"]

    def test_expired_override_reverts_to_derived(self, tmp_path, monkeypatch):
        """Expired override → reverts to derived (stale stamp → 'auto')."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        _write_stamp(tmp_path, 9999)
        mod.set_mode("user", ttl_seconds=1)
        time.sleep(1.1)
        mode, _ = mod.get_mode()
        assert mode == "auto"

    def test_fail_open_on_corrupt_file(self, tmp_path, monkeypatch):
        """Corrupt override → fail-open → derived → missing stamp → 'auto'."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        (tmp_path / "presence-mode.json").write_text("not valid json {{{")
        mode, bundle = mod.get_mode()
        assert mode == "auto"
        assert bundle is mod.MODE_BUNDLES["auto"]


# ---------------------------------------------------------------------------
# get_mode(override=...) — explicit-override param (round-2, reviewer suggestion)
# ---------------------------------------------------------------------------

class TestGetModeOverrideParam:
    """Verify that passing override= to get_mode() avoids re-reading active_override().

    The sentinel (_UNSET) path (no arg) must call active_override() as before;
    the explicit-override path (dict or None) must NOT call active_override().
    """

    def test_get_mode_with_override_dict_uses_dict_mode(self, tmp_path, monkeypatch):
        """get_mode(override=<dict>) uses the dict's mode without reading the file."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        # Track whether active_override was called.
        call_log = []

        def _spy_active_override():
            call_log.append("called")
            return None

        monkeypatch.setattr(mod, "active_override", _spy_active_override)

        override_dict = {"mode": "user", "since": "2026-06-26T00:00:00+00:00", "expires_at": None}
        mode, bundle = mod.get_mode(override=override_dict)

        assert mode == "user"
        assert bundle is mod.MODE_BUNDLES["user"]
        assert call_log == [], "active_override() must NOT be called when override is passed"

    def test_get_mode_with_override_none_goes_to_derived(self, tmp_path, monkeypatch):
        """get_mode(override=None) goes straight to derived path without active_override()."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        call_log = []

        def _spy_active_override():
            call_log.append("called")
            return {"mode": "user", "since": "2026-06-26T00:00:00+00:00", "expires_at": None}

        monkeypatch.setattr(mod, "active_override", _spy_active_override)

        # Stale stamp → derived would be 'auto'.
        _write_stamp(tmp_path, 9999)
        mode, bundle = mod.get_mode(override=None)

        assert mode == "auto"
        assert bundle is mod.MODE_BUNDLES["auto"]
        assert call_log == [], "active_override() must NOT be called when override=None is passed"

    def test_get_mode_default_still_calls_active_override(self, tmp_path, monkeypatch):
        """get_mode() with no arg still calls active_override() (unchanged default behavior)."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        call_log = []

        def _spy_active_override():
            call_log.append("called")
            return None  # no override → falls through to derived

        monkeypatch.setattr(mod, "active_override", _spy_active_override)
        _write_stamp(tmp_path, 9999)  # derived → 'auto'
        mode, _ = mod.get_mode()

        assert mode == "auto"
        assert len(call_log) == 1, "active_override() must be called exactly once for the default path"

    def test_get_mode_with_auto_override_dict(self, tmp_path, monkeypatch):
        """get_mode(override=<auto-dict>) returns ('auto', ...) without file read."""
        mod = _load_presence()
        _setup_presence(mod, tmp_path, monkeypatch)
        call_log = []
        monkeypatch.setattr(mod, "active_override", lambda: call_log.append("called") or None)

        override_dict = {"mode": "auto", "since": "2026-06-26T00:00:00+00:00", "expires_at": None}
        mode, bundle = mod.get_mode(override=override_dict)

        assert mode == "auto"
        assert bundle is mod.MODE_BUNDLES["auto"]
        assert call_log == []
