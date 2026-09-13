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
import re
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


# ---------------------------------------------------------------------------
# engram-alpha#2892 -- a head dropped by Stage 1 must be visible in the
# trailer (previously reported "0 cut" even when the entire head vanished).
# ---------------------------------------------------------------------------
class TestHeadCutSignal:
    """Borges's explicit ask (forum #411): a test that only shows a head
    being dropped proves less than a PAIR -- forced body-item cuts
    reporting real cut counts BESIDE a head cut reporting real head-cut
    status is what makes the trailer's counter the thing under test rather
    than something merely relabelled. See test_head_drop_and_forced_body_cuts_
    signal_independently below for the discriminating pair; the two single-
    axis tests bracket it as controls."""

    def test_head_dropped_whole_signals_head_cut_in_trailer(self, hooklib):
        """A single-line `head` (no newline at all) long enough that
        Stage 1's own line-boundary trim drops it WHOLE (`_trim_to_line_
        boundary` returns "" when no newline fits within the room
        available) -- constructed precisely against the ACTUAL worst-case
        trailer `emit_context()` itself sizes against (n=0, so `cut_count`
        is 0 and the worst-case trailer already reserves room for the
        `head_cut=True` clause per Correction 3), not a guessed number."""
        target = 200
        budget = 5000
        trailer_len = len(hooklib._build_context_trailer(0, 0, head_cut=True))
        head_room = target - trailer_len - 1  # sep=1 for a nonempty head
        head = "x" * (head_room + 50)  # no "\n" anywhere -- forces a full drop

        out = hooklib.emit_context(head, [], budget=budget, target=target)

        assert "head trimmed" in out, (
            "a head dropped whole by Stage 1 must be signalled in the "
            f"trailer -- got: {out!r}"
        )
        assert len(out) <= target
        # The head text itself must genuinely be gone, not merely flagged
        # while still present -- the bug this closes was silence, not a
        # false positive, but a real fix must actually drop the content.
        assert "x" * 20 not in out

    def test_short_head_survives_untouched_no_head_cut_signal(self, hooklib):
        """Control: a `head` comfortably within budget must NOT trigger the
        head-cut signal -- proves the signal is sensitive to an actual cut,
        not merely present on every call."""
        target = 200
        budget = 5000
        head = "short tldr, well within budget"

        out = hooklib.emit_context(head, [], budget=budget, target=target)

        assert "head trimmed" not in out
        assert head in out

    def test_head_drop_and_forced_body_cuts_signal_independently(self, hooklib):
        """The discriminating pair (Borges, forum #411): a head dropped
        WHOLE, forced ALONGSIDE genuine body-item cuts, in the SAME call --
        proving the trailer's body `cut` count stays numerically correct
        and INDEPENDENT of the head-cut signal, rather than the two being
        conflated into one number. A mutant that folded head_cut into the
        `M` count (or vice versa) would fail this pairing even though
        test_head_dropped_whole_signals_head_cut_in_trailer alone would
        not catch it."""
        target = 200
        budget = 5000
        body = _make_body(50, item_len=100)  # forces genuine body-item cuts
        trailer_len = len(hooklib._build_context_trailer(0, len(body), head_cut=True))
        head_room = target - trailer_len - 1
        head = "y" * (head_room + 50)  # no "\n" -- forces a full head drop

        out = hooklib.emit_context(head, body, budget=budget, target=target)

        assert "head trimmed" in out
        match = re.search(r"▸ (\d+) shown · (\d+) cut", out)
        assert match is not None, f"trailer format not found in: {out!r}"
        shown_n, cut_n = int(match.group(1)), int(match.group(2))
        assert cut_n > 0, "expected genuine body-item cuts alongside the head drop"
        assert shown_n + cut_n == len(body), (
            "shown+cut must still account for every body item -- the "
            "head-cut signal must not perturb body accounting"
        )
        # And the head is genuinely gone, same check as the single-axis test.
        assert "y" * 20 not in out


# ---------------------------------------------------------------------------
# engram-alpha#2893 -- emit_context()'s sizing loop measures against a
# generic placeholder overflow-path length; the render must be re-validated
# against `target` once the REAL overflow path is known (Stage 3), not just
# checked against the hard `budget`.
# ---------------------------------------------------------------------------
class TestRealOverflowPathRevalidation:
    """Deliberate bound-hit resolution (stated explicitly in _hooklib.py's
    own comment above the retry loop, per Kepler's Amendment/Borges's
    review ask): when shrinking `shown_n` all the way to 1 still doesn't
    fit `target` against the REAL path, emit_context() degrades to the
    same truncated_first construction Stage 2's own placeholder-sized pass
    already uses -- not a fallback nobody decided on."""

    def test_real_path_longer_than_placeholder_shrinks_shown_n_to_respect_target(
        self, hooklib, harness
    ):
        """Construct the exact boundary the spec asks for: the placeholder-
        sized Stage-2 selection is pinned (via `target = len(baseline)`) to
        land EXACTLY at the edge where a real overflow path longer than the
        placeholder pushes the same shown_n over `target`. Assert the FINAL
        rendered output still respects `target` (not just `budget`)."""
        budgets = harness.get_harness_budgets("claude-code")
        body = _make_body(60, item_len=20)

        def passthrough_writer(items):
            # Returns the EXACT placeholder path Stage 2 already sized
            # against -- makes this call's own render exactly what Stage 2
            # alone would have produced (see the paired-direction test
            # below for why this establishes the baseline precisely).
            return hooklib._placeholder_overflow_path()

        baseline = hooklib.emit_context(
            "", body, budget=budgets["budget"], target=1800, overflow_writer=passthrough_writer
        )
        target = len(baseline)  # pin the boundary with zero slack

        delta = 4  # matches the spec's own "tripwire-overflow" (17) vs
        # "hook-overflow" (13) real-world example

        def longer_writer(items):
            return hooklib._placeholder_overflow_path() + ("Z" * delta)

        real_out = hooklib.emit_context(
            "", body, budget=budgets["budget"], target=target, overflow_writer=longer_writer
        )

        assert len(real_out) <= target, (
            f"real overflow path ({delta} chars longer than the placeholder "
            f"Stage 2 sized against) pushed the render past `target`: "
            f"{len(real_out)} > {target}"
        )
        baseline_shown = int(re.search(r"▸ (\d+) shown", baseline).group(1))
        real_shown = int(re.search(r"▸ (\d+) shown", real_out).group(1))
        assert real_shown < baseline_shown, (
            "expected the retry loop to actually shrink shown_n -- if this "
            "fails, the fixture no longer lands at the boundary this test "
            "is meant to exercise"
        )

    def test_real_path_same_length_as_placeholder_leaves_output_byte_identical(
        self, hooklib, harness
    ):
        """Paired control (Borges's explicit ask): when the real overflow
        path is the SAME length as the placeholder, the retry loop's
        `while` condition must never become true (it should exit
        immediately on the first check), and the output must be
        byte-identical to what Stage 2 alone would have produced. Using a
        writer that returns the placeholder path VERBATIM makes this
        exact, not merely same-length -- Stage 3's own real-path render is
        then, by construction, the identical computation Stage 2's sizing
        loop already performed for that same (shown_n, cut_n) pair."""
        budgets = harness.get_harness_budgets("claude-code")
        body = _make_body(60, item_len=20)

        def passthrough_writer(items):
            return hooklib._placeholder_overflow_path()

        baseline = hooklib.emit_context(
            "", body, budget=budgets["budget"], target=1800, overflow_writer=passthrough_writer
        )
        target = len(baseline)

        same_out = hooklib.emit_context(
            "", body, budget=budgets["budget"], target=target, overflow_writer=passthrough_writer
        )

        assert same_out == baseline, (
            "a same-length (here: identical) real overflow path must never "
            "perturb the render the placeholder-sized pass already chose"
        )

    def test_real_path_shorter_than_placeholder_leaves_shown_n_unaffected(
        self, hooklib, harness
    ):
        """Paired control, the other direction: a real overflow path
        SHORTER than the placeholder must also leave `shown_n` untouched --
        the retry loop only ever fires when the real path makes things
        WORSE, never when it makes them better. Text differs (a different,
        shorter path string), so this checks shown/cut counts rather than
        byte-for-byte equality -- the byte-identical case is covered by the
        same-length test above."""
        budgets = harness.get_harness_budgets("claude-code")
        body = _make_body(60, item_len=20)

        def passthrough_writer(items):
            return hooklib._placeholder_overflow_path()

        baseline = hooklib.emit_context(
            "", body, budget=budgets["budget"], target=1800, overflow_writer=passthrough_writer
        )
        target = len(baseline)
        baseline_shown = int(re.search(r"▸ (\d+) shown", baseline).group(1))

        def shorter_writer(items):
            return hooklib._placeholder_overflow_path()[:-2]  # 2 chars shorter

        shorter_out = hooklib.emit_context(
            "", body, budget=budgets["budget"], target=target, overflow_writer=shorter_writer
        )
        shorter_shown = int(re.search(r"▸ (\d+) shown", shorter_out).group(1))

        assert shorter_shown == baseline_shown, (
            "a real overflow path shorter than the placeholder must never "
            "trigger the shrink-and-retry loop"
        )
        assert len(shorter_out) <= target

    def test_shrink_loop_keeps_overflow_content_in_sync_with_final_cut_set(
        self, hooklib, harness
    ):
        """Independent-review catch (engram-alpha#2892/#2893 follow-up,
        found on this round's own build): the retry loop above
        re-validates the RENDER against `target` on every shrink, but
        before this fix it reused the `overflow_path` computed ONCE,
        before the loop -- so the overflow FILE's own content could
        silently go stale relative to the cut set the render actually
        settled on. This test recovers the writer's calls (a stand-in for
        "the file actually on disk when emit_context() returns") and
        asserts the LAST call covers every item the FINAL render reports
        as cut -- not just the items cut at the loop's first (pre-shrink)
        iteration.

        Constructed to fire the loop EXACTLY once (shown_n shrinks by 1)
        without reaching the shown_n == 1 floor -- the floor-degrade path
        is covered by the other tests in this class; this one is scoped
        to the mid-loop staleness the floor-degrade branch was never
        exposed to."""
        budgets = harness.get_harness_budgets("claude-code")
        body = _make_body(60, item_len=100)

        def passthrough_writer(items):
            return hooklib._placeholder_overflow_path()

        baseline = hooklib.emit_context(
            "", body, budget=budgets["budget"], target=1800, overflow_writer=passthrough_writer
        )
        target = len(baseline)
        baseline_shown = int(re.search(r"▸ (\d+) shown", baseline).group(1))

        delta = 4  # matches the sibling tests' own real-path-longer offset
        calls = []  # every overflow_writer call's items, in call order

        def recording_writer(items):
            calls.append(list(items))
            return hooklib._placeholder_overflow_path() + ("Z" * delta)

        real_out = hooklib.emit_context(
            "", body, budget=budgets["budget"], target=target, overflow_writer=recording_writer
        )
        real_shown = int(re.search(r"▸ (\d+) shown", real_out).group(1))

        assert real_shown < baseline_shown, (
            "expected the retry loop to actually shrink shown_n -- if "
            "this fails, the fixture no longer lands at the boundary "
            "this test is meant to exercise"
        )
        assert real_shown > 1, (
            "expected the loop to settle above the shown_n == 1 floor -- "
            "this test is scoped to the mid-loop case, not the "
            "floor-degrade case the other tests in this class already "
            "cover"
        )
        assert len(calls) >= 1, "overflow_writer was never called"

        final_cut_items = body[real_shown:]
        assert calls[-1] == final_cut_items, (
            "the overflow file's LAST write must cover every item the "
            "FINAL render reports as cut -- if this fails, the overflow "
            "path written to disk is stale relative to the cut set the "
            "render actually settled on (the file the trailer's "
            "'-- see <path>' clause points at is missing content)"
        )

    def test_floor_degrade_respects_allow_single_overflow_with_real_path(
        self, hooklib, harness
    ):
        """Reviewer-fairy's Suggestion 1 on PR #2898 (APPROVE, non-blocking):
        the floor-degrade condition's own
        `not (allow_single_overflow and len(rendered) <= budget)` clause --
        verified correct by the reviewer via a real A/B (removing it
        changed real output from 518 untruncated chars to 605
        forcibly-truncated chars for an `allow_single_overflow=True` call
        landing at this floor) -- was UNTESTED: no existing fixture in this
        class drives the shrink-and-retry loop all the way down to
        `shown_n == 1` AND still exceeds `target` there against the REAL
        (writer-produced) overflow path. The sibling tests above stop once
        the loop finds ANY fit above the floor; this one forces it all the
        way down.

        Fixture: 5 oversized (800-char) body items under `target=1800` --
        only 2 fit even against the placeholder path, so Stage 2 lands
        `shown_n=2`, `cut_n=3`. Pinning `target = len(baseline)` (zero
        slack) and then supplying a real overflow path 1000 chars LONGER
        than the placeholder -- empirically larger than the ~811 chars
        saved by dropping one more 800-char item, so the shrink IS still
        insufficient once it reaches the floor -- forces the while loop to
        shrink `shown_n` from 2 to 1 and land there STILL over `target`
        (~189 chars over, measured; `delta` was chosen larger than a
        single item's own savings, not tuned to hit an exact number)."""
        budgets = harness.get_harness_budgets("claude-code")
        body = _make_body(5, item_len=800)

        def passthrough_writer(items):
            return hooklib._placeholder_overflow_path()

        baseline = hooklib.emit_context(
            "", body, budget=budgets["budget"], target=1800, overflow_writer=passthrough_writer
        )
        target = len(baseline)
        baseline_shown = int(re.search(r"▸ (\d+) shown", baseline).group(1))
        assert baseline_shown == 2, (
            "fixture assumption: Stage 2 (against the placeholder path) "
            "should land shown_n=2 out of 5 oversized items -- if this "
            "fails, the fixture no longer sets up the floor-boundary case "
            "this test targets"
        )

        delta = 1000  # empirically larger than the ~811 chars saved by
        # dropping one more 800-char item -- see docstring above

        def longer_writer(items):
            return hooklib._placeholder_overflow_path() + ("Z" * delta)

        rescued = hooklib.emit_context(
            "",
            body,
            budget=budgets["budget"],
            target=target,
            overflow_writer=longer_writer,
            allow_single_overflow=True,
        )
        rescued_shown = int(re.search(r"▸ (\d+) shown", rescued).group(1))
        assert rescued_shown == 1, (
            "expected the shrink-and-retry loop to reach the shown_n == 1 "
            "floor -- if this fails, the fixture doesn't force the floor "
            "case this test is meant to exercise"
        )
        assert len(rescued) > target, (
            "expected the floor-degrade render to still exceed `target` "
            "even at shown_n == 1 -- if this fails, the fixture's delta "
            "isn't large enough to land at the actual floor-degrade "
            "condition (it fits under target before ever reaching it)"
        )
        assert len(rescued) <= budgets["budget"], (
            "allow_single_overflow=True never permits exceeding the hard "
            "budget, only the soft target"
        )
        assert "…[truncated" not in rescued, (
            "allow_single_overflow=True at the floor-degrade branch should "
            "RESCUE the render (let it run past `target`, still within "
            "`budget`) rather than forcibly truncate it -- this is the "
            "clause the reviewer's A/B verified but no existing test "
            "exercised"
        )

        # Paired control (mirroring this PR's own established pattern for
        # paired controls throughout this class): the SAME fixture with
        # allow_single_overflow=False (the default) must produce the
        # truncated/marker output at the same floor -- confirming the flag
        # genuinely discriminates the two outcomes, not just that the
        # rescued case happens to look fine on its own.
        truncated = hooklib.emit_context(
            "",
            body,
            budget=budgets["budget"],
            target=target,
            overflow_writer=longer_writer,
        )
        truncated_shown = int(re.search(r"▸ (\d+) shown", truncated).group(1))
        assert truncated_shown == 1
        assert "…[truncated" in truncated, (
            "without allow_single_overflow, the SAME floor-boundary "
            "fixture must forcibly truncate -- if this fails, the paired "
            "control doesn't actually discriminate the flag's effect"
        )
