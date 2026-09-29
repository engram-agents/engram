"""Tests for the calibration anchor block injected by the SessionStart hook.

PR-4: engram_stats(sections=["confidence"]) + engram_stats(mode="7-turn",
sections=["confidence"]) data is rendered into a compact [ENGRAM Calibration]
block and appended to SessionStart additionalContext on every wake (both
startup and compact/resume).

Three test families:
  1. format_calibration_block: unit tests on the formatting helper.
  2. Hook integration: mock engram_stats to assert block appears in output.
  3. Resilience: mock engram_stats to raise; assert hook still completes
     successfully without the calibration block.
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"


def _load_hook_module(engram_home: Path):
    """Load the session-start hook with ENGRAM_HOME pointing at engram_home.

    Returns the module. Forces a fresh import each call to pick up env overrides.
    Forces a stub for context_tracker.write_baseline so the module-level import
    doesn't fail when there is no real baseline DB.
    """
    os.environ["ENGRAM_HOME"] = str(engram_home)
    # Stub context_tracker so the import at the top of the hook doesn't fail.
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
    # engram-alpha#127: pin detect_harness() to "claude-code" (measured
    # budget=9,500) -- this dev/source checkout has no baked platform.json
    # (see _harness.py's own docstring), so the hook's own detect_harness()
    # would otherwise return None and get_harness_budgets(None) would fall
    # through to UNMEASURED_HARNESS_FLOOR (4,000), which is smaller than
    # this file's fixtures were written against. Same discipline as
    # tests/test_lesson_tripwire_injection_bounds.py's own
    # _load_harness_budgets() docstring.
    module.detect_harness = lambda: "claude-code"
    # engram-alpha#127: stub out the tool-capability-index block so these
    # tests are not incidentally coupled to the REAL checked-in
    # tool_capability_index.txt artifact's size (measured 11,049 bytes on
    # this checkout -- alone bigger than the hook's own 9,500-char TOTAL
    # budget, which would starve every lower-priority block sorted after it,
    # including the calibration block this file actually tests).
    module.tool_capability_index_block = lambda: ""
    return module


# ---------------------------------------------------------------------------
# Canned distribution data
# ---------------------------------------------------------------------------

_CANNED_CONF_ALL = {
    "by_type": {
        "observation_factual": {"n": 200, "mean": 0.72, "p25": 0.65, "p50": 0.72, "p75": 0.80, "p90": 0.88, "p95": 0.92},
        "derivation":          {"n":  80, "mean": 0.68, "p25": 0.60, "p50": 0.68, "p75": 0.75, "p90": 0.82, "p95": 0.88},
        "conjecture":          {"n":  30, "mean": 0.45, "p25": 0.35, "p50": 0.45, "p75": 0.55, "p90": 0.58, "p95": 0.60},
        "lesson":              {"n":  15, "mean": 0.78, "p25": 0.70, "p50": 0.78, "p75": 0.85, "p90": 0.90, "p95": 0.92},
        "axiom":               {"n":   5, "mean": 1.00, "p25": 1.00, "p50": 1.00, "p75": 1.00, "p90": 1.00, "p95": 1.00},
    },
    "by_quote_type": {"hard_data": {"n": 120, "mean": 0.82}, "editorial": {"n": 80, "mean": 0.62}},
    "by_reasoning_type": {"deductive_modus_ponens": {"n": 40, "mean": 0.88}},
    "by_source_class": {"external": {"n": 280, "mean": 0.71}},
}

_CANNED_CONF_7D = {
    "by_type": {
        "observation_factual": {"n": 20, "mean": 0.74, "p25": 0.68, "p50": 0.74, "p75": 0.81, "p90": 0.89, "p95": 0.93},
        "derivation":          {"n":  8, "mean": 0.70, "p25": 0.62, "p50": 0.70, "p75": 0.77, "p90": 0.83, "p95": 0.89},
    },
    "by_quote_type": {},
    "by_reasoning_type": {},
    "by_source_class": {},
}


# ---------------------------------------------------------------------------
# 1. format_calibration_block unit tests
# ---------------------------------------------------------------------------

class TestFormatCalibrationBlock:
    """Unit tests for the format_calibration_block helper."""

    def _module(self):
        with tempfile.TemporaryDirectory() as tmp:
            engram_home = Path(tmp) / "engram"
            engram_home.mkdir()
            return _load_hook_module(engram_home)

    def test_block_starts_with_calibration_header(self):
        mod = self._module()
        result = mod.format_calibration_block(_CANNED_CONF_ALL, _CANNED_CONF_7D, 42)
        assert result.startswith("[ENGRAM Calibration: graph distribution at turn 42]")

    def test_block_line_count_at_most_10(self):
        mod = self._module()
        result = mod.format_calibration_block(_CANNED_CONF_ALL, _CANNED_CONF_7D, 10)
        lines = result.split("\n")
        assert len(lines) <= 10, f"Expected ≤10 lines, got {len(lines)}: {result}"

    def test_block_contains_per_type_rows(self):
        mod = self._module()
        result = mod.format_calibration_block(_CANNED_CONF_ALL, _CANNED_CONF_7D, 5)
        assert "observations " in result
        assert "derivations  " in result
        assert "conjectures  " in result

    def test_block_contains_all_claims_row(self):
        mod = self._module()
        result = mod.format_calibration_block(_CANNED_CONF_ALL, _CANNED_CONF_7D, 5)
        assert "All claims" in result

    def test_block_contains_7d_rolling_line(self):
        mod = self._module()
        result = mod.format_calibration_block(_CANNED_CONF_ALL, _CANNED_CONF_7D, 5)
        assert "7-turn rolling" in result

    def test_empty_by_type_returns_empty_string(self):
        mod = self._module()
        result = mod.format_calibration_block({}, {}, 5)
        assert result == ""

    def test_empty_by_type_in_conf_all_returns_empty_string(self):
        mod = self._module()
        result = mod.format_calibration_block({"by_type": {}}, {}, 5)
        assert result == ""

    def test_no_7d_data_omits_rolling_line(self):
        mod = self._module()
        result = mod.format_calibration_block(_CANNED_CONF_ALL, {"by_type": {}}, 5)
        assert "7-turn rolling" not in result

    def test_block_line_count_with_minimal_data(self):
        """Single type → 3 lines (header + all_claims + one type row)."""
        mod = self._module()
        minimal = {
            "by_type": {
                "observation_factual": {"n": 10, "mean": 0.70, "p25": 0.60, "p50": 0.70, "p75": 0.80, "p90": 0.88, "p95": 0.90},
            }
        }
        result = mod.format_calibration_block(minimal, {}, 1)
        lines = result.split("\n")
        assert 2 <= len(lines) <= 10

    def test_turn_number_appears_in_header(self):
        mod = self._module()
        result = mod.format_calibration_block(_CANNED_CONF_ALL, _CANNED_CONF_7D, 99)
        assert "turn 99" in result


# ---------------------------------------------------------------------------
# 2. Hook integration: calibration block appears in output
# ---------------------------------------------------------------------------

def _make_mock_engram_client(conf_all_response: dict, conf_7d_response: dict):
    """Return a mock EngramClient whose call() returns canned stats."""
    mock_client = MagicMock()

    def _call_side_effect(tool_name, arguments=None):
        if tool_name != "engram_stats":
            return {}
        try:
            params = json.loads((arguments or {}).get("payload_json", "{}"))
        except (json.JSONDecodeError, AttributeError):
            params = {}
        mode = params.get("mode", "all")
        if mode == "7-turn":
            return {"confidence": conf_7d_response}
        return {
            "confidence": conf_all_response,
            "memory": {"current_turn": 7, "tier_sizes": {}},
        }

    mock_client.call.side_effect = _call_side_effect
    return mock_client


def _run_hook_main(module, hook_input: dict) -> dict:
    """Run module.main() with hook_input on stdin; return parsed output JSON."""
    import io
    from contextlib import redirect_stdout

    stdin_data = json.dumps(hook_input)
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with redirect_stdout(buf):
            module.main()
    return json.loads(buf.getvalue().strip())


def test_calibration_block_present_on_startup(tmp_path):
    """Calibration block appears in additionalContext for source=startup."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    (engram_home / "sessions").mkdir()

    mod = _load_hook_module(engram_home)

    mock_client_instance = _make_mock_engram_client(_CANNED_CONF_ALL, _CANNED_CONF_7D)
    mock_client_class = MagicMock(return_value=mock_client_instance)

    with patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=mock_client_class)}):
        hook_input = {
            "session_id": "test-session-startup",
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "source": "startup",
        }
        result = _run_hook_main(mod, hook_input)

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "[ENGRAM Calibration:" in ctx


def test_calibration_block_present_on_compact(tmp_path):
    """Calibration block appears in additionalContext for source=compact."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    (engram_home / "sessions").mkdir()

    mod = _load_hook_module(engram_home)

    mock_client_instance = _make_mock_engram_client(_CANNED_CONF_ALL, _CANNED_CONF_7D)
    mock_client_class = MagicMock(return_value=mock_client_instance)

    with patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=mock_client_class)}):
        hook_input = {
            "session_id": "test-session-compact",
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "source": "compact",
        }
        result = _run_hook_main(mod, hook_input)

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "[ENGRAM Calibration:" in ctx


def test_calibration_block_absent_on_stats_failure(tmp_path):
    """If engram_stats raises, hook completes successfully and omits calibration block."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    (engram_home / "sessions").mkdir()

    mod = _load_hook_module(engram_home)

    mock_client_instance = MagicMock()
    mock_client_instance.call.side_effect = RuntimeError("DB not available")
    mock_client_class = MagicMock(return_value=mock_client_instance)

    with patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=mock_client_class)}):
        hook_input = {
            "session_id": "test-session-fail",
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "source": "startup",
        }
        result = _run_hook_main(mod, hook_input)

    # Hook must still produce valid JSON output.
    assert "hookSpecificOutput" in result
    ctx = result["hookSpecificOutput"]["additionalContext"]
    # Calibration block must NOT appear.
    assert "[ENGRAM Calibration:" not in ctx


def test_calibration_block_absent_on_import_failure(tmp_path):
    """If engram_client import raises, hook completes without calibration block."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    (engram_home / "sessions").mkdir()

    mod = _load_hook_module(engram_home)

    with patch.dict("sys.modules", {"engram_client": None}):
        hook_input = {
            "session_id": "test-session-import-fail",
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "source": "startup",
        }
        result = _run_hook_main(mod, hook_input)

    assert "hookSpecificOutput" in result
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "[ENGRAM Calibration:" not in ctx


def test_calibration_block_format_in_context(tmp_path):
    """Calibration block has per-type rows and ≤10 lines."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    (engram_home / "sessions").mkdir()

    mod = _load_hook_module(engram_home)

    mock_client_instance = _make_mock_engram_client(_CANNED_CONF_ALL, _CANNED_CONF_7D)
    mock_client_class = MagicMock(return_value=mock_client_instance)

    with patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=mock_client_class)}):
        hook_input = {
            "session_id": "test-session-format",
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "source": "startup",
        }
        result = _run_hook_main(mod, hook_input)

    ctx = result["hookSpecificOutput"]["additionalContext"]
    # Extract the calibration block: starts at "[ENGRAM Calibration:" and is
    # capped at 10 lines by format_calibration_block(). Later blocks (e.g. the
    # substrate-health probe warning) may follow without a "[" prefix, so we
    # extract exactly the first 10 lines from cal_start rather than searching
    # for the next "[".
    cal_start = ctx.find("[ENGRAM Calibration:")
    assert cal_start >= 0
    remaining = ctx[cal_start:]
    all_remaining_lines = remaining.split("\n")
    cal_lines = all_remaining_lines[:10]

    # The [:10] slice above makes this assert a tautology (len is always ≤ 10
    # after slicing). The actual ≤10-line cap is verified in the unit test
    # TestFormatCalibrationBlock.test_block_line_count_at_most_10. The assert
    # is kept here to document the intent: the calibration block must not
    # overflow into adjacent context sections.
    assert len(cal_lines) <= 10, f"Calibration block has {len(cal_lines)} lines: {cal_lines}"
    assert cal_lines[0].startswith("[ENGRAM Calibration:")
    assert any("observations" in ln for ln in cal_lines)
    assert any("derivations" in ln for ln in cal_lines)


def test_calibration_block_present_for_non_interactive_role(tmp_path):
    """Calibration block fires even when ENGRAM_SESSION_ROLE=heartbeat.

    format_calibration_block has no SESSION_ROLE gate — the block is
    unconditional (every SessionStart, every role).  This is intentional:
    calibration anchors are useful context regardless of who wakes up.
    The sleep_status_block() is the only injection that is role-gated
    (interactive-only, because only humans can decide to catch up on sleep).
    """
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    (engram_home / "sessions").mkdir()

    os.environ["ENGRAM_SESSION_ROLE"] = "heartbeat"
    try:
        mod = _load_hook_module(engram_home)

        mock_client_instance = _make_mock_engram_client(_CANNED_CONF_ALL, _CANNED_CONF_7D)
        mock_client_class = MagicMock(return_value=mock_client_instance)

        with patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=mock_client_class)}):
            hook_input = {
                "session_id": "test-session-heartbeat",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "startup",
            }
            result = _run_hook_main(mod, hook_input)
    finally:
        os.environ.pop("ENGRAM_SESSION_ROLE", None)

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "[ENGRAM Calibration:" in ctx, (
        "Calibration block should be present for non-interactive roles; "
        "it has no SESSION_ROLE gate by design."
    )


# ---------------------------------------------------------------------------
# Recharged line tests (commit 2 — #691)
# ---------------------------------------------------------------------------

def test_recharged_line_present_on_compact(tmp_path):
    """⚡ recharged line appears in additionalContext for source=compact."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    (engram_home / "sessions").mkdir()

    mod = _load_hook_module(engram_home)

    with patch.dict("sys.modules", {"engram_client": None}):
        hook_input = {
            "session_id": "test-session-recharged-compact",
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "source": "compact",
        }
        result = _run_hook_main(mod, hook_input)

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "compaction just completed" in ctx, (
        "Recharged line must be present when source=compact"
    )
    assert "stale-on-arrival" in ctx


def test_recharged_line_present_on_resume(tmp_path):
    """⚡ recharged line appears in additionalContext for source=resume."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    (engram_home / "sessions").mkdir()

    mod = _load_hook_module(engram_home)

    with patch.dict("sys.modules", {"engram_client": None}):
        hook_input = {
            "session_id": "test-session-recharged-resume",
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "source": "resume",
        }
        result = _run_hook_main(mod, hook_input)

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "compaction just completed" in ctx, (
        "Recharged line must be present when source=resume"
    )


def test_recharged_line_absent_on_startup(tmp_path):
    """⚡ recharged line must NOT appear in additionalContext for source=startup."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    (engram_home / "sessions").mkdir()

    mod = _load_hook_module(engram_home)

    with patch.dict("sys.modules", {"engram_client": None}):
        hook_input = {
            "session_id": "test-session-startup-no-recharged",
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "source": "startup",
        }
        result = _run_hook_main(mod, hook_input)

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "compaction just completed" not in ctx, (
        "Recharged line must be absent on fresh startup (source=startup)"
    )
