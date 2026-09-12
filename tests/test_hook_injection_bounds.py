"""Gate: engram-alpha#2863 WS1 -- bounded hook-injection helper.

Design authority: Borges's comment on the issue (2026-09-12T14:02:48Z) and
Kepler's two amendments (2026-09-12T14:31:26Z) -- this test suite is a
translation of that already-approved design's `## 4. The gate` section
(Borges) plus Kepler's Amendment 2 gate item, not a re-derivation of it.

Six required checks (Part C of the WS1 spec, itself a translation of
Borges S4 + Kepler's amendments):
  1. Bound            -- TestBound
  2. Head survives     -- TestHeadSurvives
  3. No writer outside the helper (structural) -- TestNoBareJoinOutsideHelper
     (currently xfail-tracked pending WS2's hook migration -- see that
     class's own docstring for why)
  4. Trailing-summary decoy MUST FAIL -- TestTrailingSummaryDecoy
  5. Straddling-line decoy MUST FAIL  -- TestStraddlingLineDecoy
  6. Unknown-harness fixture -- TestUnknownHarnessFloor

Reference implementation studied before writing this suite:
src/engram/hooks/claude/engram-lesson-tripwire-hook.py (merged f4ee7a78,
PR #2860) -- its own select_and_cap_hits / _render_tripwire_block /
_build_trailer / write_tripwire_overflow are the proven mechanism
`_hooklib.emit_context()` generalizes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).parent.parent / "src" / "engram" / "hooks" / "claude"
_HOOKLIB_PATH = _HOOKS_DIR / "_hooklib.py"
_HARNESS_PATH = _HOOKS_DIR / "_harness.py"


def _load_module(name: str, path: Path):
    """Load a hook-lib module fresh, isolated per test (mirrors
    tests/test_hooklib.py's own _load_module -- same reason: these modules
    are dynamically loaded by real hooks via a runtime sys.path insert, not
    a static top-level `import`, so tests load them the same way rather
    than assuming a package import path exists)."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_sys_path():
    """Snapshot/restore sys.path so a test's insertion doesn't leak (same
    discipline as test_hooklib.py's own fixture)."""
    before = list(sys.path)
    yield
    sys.path[:] = before


@pytest.fixture()
def hooklib():
    return _load_module("_hooklib_gate_instance", _HOOKLIB_PATH)


@pytest.fixture()
def harness():
    return _load_module("_harness_gate_instance", _HARNESS_PATH)


def _make_body(n: int, item_len: int = 60) -> list:
    """A deliberately-oversized, already-most-important-first body fixture:
    n items, each item_len characters of filler beyond its own prefix, so
    the unbounded/naive render of the full list is far larger than any
    realistic budget/target -- used across the Bound / no-writer / decoy
    fixtures below."""
    return [f"item-{i:04d}-" + ("x" * item_len) for i in range(n)]


# ---------------------------------------------------------------------------
# Item 1 -- Bound: an oversized fixture never exceeds `budget`.
# ---------------------------------------------------------------------------
class TestBound:
    def test_oversized_body_never_exceeds_hard_budget(self, hooklib, harness):
        budgets = harness.get_harness_budgets("claude-code")
        body = _make_body(1000, item_len=200)  # unbounded render would be ~200KB
        out = hooklib.emit_context(
            "tl;dr: a lot happened",
            body,
            budget=budgets["budget"],
            target=budgets["head_budget"],
        )
        assert len(out) <= budgets["budget"]

    def test_oversized_single_item_with_allow_single_overflow_still_bounded(
        self, hooklib
    ):
        """The one documented exception (allow_single_overflow) is scoped to
        `target` only -- it must NEVER let the render exceed `budget`
        (Kepler's Amendment 1). Force budget itself to be smaller than the
        single oversized item to prove the in-band truncation marker kicks
        in even when allow_single_overflow=True."""
        huge_item = "Y" * 5000
        out = hooklib.emit_context(
            "tldr", [huge_item], budget=1000, target=800, allow_single_overflow=True
        )
        assert len(out) <= 1000
        assert "…[truncated" in out

    def test_bound_holds_regardless_of_overflow_writer_presence(self, hooklib):
        """The hard invariant must hold whether or not an overflow_writer is
        supplied, and whether or not a supplied writer itself fails."""
        body = _make_body(200, item_len=150)

        out_no_writer = hooklib.emit_context("tldr", body, budget=2000, target=1500)
        assert len(out_no_writer) <= 2000

        def failing_writer(items):
            raise RuntimeError("disk full")

        out_failing_writer = hooklib.emit_context(
            "tldr", body, budget=2000, target=1500, overflow_writer=failing_writer
        )
        assert len(out_failing_writer) <= 2000
        assert "— see" not in out_failing_writer


# ---------------------------------------------------------------------------
# Item 2 -- Head survives: the leading trailer+head portion is inside its
# budget AND ends on a `\n` line boundary, never mid-line.
# ---------------------------------------------------------------------------
def _no_line_straddles(rendered_prefix: str, original_lines: list, skip_leading: int) -> bool:
    """The check this gate item is actually testing: every line in
    `rendered_prefix` past the first `skip_leading` lines (the trailer, and
    optionally other fixed-format lines) must be a COMPLETE line drawn
    verbatim from `original_lines` -- never a fragment of a longer line
    that got cut mid-way. Shared by the positive test below AND by the
    straddling-line decoy test, so both exercise the SAME check (the decoy
    proves this function is actually sensitive to a bad render, not merely
    a length check in disguise)."""
    lines = rendered_prefix.split("\n")
    return all(line in original_lines for line in lines[skip_leading:])


class TestHeadSurvives:
    def test_oversized_head_fits_target_and_ends_on_line_boundary(self, hooklib, harness):
        budgets = harness.get_harness_budgets("claude-code")
        target = budgets["head_budget"]
        head_lines = [f"line {i:03d}: " + ("y" * 30) for i in range(200)]
        head = "\n".join(head_lines)

        out = hooklib.emit_context(head, [], budget=budgets["budget"], target=target)

        assert len(out) <= target
        # skip_leading=1: line 0 is the trailer, a fixed-format line that is
        # never one of the caller's own head_lines.
        assert _no_line_straddles(out, head_lines, skip_leading=1)

    def test_head_plus_trailer_stays_clean_even_with_body_items_present(
        self, hooklib, harness
    ):
        """The head/line-boundary property must hold even when body items
        also end up shown -- head is sized against the WORST-CASE trailer
        up front, independent of how the body-selection loop resolves."""
        budgets = harness.get_harness_budgets("claude-code")
        target = budgets["head_budget"]
        head_lines = [f"summary-{i:02d}: " + ("z" * 25) for i in range(80)]
        head = "\n".join(head_lines)
        body = _make_body(50, item_len=40)

        out = hooklib.emit_context(head, body, budget=budgets["budget"], target=target)
        assert len(out) <= target

        # head_lines all start with "summary-" and body items all start
        # with "item-" -- disjoint prefixes by construction. A genuine
        # straddling bug would cut a "summary-NN: zzz...z" line mid-way,
        # producing a fragment that STILL starts with "summary-" but does
        # NOT exactly equal any complete original head line -- so checking
        # "every line starting with summary- is an exact original head
        # line" catches a straddle even though we stop iterating at the
        # first non-head-shaped line (a body item or its truncated-marker
        # variant, both of which legitimately don't start with "summary-").
        out_lines = out.split("\n")
        head_portion_lines = []
        for line in out_lines[1:]:  # skip trailer
            if not line.startswith("summary-"):
                break
            assert line in head_lines, (
                f"line {line!r} starts like a head line but doesn't exactly "
                "match any complete original head line -- looks like a "
                "straddled/mid-line head fragment"
            )
            head_portion_lines.append(line)
        assert head_portion_lines, "expected at least one head line to survive"


# ---------------------------------------------------------------------------
# Item 3 -- No writer outside the helper (structural).
# ---------------------------------------------------------------------------
# NOT enforced today (xfail-tracked): 10+ of the 14 real hook emitters do
# not yet route their additionalContext content through emit_context() --
# that migration is WS2 (engram-alpha#2863), explicitly out of scope for
# this PR (WS1: helper + _harness.py + gate only). Hard-failing this check
# today would make THIS PR's own merge red on someone else's future work,
# which is clearly wrong -- so the check exists (real, working structural
# logic below) but is skipped until WS2 activates it by removing the skip
# decorator. This is a deliberate choice named in the WS1 spec itself
# ("Choose (b)"), not an oversight.
class TestNoBareJoinOutsideHelper:
    """Any hook file that assembles a string destined for
    hookSpecificOutput.additionalContext must route that content through
    emit_context() -- a file that mentions the literal string
    "additionalContext" but never calls emit_context() anywhere in its own
    source is a violator.

    Deliberately a per-FILE check, not a per-line regex over `.join(` /
    f-strings: a naive `\\.join\\(` grep over these hook files also matches
    `os.path.join(...)` calls used for completely unrelated filesystem-path
    construction (every one of the 14 emitter hooks does this, for state
    files/config paths/etc. -- calibrated by reading a few of them, e.g.
    engram-lesson-tripwire-hook.py, engram-session-start-hook.py), which
    would make a literal-pattern grep false-positive on nearly every hook
    in the directory regardless of whether its additionalContext content is
    bounded. Per-file "does this emitter call emit_context() at all" is the
    check that actually targets the structural rule (Part C item 3's own
    intent) without that false-positive surface.
    """

    _EXCLUDED = {
        "_hooklib.py",  # emit_context()'s own implementation lives here
        "_harness.py",  # no additionalContext content of its own
    }

    @staticmethod
    def _find_violators() -> list:
        violators = []
        for path in sorted(_HOOKS_DIR.glob("*.py")):
            if path.name in TestNoBareJoinOutsideHelper._EXCLUDED:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if "additionalContext" not in text:
                continue
            if "emit_context(" in text:
                continue
            violators.append(path.name)
        return violators

    @pytest.mark.xfail(
        reason=(
            "engram-alpha#2863 WS2: 10+ of the 14 hook emitters (all but the "
            "handful that already local-cap) are not yet migrated onto "
            "emit_context() -- this is EXPECTED to report violators today. "
            "Remove this xfail once WS2's migration lands and re-run to "
            "confirm the violator list is empty."
        ),
        strict=True,
    )
    def test_every_additionalContext_emitter_routes_through_emit_context(self):
        violators = self._find_violators()
        assert not violators, (
            "hooks building additionalContext content without calling "
            f"emit_context(): {violators}"
        )

    def test_the_check_itself_currently_finds_real_violators(self):
        """Not a gate assertion -- a sanity check on the checking LOGIC
        above, so the xfail above is known to be genuinely xfailing (a
        violator list of zero would make the xfail vacuous, not a passing
        gate)."""
        violators = self._find_violators()
        assert violators, (
            "expected at least one un-migrated hook emitter today (WS2 "
            "hasn't run yet) -- if this is empty, WS2 has landed and the "
            "xfail above should be removed/activated instead."
        )


# ---------------------------------------------------------------------------
# Item 4 -- Trailing-summary decoy MUST FAIL (Kepler's, from #2858's own
# discovery). "A gate that has never been seen to fail is not a gate."
# ---------------------------------------------------------------------------
class TestTrailingSummaryDecoy:
    def test_trailer_last_variant_is_caught_by_the_preview_check(self, harness):
        """Construct a BAD variant that appends the trailer LAST instead of
        leading with it (violating Part B item 2's "trailer LEADS" rule),
        and confirm the head-survives check (which looks for the trailer
        inside the harness's own preview boundary) correctly flags it as
        failing -- proving the check is sensitive to trailer position, not
        just to overall length."""
        budgets = harness.get_harness_budgets("claude-code")
        preview_boundary = budgets["head_budget"]

        body = _make_body(400, item_len=60)
        bad_trailer = "▸ 3 shown · 397 cut — see /tmp/wherever.txt"
        # BAD: body first, trailer LAST -- the harness truncates the TAIL,
        # so a trailer placed here is exactly what gets dropped.
        bad_render = "\n".join(body) + "\n" + bad_trailer

        preview = bad_render[:preview_boundary]
        assert "▸" not in preview, (
            "the trailing-summary decoy should NOT survive the harness's "
            "own preview cut -- if this assertion fails, the gate's "
            "trailer-survives check is not actually sensitive to trailer "
            "position and would pass a genuinely broken hook"
        )

    def test_correct_emit_context_output_does_survive_the_same_check(
        self, hooklib, harness
    ):
        """Positive control for the decoy above: a REAL emit_context()
        render, under the same preview boundary, DOES carry its trailer
        inside the preview -- proving the decoy's failure above is due to
        trailer position, not an overly strict check that would also flag
        correct output."""
        budgets = harness.get_harness_budgets("claude-code")
        preview_boundary = budgets["head_budget"]
        body = _make_body(400, item_len=60)

        good_render = hooklib.emit_context(
            "tldr", body, budget=budgets["budget"], target=preview_boundary
        )
        preview = good_render[:preview_boundary]
        assert "▸" in preview


# ---------------------------------------------------------------------------
# Item 5 -- Straddling-line decoy MUST FAIL (Borges's own, S4 item 5).
# ---------------------------------------------------------------------------
class TestStraddlingLineDecoy:
    def test_naive_char_slice_straddles_a_line_and_is_caught(self, harness):
        """Construct a `head` whose NAIVELY-truncated (plain char slice,
        ignoring line boundaries) rendering straddles the preview boundary
        -- last line starts before the boundary and ends after it -- and
        confirm the SAME check used in TestHeadSurvives correctly flags
        this, proving that check is testing "ends on a line boundary" and
        not merely "is under N characters" (which the naive slice would
        trivially satisfy)."""
        budgets = harness.get_harness_budgets("claude-code")
        target = budgets["head_budget"]
        head_lines = [f"line {i:03d}: " + ("y" * 30) for i in range(200)]
        head = "\n".join(head_lines)

        naive_bad_render = "▸ 0 shown · 0 cut\n" + head
        naive_truncated = naive_bad_render[:target]  # plain slice -- no line awareness

        assert not _no_line_straddles(naive_truncated, head_lines, skip_leading=1), (
            "the naive char-sliced decoy should straddle a line and be "
            "caught -- if this assertion fails, the line-boundary check is "
            "not actually sensitive to straddling lines and would pass a "
            "genuinely broken (mid-line-truncating) implementation"
        )

    def test_real_emit_context_output_does_not_straddle_under_the_same_fixture(
        self, hooklib, harness
    ):
        """Positive control for the decoy above: the SAME oversized head,
        rendered through the real emit_context() (not the naive slice),
        does NOT straddle a line -- proving the decoy's failure is due to
        the naive implementation, not the fixture itself."""
        budgets = harness.get_harness_budgets("claude-code")
        target = budgets["head_budget"]
        head_lines = [f"line {i:03d}: " + ("y" * 30) for i in range(200)]
        head = "\n".join(head_lines)

        out = hooklib.emit_context(head, [], budget=budgets["budget"], target=target)
        assert _no_line_straddles(out, head_lines, skip_leading=1)


# ---------------------------------------------------------------------------
# Item 6 -- Unknown-harness fixture (Kepler's Amendment 2).
# ---------------------------------------------------------------------------
class TestUnknownHarnessFloor:
    def test_unknown_harness_returns_the_conservative_floor_unmeasured(self, harness):
        budgets = harness.get_harness_budgets("some-future-harness-xyz")
        assert budgets["measured"] is False
        assert budgets["budget"] == harness.UNMEASURED_HARNESS_FLOOR["budget"]
        assert budgets["head_budget"] == harness.UNMEASURED_HARNESS_FLOOR["head_budget"]

    def test_known_harness_returns_measured_numbers(self, harness):
        budgets = harness.get_harness_budgets("claude-code")
        assert budgets["measured"] is True
        assert budgets["budget"] == 9_500
        assert budgets["head_budget"] == 1_800

    def test_never_raises_on_odd_harness_strings(self, harness):
        for bad in ["", "CLAUDE-CODE", "  claude-code  ", "gemini-nano", "🤖"]:
            budgets = harness.get_harness_budgets(bad)
            assert isinstance(budgets, dict)
            assert "budget" in budgets and "head_budget" in budgets and "measured" in budgets

    def test_unknown_harness_renders_under_the_floor_and_names_it(self, hooklib, harness):
        """Kepler's own suggested wording: 'so the deficit is visible' --
        gate item 6 requires the render to (a) actually stay within the
        floor's numbers and (b) name the fact it's using an unmeasured
        floor somewhere in the output. emit_context() itself has no notion
        of "measured" (it only ever sees budget/target numbers, per Part B's
        fixed signature) -- surfacing the deficit is the CALLER's job,
        exactly as it is for a real hook: build the fact into `head` before
        calling emit_context(), using the harness dict's own `measured`
        flag to decide whether to."""
        budgets = harness.get_harness_budgets("some-future-harness-xyz")
        assert budgets["measured"] is False

        head = "tl;dr for a hook running under an unrecognized harness"
        if not budgets["measured"]:
            head = f"{head} (unmeasured harness floor)"

        body = _make_body(50, item_len=100)
        out = hooklib.emit_context(
            head, body, budget=budgets["budget"], target=budgets["head_budget"]
        )

        assert len(out) <= budgets["budget"]
        assert "(unmeasured harness floor)" in out
