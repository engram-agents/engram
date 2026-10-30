"""Ordering invariant for SessionStart's additionalContext (issue #1819,
extended #2150).

The hook runs under a 5s harness timeout. On overrun the process is KILLED and
the ENTIRE additionalContext is lost — no partial delivery, no error surfaced.
Emission order is therefore a PRIORITY decision: cheap, purely-local,
load-bearing blocks must be appended BEFORE expensive ones, so a timeout costs
only the optional tail.

The regression this guards against is not hypothetical. Measured on two
independent installs on 2026-07-25, the hook was killed at 5018ms and 5016ms
against the 5000ms budget. The calibration block — ~79% of the hook's warm
runtime, dominated by EngramClient() construction — ran FIRST and consumed the
whole budget, so focus_block() (0.002s, free) was never reached and the
"deterministic focus channel" the identity layer depends on across compaction
silently delivered nothing for multiple sessions.

A second, distinct axis (#2150): nothing caps this hook's TOTAL emission
size, so a downstream reader that truncates (a harness preview, a context
budget) takes from the END, not the front. Measured live 2026-08-19: a
62,859-byte injection, a ~2KB harness preview, the focus block starting at
line 164 of 264 — well past the cut, because it was emitted after the
32,000-char-capped history arc. #1819's TIME-axis rule (cheap before
expensive) does not by itself say anything about focus-vs-history — both are
cheap — so this file's original tests left that ordering completely
unlocked; the tests below close that gap.

These tests lock the order so the fix cannot drift back. Complements
tests/test_session_start_hook_focus_render.py (focus renders at all) and
tests/test_session_start_hook_calibration.py (calibration renders at all);
neither constrains their relative position.
"""

import importlib.util
import io
import json
import os
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"

_CANNED_CONF_ALL = {
    "by_type": {
        "observation_factual": {"n": 200, "mean": 0.72, "p25": 0.65, "p50": 0.72, "p75": 0.80, "p90": 0.88, "p95": 0.92},
        "derivation":          {"n":  80, "mean": 0.68, "p25": 0.60, "p50": 0.68, "p75": 0.75, "p90": 0.82, "p95": 0.88},
    },
    "by_quote_type": {"hard_data": {"n": 120, "mean": 0.82}},
    "by_reasoning_type": {"deductive_modus_ponens": {"n": 40, "mean": 0.88}},
    "by_source_class": {"external": {"n": 280, "mean": 0.71}},
}

_CANNED_CONF_7D = {
    "by_type": {
        "observation_factual": {"n": 20, "mean": 0.74, "p25": 0.68, "p50": 0.74, "p75": 0.81, "p90": 0.89, "p95": 0.93},
    },
    "by_quote_type": {},
    "by_reasoning_type": {},
    "by_source_class": {},
}

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_hook_module(engram_home: Path):
    os.environ["ENGRAM_HOME"] = str(engram_home)
    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    sys.modules["context_tracker"] = stub_ct

    for mod_name in list(sys.modules.keys()):
        if mod_name in ("engram-session-start-hook", "engram_session_start_hook"):
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
    # budget, which would starve every lower-priority block sorted after it
    # and has nothing to do with what this file's own tests exercise).
    module.tool_capability_index_block = lambda: ""
    return module


def _make_focused_db(engram_home: Path) -> None:
    conn = sqlite3.connect(str(engram_home / "knowledge.db"))
    conn.execute(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            claim TEXT,
            focused_at TEXT,
            focus_reason TEXT,
            is_current INTEGER
        )
        """
    )
    conn.execute(
        "INSERT INTO nodes (id, claim, focused_at, focus_reason, is_current) VALUES (?, ?, ?, ?, ?)",
        ("ob_9001", "Order-invariant guard claim.", "2026-07-09T10:00:00Z", "order test", 1),
    )
    conn.execute("PRAGMA user_version = 1")  # #3741: a real store carries a schema version; the flag-OFF calibration block now runs only on one
    conn.commit()
    conn.close()


def _write_history_file(history_dir: Path, name: str, content: str) -> Path:
    """Seed a history-arc file (mirrors test_session_start_hook_history_arc.py's
    helper of the same name/shape) so history_arc_block() has real content to
    render instead of the "no history entries" placeholder — needed to
    exercise the #2150 focus-vs-history ordering, distinct from #1819's
    focus-vs-calibration ordering the rest of this file locks."""
    history_dir.mkdir(parents=True, exist_ok=True)
    path = history_dir / name
    path.write_text(content, encoding="utf-8")
    return path


def _make_mock_engram_client():
    mock_client = MagicMock()

    def _call_side_effect(tool_name, arguments=None):
        if tool_name != "engram_stats":
            return {}
        try:
            params = json.loads((arguments or {}).get("payload_json", "{}"))
        except (json.JSONDecodeError, AttributeError):
            params = {}
        if params.get("mode") == "7-turn":
            return {"confidence": _CANNED_CONF_7D}
        return {
            "confidence": _CANNED_CONF_ALL,
            "memory": {"current_turn": 7, "tier_sizes": {}},
        }

    mock_client.call.side_effect = _call_side_effect
    return mock_client


def _run_hook_main(module, hook_input: dict) -> dict:
    stdin_data = json.dumps(hook_input)
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with redirect_stdout(buf):
            module.main()
    return json.loads(buf.getvalue().strip())


@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram"
    home.mkdir()
    (home / "sessions").mkdir()
    return home


def _context_with_both_blocks(tmp_path, engram_home, source="startup") -> str:
    _make_focused_db(engram_home)
    mod = _load_hook_module(engram_home)
    mock_client_class = MagicMock(return_value=_make_mock_engram_client())
    with patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=mock_client_class)}):
        result = _run_hook_main(
            mod,
            {
                "session_id": f"order-test-{source}",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": source,
            },
        )
    return result["hookSpecificOutput"]["additionalContext"]


def _context_with_focus_and_history(tmp_path, engram_home, source) -> str:
    """Like _context_with_both_blocks, plus a real history-arc file so
    history_arc_block() renders actual content (not the "no history
    entries" placeholder, which never contains "Most recent arc —")."""
    _make_focused_db(engram_home)
    _write_history_file(
        engram_home / "history",
        "2026-08-15.md",
        "Order-invariant guard history-arc content.",
    )
    mod = _load_hook_module(engram_home)
    mock_client_class = MagicMock(return_value=_make_mock_engram_client())
    with patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=mock_client_class)}):
        result = _run_hook_main(
            mod,
            {
                "session_id": f"order-test-history-{source}",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": source,
            },
        )
    return result["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("source", ["compact", "resume"])
def test_focus_block_precedes_history_arc(tmp_path, engram_home, source):
    """#2150: the must-survive focus channel must be emitted before the
    history arc, which caps at 32,000 chars and is itself explicitly
    "already lived, not a to-do list" — the block a truncating reader
    (harness preview, context budget) can afford to lose first. Covers the
    SIZE axis; test_focus_block_precedes_calibration_block above covers the
    pre-existing TIME axis (#1819). Neither subsumes the other."""
    ctx = _context_with_focus_and_history(tmp_path, engram_home, source)

    focus_at = ctx.find("Focused nodes")
    history_at = ctx.find("Most recent arc —")

    assert focus_at != -1, f"source={source}: focus block missing entirely: {ctx!r}"
    assert history_at != -1, f"source={source}: history arc missing entirely: {ctx!r}"
    assert focus_at < history_at, (
        f"source={source}: ORDERING REGRESSION (#2150) — the history arc is "
        f"emitted before the focus list. The history arc caps at 32,000 chars "
        f"and focus_block() has measured as large as 30,139 chars post-dedup, "
        f"so a truncating reader that takes from the tail loses the "
        f"must-survive focus channel first. "
        f"focus at {focus_at}, history at {history_at}."
    )


def test_history_arc_absent_on_startup_focus_present(tmp_path, engram_home):
    """#2150's control-flow change: history_arc_block() was pulled out of the
    source if/else's `else:` arm into its own `if source != "startup":`
    block so it could be repositioned after focus/saved-sets. This
    pins that the non-startup-only gating survived the move — without it, a
    future edit could silently restore history on startup and nothing here
    would fail."""
    ctx = _context_with_focus_and_history(tmp_path, engram_home, "startup")

    assert "Focused nodes" in ctx, f"focus block missing on startup: {ctx!r}"
    assert "Most recent arc —" not in ctx, (
        f"ORDERING REGRESSION (#2150) — the history arc rendered on "
        f"source=='startup'; it must only render on compact/resume. {ctx!r}"
    )


@pytest.mark.parametrize("source", ["startup", "compact", "resume"])
def test_focus_block_precedes_calibration_block(tmp_path, engram_home, source):
    """#1819: the cheap load-bearing focus list must be emitted before the
    expensive calibration block, for every session source."""
    ctx = _context_with_both_blocks(tmp_path, engram_home, source)

    focus_at = ctx.find("Focused nodes")
    calib_at = ctx.find("ENGRAM Calibration")

    assert focus_at != -1, f"source={source}: focus block missing entirely: {ctx!r}"
    assert calib_at != -1, f"source={source}: calibration block missing entirely: {ctx!r}"
    assert focus_at < calib_at, (
        f"source={source}: ORDERING REGRESSION (#1819) — the calibration block is "
        f"emitted before the focus list. This hook runs under a 5s timeout and is "
        f"killed wholesale on overrun, so the expensive block running first means a "
        f"slow start silently drops the deterministic focus channel. "
        f"focus at {focus_at}, calibration at {calib_at}."
    )


def test_focus_survives_calibration_failure(tmp_path, engram_home):
    """The whole point of the ordering: calibration blowing up must not take the
    focus list with it."""
    _make_focused_db(engram_home)
    mod = _load_hook_module(engram_home)

    exploding_client = MagicMock()
    exploding_client.call.side_effect = RuntimeError("engram_stats unavailable")
    mock_client_class = MagicMock(return_value=exploding_client)

    with patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=mock_client_class)}):
        result = _run_hook_main(
            mod,
            {
                "session_id": "order-test-calib-fail",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "startup",
            },
        )

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "Focused nodes" in ctx, "focus list must survive a calibration failure"
    assert "ob_9001" in ctx
    assert "ENGRAM Calibration" not in ctx


def test_focus_survives_calibration_client_construction_failure(tmp_path, engram_home):
    """Closest unit-testable analogue of the real #1819 failure: the cost that
    killed the hook was EngramClient() construction, not the stats calls."""
    _make_focused_db(engram_home)
    mod = _load_hook_module(engram_home)

    def _explode_on_construct(*args, **kwargs):
        raise RuntimeError("server runtime unavailable during construction")

    with patch.dict(
        "sys.modules",
        {"engram_client": MagicMock(EngramClient=MagicMock(side_effect=_explode_on_construct))},
    ):
        result = _run_hook_main(
            mod,
            {
                "session_id": "order-test-construct-fail",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "startup",
            },
        )

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "Focused nodes" in ctx, (
        "focus list must survive EngramClient construction failure — this is the "
        "exact cost that consumed the 5s budget in #1819"
    )
    assert "ENGRAM Calibration" not in ctx
