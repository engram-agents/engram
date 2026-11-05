"""Tests: engram-alpha#127 -- engram-session-start-hook.py's TOTAL
additionalContext emission cap via _hooklib.emit_context().

Before this fix, main() built its additionalContext as a bare
"\\n".join(lines) with only PER-BLOCK caps (e.g. HISTORY_ARC_CHAR_CAP,
32,000 chars, alone bigger than #2863's 9,500-char total budget) -- so the
TOTAL emission was unbounded. Measured 62,859 bytes on a real post-compact
injection (#2150, 2026-08-19) and 64.3 KB on a live session start
(#127, 2026-09-28). This file is the analogue of
tests/test_lesson_tripwire_injection_bounds.py (the reference migration
this fix follows) and tests/test_surface_hook_injection_bounds.py for THIS
hook; it complements tests/test_session_start_hook_block_order.py's own
per-block ordering-invariant coverage (which this fix generalizes into an
actual TOTAL-size cap -- see main()'s own "ORDERING INVARIANT" comment for
how the two relate) rather than duplicating it.

Covers the spec's own "Tests" section for #127:
  1. Synthetic oversized input (>=60K chars, spread across several P3
     blocks, none individually exceeding the hard budget alone) -> output
     never exceeds budget (9,500); the trailer is line 1; every P0 warning
     line is inside the first head_budget (1,800) chars; the overflow file
     exists and contains every cut block's full text, verbatim.
  2. Small input -> the trailer reads "0 cut", no overflow file is
     written, and block order follows the P0<P1<P2<P3 priority scheme
     (within P3, the hook's own existing relative order is preserved --
     see the class docstring below for why this is the reading used here).
  3. Oversized P1 alone (the live focus block, bigger than the hard budget
     BY ITSELF) -> truncated in-band with the
     "...[truncated, full text in <path>]" marker; its full, untruncated
     text is still recoverable from the overflow file.

Item 4 of the spec's own "Tests" section (test_hook_injection_bounds.py
passing with this hook removed from `_EXCLUDED`) is covered by that file
directly, not duplicated here. Item 5 (the two mutation controls) is a
discriminating-check discipline run once against a deliberately-broken
copy of the hook and reverted -- not shippable as a permanently-passing
test (a mutation control that always "passes" in CI proves nothing); see
the PR description / handoff for that run's result.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
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
    },
    "by_quote_type": {"hard_data": {"n": 120, "mean": 0.82}},
    "by_reasoning_type": {},
    "by_source_class": {},
}
_CANNED_CONF_7D = {
    "by_type": {},
    "by_quote_type": {},
    "by_reasoning_type": {},
    "by_source_class": {},
}

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_hook_module(engram_home: Path):
    """Load engram-session-start-hook.py fresh, ENGRAM_HOME pointed at
    engram_home. Mirrors tests/test_session_start_hook_block_order.py's own
    _load_hook_module() -- same stub/reload discipline -- plus this file's
    own two #127-specific fixture needs:

    - A pinned detect_harness(): this dev/source checkout has no baked
      platform.json (see _harness.py's own docstring), so the hook's own
      detect_harness() would otherwise return None and
      get_harness_budgets(None) would silently fall through to
      UNMEASURED_HARNESS_FLOOR (4,000 chars) instead of claude-code's
      measured 9,500 -- the number every assertion in this file is written
      against. Same discipline as
      tests/test_lesson_tripwire_injection_bounds.py's own
      _load_harness_budgets() docstring.
    - A stubbed tool_capability_index_block(): this file's fixtures are
      about the CAP MECHANISM itself, not the real, size-varying checked-in
      tool_capability_index.txt artifact (measured 11,049 bytes on this
      checkout -- bigger than the hook's own total budget by itself, which
      would starve every block sorted after it and has nothing to do with
      what any test in this file exercises).
    - #3217: tool_capability_short_block() is stubbed to "" by the same
      reasoning -- this file's fixtures are about the cap MECHANISM, not
      the real, size-varying checked-in artifact the short block also
      reads from. Individual tests that need the real-seat shape (both a
      full and a short tool-caps block present) override this default with
      a realistic ~800-char stub, same as they already do for
      tool_capability_index_block.
    """
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
    module.detect_harness = lambda: "claude-code"
    module.tool_capability_index_block = lambda: ""
    module.tool_capability_short_block = lambda: ""
    return module


def _run_hook_main(module, hook_input: dict) -> dict:
    stdin_data = json.dumps(hook_input)
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with redirect_stdout(buf):
            module.main()
    return json.loads(buf.getvalue().strip())


def _mock_no_engram_client():
    """EngramClient construction raises -> the calibration block silently
    skips (never renders). Mirrors
    tests/test_session_start_hook_block_order.py's own
    test_focus_survives_calibration_client_construction_failure fixture."""
    return patch.dict(
        "sys.modules",
        {"engram_client": MagicMock(EngramClient=MagicMock(
            side_effect=RuntimeError("no client in this test")
        ))},
    )


def _mock_engram_client():
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
        return {"confidence": _CANNED_CONF_ALL, "memory": {"current_turn": 3, "tier_sizes": {}}}

    mock_client.call.side_effect = _call_side_effect
    return patch.dict(
        "sys.modules", {"engram_client": MagicMock(EngramClient=MagicMock(return_value=mock_client))}
    )


def _stub_healthy_substrate(mod):
    """Patch the four substrate-health probes to their all-clear return
    shapes so the P0 block never fires -- isolates a fixture from this
    seat's own real MCP/viz/backup state (mirrors
    tests/test_session_start_hook_block_order.py's own
    patch.object(mod, "_check_mcp_health", ...) usage)."""
    return (
        patch.object(mod, "_check_mcp_health", return_value=(True, None)),
        patch.object(mod, "_check_backup_staleness", return_value=(True, None)),
        patch.object(mod, "_check_viz_server_health", return_value=("not-configured", None)),
        patch.object(mod, "_check_embedding_restore_flag", return_value=(True, None)),
    )


@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram"
    home.mkdir()
    (home / "sessions").mkdir()
    return home


# ---------------------------------------------------------------------------
# Item 1 -- Synthetic oversized input (>=60K chars across several P3
# blocks, none individually exceeding the hard budget alone).
# ---------------------------------------------------------------------------
class TestOversizedInputStaysBounded:
    def test_never_exceeds_budget_trailer_first_p0_in_head_overflow_recoverable(
        self, engram_home, tmp_path
    ):
        mod = _load_hook_module(engram_home)

        # Force ONE P0 substrate-health warning so the head-budget
        # assertion below has real P0 content to check against. Direct
        # attribute assignment on this per-test, freshly-loaded module
        # (discarded at test end) -- no mock.patch bookkeeping needed.
        # #3202: at source=startup an MCP miss is only a soft ℹ️ line, so
        # force a genuine ⚠️ condition (stale git backup) instead.
        mod._check_mcp_health = lambda home: (True, None)
        mod._check_backup_staleness = lambda home: (False, "forced-stale for test")
        mod._check_viz_server_health = lambda home: ("not-configured", None)
        mod._check_embedding_restore_flag = lambda home: (True, None)

        # Eight independently-monkeypatchable P3 blocks (7 startup-only +
        # saved-sets), each ~7,000 chars with a distinguishing
        # marker -- 56,000 chars total, well past #127's 9,500-char budget,
        # but no SINGLE block here exceeds the budget alone (that edge case
        # is TestOversizedSingleP1Block below, not this fixture).
        block_names = [
            "sleep_status_block", "auto_sleep_cron_block", "fairy_policy_block",
            "forum_monitor_block", "forum_updates_monitor_block",
        ]
        for name in block_names:
            marker = f"MARK-{name.upper()}-"
            setattr(mod, name, lambda _m=marker: _m + ("x" * 7000))
        mod.peer_topology_block = lambda agent_name: "MARK-PEER-" + ("x" * 7000)
        mod.piece_c_git_log = lambda session_id: "MARK-PIECEC-" + ("x" * 7000)
        mod.saved_focus_sets_block = lambda home: "MARK-SAVEDSETS-" + ("x" * 7000)

        with _mock_no_engram_client():
            result = _run_hook_main(mod, {
                "session_id": "oversized-127",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "startup",
            })
        ctx = result["hookSpecificOutput"]["additionalContext"]

        assert len(ctx) <= 9_500, f"exceeded hard budget: {len(ctx)} chars"

        lines = ctx.split("\n")
        assert lines[0].startswith("▸ "), f"trailer must be line 1: {lines[0]!r}"

        head_region = ctx[:1_800]
        assert "⚠️  ENGRAM substrate health" in head_region, (
            f"P0 warning header missing from the first head_budget (1,800) "
            f"chars: {ctx[:300]!r}"
        )
        assert "Git backup: forced-stale for test" in head_region, (
            f"P0 warning line missing from the first head_budget (1,800) "
            f"chars: {ctx[:300]!r}"
        )

        match = re.search(r"— see (\S+)$", lines[0])
        assert match, f"trailer has no overflow pointer despite an oversized fixture: {lines[0]!r}"
        overflow_path = Path(match.group(1))
        assert overflow_path.exists(), f"overflow file missing: {overflow_path}"
        overflow_text = overflow_path.read_text(encoding="utf-8")

        # Every block NOT shown in full inline must be recoverable, in
        # full, verbatim, from the overflow file -- nothing may be
        # silently lost.
        all_markers = [
            "MARK-SLEEP_STATUS_BLOCK-", "MARK-AUTO_SLEEP_CRON_BLOCK-",
            "MARK-FAIRY_POLICY_BLOCK-", "MARK-FORUM_MONITOR_BLOCK-",
            "MARK-FORUM_UPDATES_MONITOR_BLOCK-", "MARK-PEER-", "MARK-PIECEC-",
            "MARK-SAVEDSETS-",
        ]
        cut_markers = [m for m in all_markers if m not in ctx]
        assert cut_markers, (
            "fixture assumption: with 56,000 chars across 8 blocks under a "
            "9,500-char budget, at least one block must be cut -- if this "
            "fails, the fixture no longer forces a real cut"
        )
        for marker in cut_markers:
            full_block = marker + ("x" * 7000)
            assert full_block in overflow_text, (
                f"{marker!r} was cut from ctx but its full text is not "
                f"verbatim in the overflow file -- content silently lost"
            )


# ---------------------------------------------------------------------------
# Item 2 -- Small input: "0 cut", no overflow file, priority-sorted order.
# ---------------------------------------------------------------------------
# What "block order unchanged vs the pre-change join" (the spec's Tests
# section, item 2) means when nothing is cut: NOT byte-identical to the
# pre-#127 bare join (that join's own literal append order put the
# active-session line first, AHEAD of the focus block -- #127's step 2
# explicitly re-orders body by priority class). Read here as: cross-class
# order is the priority scheme, and WITHIN one class, the hook's
# pre-existing relative append order is preserved verbatim -- which is
# exactly what step 2's own text says for "everything else ... in the
# hook's existing relative order" and what this test locks.
#
# #127 r3 UPDATE: this test previously asserted focus (P1) BEFORE the
# active-session line (P2) -- r1/r2's own priority scheme, before the
# reviewer-fairy finding on PR #3214 (comment 5880917666) showed that
# ordering pushes the #2859 must-not-miss lines past the harness's
# 1,800-char head-preview window on a real-seat-shaped focus block. #127 r3
# moves the must-not-miss group (_P_MUST) ahead of focus (_P_FOCUS) -- see
# the hook's own ORDERING INVARIANT comment, rule (c) -- so the assertion
# below is inverted to match: _P_MUST (active-session / session-start-
# reading / compaction notice) now precedes _P_FOCUS, which still precedes
# _P_REST (calibration). This is a direct, necessary consequence of this
# spec's own reordering, not an independent judgment call -- flagged in the
# handoff regardless, per this file's own "flag every ordering assertion
# edit" precedent from r1/r2.
class TestSmallInputNothingCut:
    def test_zero_cut_no_overflow_file_priority_sorted_order(self, engram_home, tmp_path):
        mod = _load_hook_module(engram_home)
        for p in _stub_healthy_substrate(mod):
            p.start()

        # A focused node so the focus (_P_FOCUS) block actually renders
        # (otherwise this test couldn't distinguish "focus correctly
        # positioned" from "focus simply absent").
        import sqlite3
        conn = sqlite3.connect(str(engram_home / "knowledge.db"))
        conn.execute(
            "CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT, "
            "focused_at TEXT, focus_reason TEXT, is_current INTEGER)"
        )
        conn.execute(
            "INSERT INTO nodes (id, claim, focused_at, focus_reason, is_current) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ob_8001", "Small-input order-check claim.", "2026-09-28T00:00:00Z", "order check", 1),
        )
        conn.execute("PRAGMA user_version = 1")  # #3741: a real store carries a schema version; the flag-OFF calibration block now runs only on one
        conn.commit()
        conn.close()

        with _mock_engram_client():
            result = _run_hook_main(mod, {
                "session_id": "small-input-127",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "compact",
            })
        ctx = result["hookSpecificOutput"]["additionalContext"]

        assert "0 cut" in ctx.splitlines()[0], f"expected 0 cut, got trailer: {ctx.splitlines()[0]!r}"
        assert not (engram_home / "session-start-overflow").exists(), (
            "no overflow file/dir should be written when nothing is cut"
        )

        focus_idx = ctx.find("Focused nodes")
        active_idx = ctx.find("[Active session:")
        reading_idx = ctx.find("[Session start reading")
        compaction_idx = ctx.find("A compaction just completed")
        calib_idx = ctx.find("ENGRAM Calibration")

        assert focus_idx != -1, f"focus block (_P_FOCUS) missing: {ctx!r}"
        assert active_idx != -1, f"active-session line (_P_MUST) missing: {ctx!r}"
        assert reading_idx != -1, f"session-start-reading pointer (_P_MUST) missing: {ctx!r}"
        assert compaction_idx != -1, f"compaction notice (_P_MUST) missing: {ctx!r}"
        assert calib_idx != -1, f"calibration block (_P_REST) missing: {ctx!r}"

        # #4351: on a non-startup source the re-orient block now leads the
        # group (session-start-reading, then active-session, then the
        # compaction notice).
        assert reading_idx < active_idx < compaction_idx, (
            "within _P_MUST, the order is (session-start-reading re-orient "
            "block, then active-session, then compaction notice) per #4351"
        )
        # #127 r3: _P_MUST now precedes _P_FOCUS (inverted from r1/r2's own
        # P1<P2 scheme -- see this class's docstring above).
        assert compaction_idx < focus_idx, (
            "_P_MUST (the #2859 must-not-miss group) must precede _P_FOCUS "
            "per #127 r3's reordering"
        )
        assert focus_idx < calib_idx, "_P_FOCUS must precede _P_REST (calibration)"


# ---------------------------------------------------------------------------
# Item 3 -- Oversized P1 alone (focus block bigger than the hard budget by
# itself) -> #127 r2 UPDATE: pre-cut IN THE HOOK (main()'s own
# reserve/focus_cap computation) before ever reaching emit_context(), rather
# than emit_context()'s own per-item truncated_first path -- so the marker
# is the hook's own "+K more focused nodes cut" line (_focus_cut_marker),
# NOT emit_context()'s "...[truncated, full text in <path>]" marker, and
# every OTHER small block (here: the two P2 blocks) stays inline instead of
# being dragged into truncated_first's single-item degrade alongside it.
# This is a narrow fixture update to a PRE-EXISTING r1 test, on the same
# footing as r1's own two narrow fixture updates (detect_harness pinning,
# tool_capability_index_block stubbing) -- the r1-era assertions here
# described exactly the mechanism #127 r2 replaces for this scenario (see
# the r2 spec's own "Tests" section, "Floor" item, and its "r1 ambiguous
# decisions" item 3: "resolved by this spec"). Flagged in the handoff.
# ---------------------------------------------------------------------------
class TestOversizedSingleP1Block:
    def test_focus_block_alone_over_budget_precut_in_hook_full_text_recoverable(
        self, engram_home, tmp_path
    ):
        mod = _load_hook_module(engram_home)
        for p in _stub_healthy_substrate(mod):
            p.start()

        huge_focus = "FOCUS-ALONE-" + ("f" * 15_000)  # > 9,500-char budget alone
        mod.focus_block = lambda home: huge_focus

        with _mock_no_engram_client():
            result = _run_hook_main(mod, {
                "session_id": "oversized-p1-127",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "startup",
            })
        ctx = result["hookSpecificOutput"]["additionalContext"]

        assert len(ctx) <= 9_500
        assert "more focused nodes cut" in ctx, (
            f"expected the hook's own in-band focus-cut marker: {ctx[-250:]!r}"
        )
        assert huge_focus not in ctx, "the full oversized block must not appear whole inline"

        # Both small P2 blocks must survive inline -- the entire point of
        # #127 r2's pre-cut: without it, emit_context()'s own
        # truncated_first degrade would have dropped both of these behind
        # the single (huge, truncated) focus item.
        assert "[Active session:" in ctx, "P2 active-session line must stay inline"
        assert "[Session start reading" in ctx, "P2 session-start-reading pointer must stay inline"

        match = re.search(r"— see (\S+)$", ctx.splitlines()[0])
        assert match, f"trailer has no overflow pointer: {ctx.splitlines()[0]!r}"
        overflow_text = Path(match.group(1)).read_text(encoding="utf-8")
        assert huge_focus in overflow_text, (
            "the truncated block's FULL, untruncated text must be "
            "recoverable verbatim from the overflow file"
        )


# ---------------------------------------------------------------------------
# #127 r2 -- "Real-seat shape": a ~40K focus block made of many real
# "  - [ob_9xxx] ..." entry lines, the P2 blocks, a small P3 block, a
# ~4.5K history arc, a small calibration block, and an 11K tool-caps
# block, under the real budget (9,500) -- the exact shape measured on a
# real seat in the r2 spec's own "Why" section (`▸ 1 shown · 7/9 cut`
# before this fix).
# ---------------------------------------------------------------------------
def _synthetic_focus_block(n_entries: int, reason: str = "cohort test pin") -> str:
    """A focus_block()-shaped fixture: real header + N real "  - [ob_9xxx]
    ..." entry lines (synthetic ob_9xxx IDs per #127 r2's own "DO NOT" —
    never a real ENGRAM node id), long enough in aggregate to reach ~40K
    chars across ~500 entries at ~80 chars each."""
    header = "📌 Focused nodes — the deterministic focus channel (verbatim, must survive compaction):"
    lines = [header]
    for i in range(n_entries):
        lines.append(
            f"  - [ob_9{i:03d}] ({reason}) Synthetic focus claim number {i} for the real-seat fixture."
        )
    return "\n".join(lines)


class TestRealSeatShape:
    def test_every_small_block_inline_focus_marker_correct_k_overflow_has_full_text(
        self, engram_home, tmp_path
    ):
        mod = _load_hook_module(engram_home)
        for p in _stub_healthy_substrate(mod):
            p.start()

        huge_focus = _synthetic_focus_block(500)  # ~40K chars, matches the r2 spec's own real-seat figure
        mod.focus_block = lambda home: huge_focus
        mod.history_arc_block = lambda home: "MARK-ARC-" + ("a" * 4_500)  # ~4.5K, matches the spec's "day arc"
        mod.saved_focus_sets_block = lambda home: "MARK-SAVEDSETS-" + ("s" * 100)
        huge_tool_caps = "MARK-TOOLCAPS-" + ("c" * 11_000)  # ~11K, matches the spec's tool-caps figure
        mod.tool_capability_index_block = lambda: huge_tool_caps
        # #3217: realistic ~800-char short-tool-caps stub (owner's own
        # measurement, issue comment 5881294279: names-only form is 803
        # chars against the real artifact) -- both the full and short
        # tool-caps blocks are present under the real-seat shape.
        short_tool_caps_stub = "MARK-SHORTCAPS-" + ("k" * 800)
        mod.tool_capability_short_block = lambda: short_tool_caps_stub

        import sqlite3
        _store = sqlite3.connect(str(engram_home / "knowledge.db"))
        _store.execute("PRAGMA user_version = 1")  # #3741: a real store carries a schema version; the flag-OFF calibration block now runs only on one
        _store.commit()
        _store.close()

        with _mock_engram_client():
            result = _run_hook_main(mod, {
                "session_id": "real-seat-127",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "compact",
            })
        ctx = result["hookSpecificOutput"]["additionalContext"]

        assert len(ctx) <= 9_500, f"exceeded hard budget: {len(ctx)} chars"

        # Every small P2/P3 block appears inline -- the entire point of
        # #127 r2's tool-caps-last + focus-sub-cap fix (mutation control
        # (a) below is what breaks this).
        assert "[Active session:" in ctx
        assert "[Session start reading" in ctx
        assert "A compaction just completed" in ctx
        assert "MARK-ARC-" in ctx
        assert "MARK-SAVEDSETS-" in ctx
        # Mutation control (b)'s own named target: calibration specifically
        # must be inline. If tool-caps is moved back ahead of calibration
        # (mutation (b)), this line goes red.
        assert "ENGRAM Calibration" in ctx, "calibration (small P3) must be inline"
        # #3217: the SHORT tool-caps block is small enough to survive
        # inline even under the real-seat shape, unlike the full index
        # (asserted absent below).
        assert short_tool_caps_stub in ctx, "short tool-caps block must be inline"

        # The focus marker appears with the correct K.
        match_marker = re.search(r"\(\+(\d+) more focused nodes cut", ctx)
        assert match_marker, f"expected the focus-cut marker with a K count: {ctx!r}"
        k = int(match_marker.group(1))
        assert k > 0, "a ~40K focus block under a 9,500 budget must drop at least one entry"
        total_entries = huge_focus.count("\n  - [")
        kept_entries = total_entries - k
        assert 0 <= kept_entries < total_entries, (
            f"K={k} is inconsistent with {total_entries} total entry lines"
        )

        # Neither the full focus text nor the full tool-caps text appears
        # whole inline -- both are, by construction, cut to the overflow
        # file.
        assert huge_focus not in ctx
        assert huge_tool_caps not in ctx

        match = re.search(r"— see (\S+)$", ctx.splitlines()[0])
        assert match, f"trailer has no overflow pointer: {ctx.splitlines()[0]!r}"
        overflow_text = Path(match.group(1)).read_text(encoding="utf-8")
        assert huge_focus in overflow_text, (
            "the full, untruncated focus text must be recoverable byte-for-byte "
            "from the overflow file"
        )
        assert huge_tool_caps in overflow_text, (
            "the full tool-capability-index text must be recoverable "
            "byte-for-byte from the overflow file"
        )


# ---------------------------------------------------------------------------
# #127 r2 -- "Small focus": a focus block under focus_cap -> no marker, no
# duplicate focus text in the overflow file. Extends
# TestSmallInputNothingCut's own "0 cut" coverage above with assertions
# specific to the new focus-marker/dedup-copy mechanism.
# ---------------------------------------------------------------------------
class TestSmallFocusNoMarkerNoDuplicate:
    def test_small_focus_no_marker_no_duplicate_copy(self, engram_home, tmp_path):
        mod = _load_hook_module(engram_home)
        for p in _stub_healthy_substrate(mod):
            p.start()

        small_focus = _synthetic_focus_block(3)  # well under any plausible focus_cap
        mod.focus_block = lambda home: small_focus

        with _mock_engram_client():
            result = _run_hook_main(mod, {
                "session_id": "small-focus-127",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "compact",
            })
        ctx = result["hookSpecificOutput"]["additionalContext"]

        assert "0 cut" in ctx.splitlines()[0], f"expected 0 cut, got: {ctx.splitlines()[0]!r}"
        assert "more focused nodes cut" not in ctx, "no marker when focus already fits"
        assert small_focus in ctx, "the (untruncated) focus block must appear inline whole"
        assert ctx.count("Synthetic focus claim number 0 ") == 1, (
            "the focus text must appear exactly once -- no duplicate copy "
            "when nothing was truncated"
        )
        assert not (engram_home / "session-start-overflow").exists(), (
            "no overflow file/dir when nothing is cut"
        )


# ---------------------------------------------------------------------------
# #127 r2 -- "Floor": the OTHER blocks (P0/P2/P3 minus tool-caps) alone
# already consume nearly the whole budget -> focus_cap is floored at
# FOCUS_FLOOR (1,500) rather than driven to near-zero, and nothing crashes.
# ---------------------------------------------------------------------------
class TestFocusFloor:
    def test_other_blocks_dominate_focus_still_shown_at_floor_nothing_crashes(
        self, engram_home, tmp_path
    ):
        mod = _load_hook_module(engram_home)
        for p in _stub_healthy_substrate(mod):
            p.start()

        # A moderate-size focus block (bigger than FOCUS_FLOOR, so the
        # floor -- not "already fit" -- is what's under test) alongside a
        # single P3 block big enough, alone, to push `reserve` past the
        # budget on its own.
        moderate_focus = _synthetic_focus_block(40)  # a few thousand chars
        mod.focus_block = lambda home: moderate_focus
        mod.saved_focus_sets_block = lambda home: "MARK-HUGE-OTHER-" + ("z" * 20_000)

        with _mock_no_engram_client():
            result = _run_hook_main(mod, {
                "session_id": "floor-127",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "startup",
            })
        ctx = result["hookSpecificOutput"]["additionalContext"]

        assert len(ctx) <= 9_500, f"exceeded hard budget: {len(ctx)} chars"
        # Focus must still be shown, at or near FOCUS_FLOOR (module import
        # deliberately avoided here -- this test asserts on the OBSERVABLE
        # render, not the hook's own internal constant).
        assert "📌 Focused nodes" in ctx, "focus block must still render, even at the floor"
        assert "[Active session:" in ctx, "must not crash -- normal small P2 content still present"


# ---------------------------------------------------------------------------
# #127 r3 -- Head-window test (reviewer-fairy option (c) on PR #3214,
# comment 5880917666): under the real-seat shape (>=40K focus, small
# _P_MUST/_P_REST, 11K tool-caps), every #2859 must-not-miss needle lands
# inside ctx[:1800] -- the harness's own PHYSICAL preview-truncation
# boundary (`_HEAD_PREVIEW_CHARS` in
# tests/test_session_start_hook_piece_c_sidecar.py, unmodified by this
# spec) -- for BOTH startup and compact sources. r1/r2 ranked _P_FOCUS
# ahead of this group; #127 r3 moves the group (_P_MUST) ahead of
# _P_FOCUS specifically to hold this invariant under a real-seat-shaped
# focus block, which the #2859 test's own trivial-focus fixture never
# exercised.
# ---------------------------------------------------------------------------

# Copied from tests/test_session_start_hook_piece_c_sidecar.py's own
# `must_not_miss` list (defined as a local variable inside a test function
# there, not a module-level constant -- so not cleanly importable without
# either touching that file (out of scope / explicitly DO-NOT-touch per the
# #127 r3 spec) or importing a test module as a library, which is fragile).
# Per the spec's own fallback instruction ("otherwise copy it, with a
# comment naming the source file"), copied verbatim here.
_MUST_NOT_MISS_2859 = [
    "[Active session:",
    "[Session start reading —",
    "[Coder fairy policy:",
    "[Reviewer fairy policy:",
]


class TestHeadWindowRealSeatShape:
    @pytest.mark.parametrize("source", ["startup", "compact"])
    def test_must_not_miss_needles_inside_head_preview_window(
        self, engram_home, tmp_path, source
    ):
        # fairy_policy_block() needs a real (even if minimal) config.json to
        # open without raising -- matches
        # tests/test_session_start_hook_piece_c_sidecar.py's own
        # `engram_home` fixture, which documents the same requirement.
        (engram_home / "config.json").write_text("{}", encoding="utf-8")

        mod = _load_hook_module(engram_home)
        for p in _stub_healthy_substrate(mod):
            p.start()

        huge_focus = _synthetic_focus_block(500)  # ~40K chars, real-seat shape
        mod.focus_block = lambda home: huge_focus
        mod.history_arc_block = lambda home: "MARK-ARC-" + ("a" * 4_500)
        mod.saved_focus_sets_block = lambda home: "MARK-SAVEDSETS-" + ("s" * 100)
        huge_tool_caps = "MARK-TOOLCAPS-" + ("c" * 11_000)
        mod.tool_capability_index_block = lambda: huge_tool_caps
        # #3217: realistic ~800-char short-tool-caps stub, same as
        # TestRealSeatShape above.
        mod.tool_capability_short_block = lambda: "MARK-SHORTCAPS-" + ("k" * 800)

        with _mock_engram_client():
            result = _run_hook_main(mod, {
                "session_id": f"head-window-127-{source}",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": source,
            })
        ctx = result["hookSpecificOutput"]["additionalContext"]
        head = ctx[:1800]

        needles = list(_MUST_NOT_MISS_2859)
        if source != "startup":
            # fairy_policy_block() is gated to source=="startup" -- a
            # pre-existing gate this spec does not touch (see main()'s own
            # `if source == "startup":` block). It never renders at all on
            # compact, so checking for these two needles on compact would
            # be checking for content this hook never produces on this
            # source, not a #127 r3 regression.
            needles = [n for n in needles if "fairy policy" not in n]
        for needle in needles:
            assert needle in head, (
                f"source={source}: must-not-miss needle {needle!r} not "
                f"found in first 1800 chars: {head!r}"
            )

        if source != "startup":
            # The compaction notice is the #127 r3 spec's own extra
            # must-not-miss item, "plus the compaction notice on compact".
            assert "A compaction just completed" in head, (
                f"source={source}: compaction notice not found in first "
                f"1800 chars: {head!r}"
            )


# ---------------------------------------------------------------------------
# #127 r3 -- Unmeasured-floor test (reviewer suggestion 2 on PR #3214,
# comment 5880917666): the same real-seat shape, but with the harness
# budget at the unmeasured floor (`detect_harness()` -> None, budget
# 4,000 -- see _harness.py's UNMEASURED_HARNESS_FLOOR) -> output stays
# within that smaller budget, the must-not-miss needles are still inside
# ctx[:1800] (the harness preview boundary is a FIXED physical constant,
# independent of this hook's own internal `target`/`budget` choice), the
# focus block renders truncated with its marker, and nothing crashes.
# ---------------------------------------------------------------------------
class TestUnmeasuredFloorRealSeatShape:
    def test_real_seat_shape_at_unmeasured_floor_budget(self, engram_home, tmp_path):
        (engram_home / "config.json").write_text("{}", encoding="utf-8")

        mod = _load_hook_module(engram_home)
        # Override _load_hook_module()'s own claude-code pin: this test is
        # specifically about the UNMEASURED harness path
        # (get_harness_budgets(None) -> UNMEASURED_HARNESS_FLOOR, budget
        # 4,000), not the measured claude-code budget every other test in
        # this file is pinned against.
        mod.detect_harness = lambda: None
        for p in _stub_healthy_substrate(mod):
            p.start()

        huge_focus = _synthetic_focus_block(500)  # ~40K chars, real-seat shape
        mod.focus_block = lambda home: huge_focus
        mod.history_arc_block = lambda home: "MARK-ARC-" + ("a" * 4_500)
        mod.saved_focus_sets_block = lambda home: "MARK-SAVEDSETS-" + ("s" * 100)
        huge_tool_caps = "MARK-TOOLCAPS-" + ("c" * 11_000)
        mod.tool_capability_index_block = lambda: huge_tool_caps
        # #3217: realistic ~800-char short-tool-caps stub, same as
        # TestRealSeatShape above.
        mod.tool_capability_short_block = lambda: "MARK-SHORTCAPS-" + ("k" * 800)

        with _mock_engram_client():
            result = _run_hook_main(mod, {
                "session_id": "unmeasured-floor-127",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "compact",
            })
        ctx = result["hookSpecificOutput"]["additionalContext"]

        assert len(ctx) <= 4_000, (
            f"exceeded the unmeasured-floor budget (4,000): {len(ctx)} chars"
        )

        head = ctx[:1800]
        needles = [n for n in _MUST_NOT_MISS_2859 if "fairy policy" not in n]
        for needle in needles:
            assert needle in head, (
                f"must-not-miss needle {needle!r} not found in first 1800 "
                f"chars at the unmeasured floor: {head!r}"
            )
        assert "A compaction just completed" in head, (
            f"compaction notice not found in first 1800 chars at the "
            f"unmeasured floor: {head!r}"
        )

        assert "more focused nodes cut" in ctx, (
            f"expected the focus block to render truncated with its "
            f"in-band marker at the unmeasured floor: {ctx[-250:]!r}"
        )
        assert huge_focus not in ctx, (
            "the full oversized focus block must not appear whole inline "
            "at the unmeasured floor"
        )
