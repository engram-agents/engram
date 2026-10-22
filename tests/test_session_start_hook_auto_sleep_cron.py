"""Tests for auto_sleep_cron_block() injected by the SessionStart hook.

Verifies:
  1. Returns "" when SESSION_ROLE != "interactive".
  2. Returns "" when cadence.auto_sleep_enabled is False (default).
  3. Returns "" on config read/parse error (silent failure).
  4. Returns nudge string containing "AUTO-SLEEP NIGHTLY" when enabled+interactive.
  5. Nudge contains the correctly interpolated {time} value.
  6. Nudge contains a valid cron expression matching the computed minute/hour.
  7. Jitter is stable — same ENGRAM_HOME always produces the same minute.
  8. Jitter overflow (configured_minute + jitter >= 60) falls back to configured_minute.
  9. Hook integration: cron block appears in startup additionalContext when enabled.
 10. Hook integration: cron block absent from compact/resume source.
"""

import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Module-load helpers (mirror test_session_start_hook_health_probe.py pattern)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"

import pytest  # noqa: E402
# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_hook_module(engram_home: Path, session_role: str = "interactive"):
    """Load the session-start hook with ENGRAM_HOME and SESSION_ROLE set.

    Forces a fresh import each call. Stubs context_tracker so module-level
    import doesn't fail when there is no real baseline DB.
    """
    os.environ["ENGRAM_HOME"] = str(engram_home)
    os.environ["ENGRAM_SESSION_ROLE"] = session_role

    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    sys.modules["context_tracker"] = stub_ct

    for mod_name in list(sys.modules.keys()):
        if mod_name in (
            "engram-session-start-hook",
            "engram_session_start_hook",
        ):
            del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location("engram_session_start_hook", _HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_hook_main(module, hook_input: dict) -> dict:
    """Run module.main() with hook_input on stdin; return parsed output JSON."""
    stdin_data = json.dumps(hook_input)
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with redirect_stdout(buf):
            module.main()
    return json.loads(buf.getvalue().strip())


def _make_engram_home(tmp_path: Path) -> Path:
    """Create a minimal engram home directory with sessions/ subdir."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    (engram_home / "sessions").mkdir()
    return engram_home


def _write_config(engram_home: Path, cadence: dict) -> None:
    """Write a minimal config.json with the given cadence block."""
    config = {"schema_version": 2, "cadence": cadence}
    (engram_home / "config.json").write_text(json.dumps(config))


def _mock_no_engram_client():
    """Patch sys.modules so engram_client import raises (calibration silently skipped)."""
    return patch.dict("sys.modules", {"engram_client": None})


# ---------------------------------------------------------------------------
# Helper: expected jitter for a given ENGRAM_HOME path
# ---------------------------------------------------------------------------

def _expected_jitter(engram_home_str: str) -> int:
    return int(hashlib.sha256(engram_home_str.encode()).hexdigest(), 16) % 10


# ---------------------------------------------------------------------------
# 1. Non-interactive role → returns ""
# ---------------------------------------------------------------------------

def test_non_interactive_role_returns_empty(tmp_path):
    """auto_sleep_cron_block() returns "" when SESSION_ROLE is not interactive."""
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="heartbeat")
    result = mod.auto_sleep_cron_block()
    assert result == "", f"Expected '' for non-interactive role, got: {result!r}"


def test_sleep_role_returns_empty(tmp_path):
    """auto_sleep_cron_block() returns "" when SESSION_ROLE is 'sleep'."""
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="sleep")
    result = mod.auto_sleep_cron_block()
    assert result == ""


# ---------------------------------------------------------------------------
# 2. Disabled (default) → returns ""
# ---------------------------------------------------------------------------

def test_disabled_by_default_returns_empty(tmp_path):
    """Returns "" when cadence.auto_sleep_enabled is missing (defaults to False)."""
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {})  # no auto_sleep_enabled key

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()
    assert result == ""


def test_explicitly_disabled_returns_empty(tmp_path):
    """Returns "" when cadence.auto_sleep_enabled is explicitly False."""
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": False, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()
    assert result == ""


# ---------------------------------------------------------------------------
# 3. Config read/parse errors → returns ""
# ---------------------------------------------------------------------------

def test_missing_config_returns_empty(tmp_path):
    """Returns "" silently when config.json does not exist."""
    engram_home = _make_engram_home(tmp_path)
    # No config.json written.

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()
    assert result == ""


def test_malformed_config_returns_empty(tmp_path):
    """Returns "" silently when config.json is not valid JSON."""
    engram_home = _make_engram_home(tmp_path)
    (engram_home / "config.json").write_text("{ bad json }")

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()
    assert result == ""


def test_non_dict_config_shapes_return_empty(tmp_path):
    """Valid-JSON-but-non-dict shapes must return "" without raising — the hook
    must never break session start. (#335 review: config.get()/
    cadence.get() raise AttributeError on these, which the prior
    JSONDecodeError-only guard did not catch.)"""
    engram_home = _make_engram_home(tmp_path)
    mod = _load_hook_module(engram_home, session_role="interactive")
    for raw in ["null", "[1, 2, 3]", '"a string"', "42", '{"cadence": null}']:
        (engram_home / "config.json").write_text(raw)
        result = mod.auto_sleep_cron_block()
        assert result == "", f"non-dict config {raw!r} should return '' (got {result!r})"


def test_malformed_time_returns_empty(tmp_path):
    """Returns "" silently when auto_sleep_time cannot be parsed as HH:MM."""
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "not-a-time"})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()
    assert result == ""


# ---------------------------------------------------------------------------
# 4. Enabled + interactive → nudge contains "AUTO-SLEEP NIGHTLY"
# ---------------------------------------------------------------------------

def test_enabled_interactive_contains_auto_sleep_nightly(tmp_path):
    """Returns a non-empty string containing 'AUTO-SLEEP NIGHTLY' when enabled+interactive."""
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()
    assert result != ""
    assert "AUTO-SLEEP NIGHTLY" in result, f"Expected 'AUTO-SLEEP NIGHTLY' in nudge: {result!r}"


# ---------------------------------------------------------------------------
# 5. Nudge contains the correctly interpolated {time}
# ---------------------------------------------------------------------------

def test_nudge_contains_configured_time(tmp_path):
    """The nudge output contains the user-configured auto_sleep_time string."""
    engram_home = _make_engram_home(tmp_path)
    configured_time = "02:30"
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": configured_time})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()
    assert configured_time in result, (
        f"Expected configured time '{configured_time}' in nudge: {result!r}"
    )


def test_nudge_time_appears_multiple_times(tmp_path):
    """auto_sleep_time appears multiple times in the nudge (header + user message)."""
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "01:15"})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()
    assert result.count("01:15") >= 2, (
        f"Expected '01:15' at least twice in nudge, got {result.count('01:15')}"
    )


# ---------------------------------------------------------------------------
# 6. Nudge contains a valid cron expression
# ---------------------------------------------------------------------------

def test_nudge_contains_cron_expr(tmp_path):
    """The nudge string contains a cron expression matching the expected pattern."""
    import re as _re
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()

    # Should contain something like "5 3 * * *" (minute hour * * *)
    cron_pattern = _re.compile(r'\d{1,2} \d{1,2} \* \* \*')
    assert cron_pattern.search(result), (
        f"Expected cron expression pattern in nudge: {result!r}"
    )


def test_cron_expr_minute_is_computed_correctly(tmp_path):
    """The cron minute equals (configured_minute + jitter) when sum < 60."""
    engram_home = _make_engram_home(tmp_path)
    configured_minute = 0
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()

    jitter = _expected_jitter(str(engram_home))
    expected_minute = configured_minute + jitter  # sum is 0–9, always < 60
    expected_cron = f"{expected_minute} 3 * * *"
    assert expected_cron in result, (
        f"Expected cron '{expected_cron}' in nudge, got: {result!r}"
    )


def test_cron_expr_hour_matches_configured_hour(tmp_path):
    """The cron hour matches the hour from auto_sleep_time."""
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "22:00"})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()

    # Hour should be 22 in the cron expression
    import re as _re
    match = _re.search(r'(\d{1,2}) 22 \* \* \*', result)
    assert match, f"Expected hour=22 in cron expression in nudge: {result!r}"


# ---------------------------------------------------------------------------
# 7. Jitter stability — same ENGRAM_HOME → same minute
# ---------------------------------------------------------------------------

def test_jitter_is_stable_across_calls(tmp_path):
    """Multiple calls with the same ENGRAM_HOME produce the same cron minute."""
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result1 = mod.auto_sleep_cron_block()
    result2 = mod.auto_sleep_cron_block()
    assert result1 == result2, "Jitter should be deterministic — same result each call"


def test_jitter_deterministic_value(tmp_path):
    """Jitter value is exactly int(sha256(ENGRAM_HOME).hexdigest(), 16) % 10."""
    engram_home = _make_engram_home(tmp_path)
    # Use a minute where jitter cannot overflow (minute=0 → sum 0–9 < 60)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()

    jitter = _expected_jitter(str(engram_home))
    expected_minute = 0 + jitter
    assert f"{expected_minute} 3 * * *" in result, (
        f"Expected jitter={jitter} → minute={expected_minute} in cron, got: {result!r}"
    )


# ---------------------------------------------------------------------------
# 8. Jitter overflow → falls back to configured_minute
# ---------------------------------------------------------------------------

def test_jitter_overflow_falls_back_to_configured_minute(tmp_path):
    """When configured_minute + jitter >= 60, minute falls back to configured_minute."""
    engram_home = _make_engram_home(tmp_path)
    # Use minute=55; even jitter=5 would overflow (55+5=60 >=60).
    # We need to pick a minute where configured_minute + jitter >= 60.
    # Jitter for this path is deterministic, so find a minute that overflows.
    jitter = _expected_jitter(str(engram_home))
    # Pick configured_minute such that configured_minute + jitter >= 60
    configured_minute = 60 - jitter  # exactly at boundary (>=60 triggers fallback)
    if configured_minute >= 60:
        configured_minute = 59  # fallback: 59 + any jitter >= 1 overflows

    time_str = f"02:{configured_minute:02d}"
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": time_str})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()

    # When overflow: minute should be configured_minute (not configured_minute + jitter)
    if configured_minute + jitter >= 60:
        expected_cron = f"{configured_minute} 2 * * *"
        assert expected_cron in result, (
            f"Expected fallback to configured_minute={configured_minute} on overflow, "
            f"got: {result!r}"
        )


def test_jitter_overflow_not_uses_wrapped_minute(tmp_path):
    """Overflow does NOT produce a wrapped (mod 60) or hour-carry minute."""
    engram_home = _make_engram_home(tmp_path)
    jitter = _expected_jitter(str(engram_home))
    configured_minute = 60 - jitter  # at boundary or needs adjustment
    if configured_minute >= 60:
        configured_minute = 59

    time_str = f"04:{configured_minute:02d}"
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": time_str})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()

    if configured_minute + jitter >= 60:
        # The wrapped minute (configured_minute + jitter) % 60 must NOT appear
        wrapped = (configured_minute + jitter) % 60
        # Only check if wrapped != configured_minute (otherwise assertion is vacuous)
        if wrapped != configured_minute:
            import re as _re
            bad_cron = f"{wrapped} 4 * * *"
            assert bad_cron not in result, (
                f"Overflow should not produce wrapped minute {wrapped}, got: {result!r}"
            )


# ---------------------------------------------------------------------------
# 9. Hook integration: cron block appears in startup additionalContext
# ---------------------------------------------------------------------------

def _parse_trailer(ctx: str):
    """Parse emit_context's line-1 trailer -> (shown, cut, overflow_path).

    The trailer always reads '▸ N shown · K cut', even when K == 0, so a bare
    'cut' substring proves nothing; the count and the named file are the facts.
    """
    import re as _re
    first_line = ctx.splitlines()[0]
    m = _re.match(
        r"▸ (\d+) shown · (\d+) cut(?: · head trimmed)?(?: — see (.+))?$", first_line
    )
    assert m, f"unparseable trailer: {first_line!r}"
    path = m.group(3)
    assert int(m.group(2)) == 0 or path, f"cut>0 but no overflow path: {first_line!r}"
    return int(m.group(1)), int(m.group(2)), path


def test_integration_cron_block_in_startup_context(tmp_path):
    """When enabled+interactive+startup, cron block appears in additionalContext."""
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="interactive")

    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            hook_input = {
                "session_id": "test-auto-sleep-startup",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "startup",
            }
            result = _run_hook_main(mod, hook_input)

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "AUTO-SLEEP NIGHTLY" in ctx, (
        f"Expected 'AUTO-SLEEP NIGHTLY' in additionalContext: {ctx!r}"
    )
    # The full block is _P_REST: it is either visible, or (when the injection
    # overflows -- e.g. a long pytest --basetemp inflates the overflow pointer,
    # #3336) cut, in which case the must-not-miss line must still be visible
    # and the overflow file named on line 1 must actually hold the full block.
    assert mod.auto_sleep_must_line() in ctx
    if "[Auto-sleep enabled]" not in ctx:
        shown, cut, path = _parse_trailer(ctx)
        assert cut >= 1, f"full block absent but trailer reports cut={cut}"
        with open(path, encoding="utf-8") as fh:
            overflow_text = fh.read()
        assert "[Auto-sleep enabled]" in overflow_text, (
            f"cut full block not recoverable from overflow file {path}"
        )


# ---------------------------------------------------------------------------
# 10. Hook integration: cron block absent from compact/resume source
# ---------------------------------------------------------------------------

def test_integration_cron_block_absent_on_compact(tmp_path):
    """Cron block is NOT injected for source=compact (only source=startup)."""
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="interactive")

    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            hook_input = {
                "session_id": "test-auto-sleep-compact",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "compact",
            }
            result = _run_hook_main(mod, hook_input)

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "AUTO-SLEEP NIGHTLY" not in ctx, (
        f"Cron block should NOT appear for source=compact, got: {ctx!r}"
    )


def test_integration_cron_block_absent_when_disabled(tmp_path):
    """Cron block is NOT injected when auto_sleep_enabled is False."""
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": False, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="interactive")

    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            hook_input = {
                "session_id": "test-auto-sleep-disabled",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "startup",
            }
            result = _run_hook_main(mod, hook_input)

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "AUTO-SLEEP NIGHTLY" not in ctx


# ---------------------------------------------------------------------------
# Round-2 blocker/suggestion fixes
# ---------------------------------------------------------------------------

def test_auto_sleep_time_null_returns_empty(tmp_path):
    """BLOCKER fix: auto_sleep_time=null (JSON null → Python None) returns "" without raising.

    cadence.get("auto_sleep_time", "03:00") only returns the default when the key
    is ABSENT. A present null becomes None, which would raise AttributeError on
    .split(). The isinstance guard must catch this and return "".
    """
    engram_home = _make_engram_home(tmp_path)
    # Write config with auto_sleep_time explicitly set to JSON null.
    config = {"schema_version": 2, "cadence": {"auto_sleep_enabled": True, "auto_sleep_time": None}}
    (engram_home / "config.json").write_text(json.dumps(config))

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()
    assert result == "", f"Expected '' for auto_sleep_time=null, got: {result!r}"


def test_auto_sleep_time_int_returns_empty(tmp_path):
    """BLOCKER fix: auto_sleep_time=300 (an int) returns "" without raising.

    An integer value also makes .split() raise AttributeError. The isinstance
    guard must treat it as invalid and return "".
    """
    engram_home = _make_engram_home(tmp_path)
    config = {"schema_version": 2, "cadence": {"auto_sleep_enabled": True, "auto_sleep_time": 300}}
    (engram_home / "config.json").write_text(json.dumps(config))

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()
    assert result == "", f"Expected '' for auto_sleep_time=int, got: {result!r}"


def test_auto_sleep_time_out_of_range_returns_empty(tmp_path):
    """SUGGESTION fix: auto_sleep_time='99:99' passes int() but must return "".

    "99:99" yields hour=99, minute=99 — both out of valid range. The range
    check (0 <= hour <= 23 and 0 <= configured_minute <= 59) must catch this
    before a silently-invalid cron expression is handed to the agent.
    """
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "99:99"})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()
    assert result == "", f"Expected '' for out-of-range '99:99', got: {result!r}"


# ---------------------------------------------------------------------------
# 11. Cron prompt template contains stale-lock detection phrases
# ---------------------------------------------------------------------------

def test_cron_prompt_contains_mtime_check(tmp_path):
    """Cron prompt instructs the cron-fired session to check the lock file mtime."""
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()

    assert "check its mtime" in result, (
        f"Expected 'check its mtime' in cron prompt for freshness gate: {result!r}"
    )


def test_cron_prompt_contains_2_hours_threshold(tmp_path):
    """Cron prompt specifies the 2-hour freshness threshold for stale-lock detection."""
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()

    assert "2 hours" in result, (
        f"Expected '2 hours' threshold in cron prompt: {result!r}"
    )


def test_cron_prompt_contains_stale_lock(tmp_path):
    """Cron prompt uses the phrase 'stale lock' to name the recovery-path condition."""
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()

    assert "stale lock" in result, (
        f"Expected 'stale lock' in cron prompt: {result!r}"
    )


def test_cron_prompt_contains_recovered_from_stale_lock(tmp_path):
    """Cron prompt instructs logging 'recovered from stale lock' on the recovery path."""
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()

    assert "recovered from stale lock" in result, (
        f"Expected 'recovered from stale lock' in cron prompt: {result!r}"
    )


def test_cron_prompt_still_contains_auto_sleep_nightly(tmp_path):
    """Regression: stale-lock fix does not remove the AUTO-SLEEP NIGHTLY marker."""
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()

    assert "AUTO-SLEEP NIGHTLY" in result, (
        f"Expected 'AUTO-SLEEP NIGHTLY' still present after stale-lock fix: {result!r}"
    )


def test_cron_prompt_still_contains_last_sleep_success(tmp_path):
    """Regression: stale-lock fix does not remove last-sleep-success.json reference."""
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()

    assert "last-sleep-success.json" in result, (
        f"Expected 'last-sleep-success.json' still present after stale-lock fix: {result!r}"
    )


def test_cron_prompt_no_spurious_backslash_escapes(tmp_path):
    """Verify the emitted cron prompt has no literal-backslash-before-apostrophe sequences.

    Regression guard for B1 (PR #428 round-2): the template previously emitted
    session\\'s (backslash + apostrophe) due to double-quoted string escaping.
    """
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="interactive")
    result = mod.auto_sleep_cron_block()

    # The two-char sequence \' (backslash + apostrophe) must NOT appear.
    assert "\\'" not in result, (
        f"Found literal-backslash-before-apostrophe in prompt: {result!r}"
    )
    # Explicitly verify the apostrophes that previously regressed:
    assert "session's" in result, (
        f"Expected 'session\\'s' (no backslash) in prompt: {result!r}"
    )
    assert "tonight's" in result, (
        f"Expected 'tonight\\'s' (no backslash) in prompt: {result!r}"
    )


def test_encode_with_errors_replace_does_not_raise(tmp_path):
    """BLOCKER fix: jitter hash encode uses errors='replace', not strict.

    Simulate a surrogateescape-laden path by patching ENGRAM_HOME on the module
    after load. The .encode("utf-8", errors="replace") path must not raise even
    for strings that would fail strict UTF-8 encoding. We construct a string
    containing a lone surrogate (\\udcff) to trigger the difference.
    """
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})

    mod = _load_hook_module(engram_home, session_role="interactive")

    # Inject a surrogate-containing path directly on the loaded module so the
    # hash line exercises errors="replace" rather than the default strict encoder.
    surrogate_path = str(engram_home) + "\udcff"
    original_home = mod.ENGRAM_HOME
    mod.ENGRAM_HOME = surrogate_path
    try:
        # Must not raise UnicodeEncodeError (or any exception).
        result = mod.auto_sleep_cron_block()
        # Result should still be a string (non-empty because config is valid).
        assert isinstance(result, str), f"Expected str result, got: {type(result)}"
    finally:
        mod.ENGRAM_HOME = original_home


# ---------------------------------------------------------------------------
# #3336 -- the cron instruction must survive injection overflow
#
# The full [Auto-sleep enabled] block is _P_REST, so when the 9,500-char
# budget overflows (a long ENGRAM_HOME inflates the overflow pointer; a big
# block does the same) it is cut, and the seat silently never registers
# its nightly sleep cron. A one-line must-not-miss pointer at _P_MUST keeps
# the instruction visible and names the overflow file holding the full text.
# ---------------------------------------------------------------------------

_MUST_TOKEN = "AUTO-SLEEP NIGHTLY"
_FULL_BLOCK_MARK = "[Auto-sleep enabled]"  # must NOT appear in the short must line
# A phrase that occurs once, inside the full cron prompt, and nowhere in the
# short must line -- counting it counts copies of the full prompt.
_FULL_PROMPT_ONLY = "recovered from stale lock"


def _long_engram_home(tmp_path: Path, min_len: int = 300) -> Path:
    """Nested engram home whose absolute path is >= min_len chars."""
    base = tmp_path
    i = 0
    while len(str(base / "engram")) < min_len:
        base = base / f"nested-dir-segment-{i:02d}-padding-padding"
        i += 1
    base.mkdir(parents=True)
    return _make_engram_home(base)


def _run_startup(mod, tmp_path: Path, name: str) -> str:
    with _mock_no_engram_client():
        with patch.object(mod, "_check_mcp_health", return_value=(True, None)):
            result = _run_hook_main(mod, {
                "session_id": name,
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "startup",
            })
    return result["hookSpecificOutput"]["additionalContext"]


def test_must_line_survives_overflow_when_enabled(tmp_path):
    """Cell 1: enabled + forced overflow -> must line (token + CronList) present."""
    engram_home = _long_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})
    mod = _load_hook_module(engram_home, session_role="interactive")
    # Force overflow: an earlier _P_REST block outranks the cron block regardless of the path length alone.
    mod.sleep_status_block = lambda: "SLEEP-FILLER-" + ("a" * 9_000)

    ctx = _run_startup(mod, tmp_path, "test-3336-overflow")

    # Precondition: overflow really cut the full block (else test is vacuous).
    assert _FULL_BLOCK_MARK not in ctx, "fixture did not overflow the full block"
    assert _MUST_TOKEN in ctx, f"must line dropped under overflow: {ctx!r}"
    assert "CronList" in ctx, f"CronList instruction dropped under overflow: {ctx!r}"
    # Pin the pointer's promise: the file named on line 1 holds the full prompt.
    _shown, cut, path = _parse_trailer(ctx)
    assert cut >= 1 and path
    with open(path, encoding="utf-8") as fh:
        overflow_text = fh.read()
    assert _FULL_BLOCK_MARK in overflow_text
    assert "AUTO-SLEEP CRON-PROMPT to register" in overflow_text
    assert _FULL_PROMPT_ONLY in overflow_text


def test_must_line_absent_when_disabled(tmp_path):
    """Cell 2: disabled -> no must line, no noise (also under overflow)."""
    engram_home = _long_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": False, "auto_sleep_time": "03:00"})
    mod = _load_hook_module(engram_home, session_role="interactive")
    mod.sleep_status_block = lambda: "SLEEP-FILLER-" + ("a" * 9_000)

    ctx = _run_startup(mod, tmp_path, "test-3336-disabled")
    assert _MUST_TOKEN not in ctx
    assert "CronList" not in ctx


def test_must_line_helper_empty_when_disabled_or_noninteractive(tmp_path):
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": False})
    mod = _load_hook_module(engram_home, session_role="interactive")
    assert mod.auto_sleep_must_line() == ""

    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "99:99"})
    assert mod.auto_sleep_must_line() == "", "invalid time must disable like the block"

    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})
    mod = _load_hook_module(engram_home, session_role="heartbeat")
    assert mod.auto_sleep_must_line() == ""


def test_must_line_is_short_and_references_not_duplicates(tmp_path):
    engram_home = _make_engram_home(tmp_path)
    _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})
    mod = _load_hook_module(engram_home, session_role="interactive")
    line = mod.auto_sleep_must_line()
    assert _MUST_TOKEN in line and "CronList" in line
    assert "\n" not in line
    assert len(line) <= 300, f"must line too long ({len(line)})"
    assert _FULL_PROMPT_ONLY not in line
    # Existing integration tests key on the full block's header; the must line
    # must not satisfy them by itself.
    assert _FULL_BLOCK_MARK not in line


def test_no_overflow_has_must_line_and_single_full_prompt():
    """Cell 3: no overflow -> both present; full prompt appears exactly once.

    The no-overflow premise must not depend on pytest's basetemp length (a long
    tmp path inflates the marker line and overflow pointer and tips the
    injection over budget), so this cell uses its own short mkdtemp home and
    asserts the premise explicitly.
    """
    import shutil
    short = Path(tempfile.mkdtemp(prefix="e"))
    try:
        engram_home = _make_engram_home(short)
        _write_config(engram_home, {"auto_sleep_enabled": True, "auto_sleep_time": "03:00"})
        mod = _load_hook_module(engram_home, session_role="interactive")
        # A dev/source checkout has no baked harness, so detect_harness() is
        # None and the hook runs at the 4,000-char unmeasured floor, where the
        # ~2.6K cron block alone squeezes other blocks out. Pin the measured
        # claude-code budget (same as test_session_start_hook_injection_bounds)
        # so the premise holds on every host.
        mod.detect_harness = lambda: "claude-code"
        must = mod.auto_sleep_must_line()

        ctx = _run_startup(mod, short, "test-3336-no-overflow")

        # Premise, stated explicitly. The hook cuts the full tool-capability
        # index to its overflow file by design (#3217), so raw `cut == 0` is
        # unreachable; the premise is that auto-sleep content adds no cuts, i.e.
        # the cut count equals the auto-sleep-disabled baseline on the same
        # fixture.
        _shown, cut, _path = _parse_trailer(ctx)
        _write_config(engram_home, {"auto_sleep_enabled": False})
        _s, baseline_cut, _p = _parse_trailer(
            _run_startup(mod, short, "test-3336-no-overflow-baseline")
        )
    finally:
        shutil.rmtree(short, ignore_errors=True)
    assert cut == baseline_cut, (
        f"premise broken: auto-sleep content was cut (cut={cut}, "
        f"auto-sleep-off baseline={baseline_cut}); cell is not no-overflow"
    )
    assert must and must in ctx
    assert _FULL_BLOCK_MARK in ctx
    assert ctx.count(_FULL_PROMPT_ONLY) == 1, "full cron prompt must not be duplicated"
    assert ctx.index(must) < ctx.index(_FULL_BLOCK_MARK), "must line precedes body"
