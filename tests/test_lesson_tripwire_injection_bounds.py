"""Tests: engram-alpha#2863 WS2 -- engram-lesson-tripwire-hook.py's
migration onto `_hooklib.emit_context()`.

This hook is the special case in the WS2 migration wave: its own bespoke
render mechanism (select_and_cap_hits / _render_tripwire_block /
_build_trailer / its own _placeholder_overflow_path -- all now REMOVED)
was the REFERENCE IMPLEMENTATION `_hooklib.emit_context()` was generalized
FROM (see that function's own docstring, and engram-lesson-tripwire-hook.py's
module docstring). This file is the analogue of
tests/test_surface_hook_injection_bounds.py for THIS hook.

Companion to tests/test_lesson_tripwire.py (the hook's own, pre-existing
unit-test suite -- unit tests for load_tripwires/check_command/deny/
shadow-block/demotion logic, plus the #2858 preview-cap regression tests,
two of which were updated in this same change to assert through main()'s
observable output instead of the two now-removed internal functions they
used to call directly: _build_trailer and select_and_cap_hits). This file
adds the WS2-specific bound/head/ordering coverage the shared migration
spec calls for:

  1. Worst-case fixture forcing many simultaneous tripwire matches --
     TestWorstCaseInjectionBounds (never exceeds budget=9500).
  2. Head-boundary assertion (spy on emit_context(), assert on the `head`
     argument directly) -- TestHeadBoundarySpy (stays within target=1800;
     for this hook `head` is the FIXED TRIPWIRE_HEADER constant, so this is
     trivially satisfied today, but the assertion is a real regression
     guard against a future change that makes `head` grow with input).
  3. Specificity-ordering regression, reproducing
     tests/test_lesson_tripwire.py's own 20-match ordering assertions
     through the SAME call path -- TestSpecificityOrderingPreserved.
  4. Nothing-fires / common-case sanity -- TestCommonCaseSanity (unlike
     engram-surface-hook.py, this hook's main() calls a bare `sys.exit(0)`
     with NO stdout at all when nothing fires, rather than printing `{}` --
     a real, pre-existing difference between the two hooks' early-exit
     contracts, not something this migration changed).
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
import unittest.mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HOOK_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-lesson-tripwire-hook.py"
_HARNESS_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "_harness.py"


def _load_hook():
    """Import the hook as a module (hyphenated filename, not importable via
    normal import -- same pattern as tests/test_lesson_tripwire.py's own
    _import_hook())."""
    spec = importlib.util.spec_from_file_location("lesson_tripwire_hook_bounds_pt", str(_HOOK_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_harness_budgets(harness: str = "claude-code"):
    """Return get_harness_budgets(harness) for a fresh _harness.py load.

    Pinned to "claude-code" explicitly rather than relying on the hook's
    own detect_harness() call: this dev/source checkout has no baked
    platform.json (see _harness.py's own docstring), so detect_harness()
    would return None here and get_harness_budgets(None) would silently
    fall through to UNMEASURED_HARNESS_FLOOR (4,000 / 1,800->1,200 -- NOT
    claude-code's measured 9,500 / 1,800) instead of the numbers this
    file's own worst-case assertions are written against. Every test below
    that cares about the exact budget/target numbers also monkeypatches
    mod.detect_harness so main() itself uses the SAME pinned harness at
    runtime (same discipline as test_surface_hook_injection_bounds.py's
    own _load_harness_budgets() docstring)."""
    spec = importlib.util.spec_from_file_location("_harness_lesson_bounds_pt", _HARNESS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.get_harness_budgets(harness)


def _run_hook_main(mod, hook_input: dict, *, tripwires=None, recall: str = "", prevention_lessons=None) -> str:
    """Run the hook's main() with controlled tripwires/recall/
    prevention_lessons; return raw stdout. Mirrors
    tests/test_lesson_tripwire.py's own _run_hook_main() exactly, but
    against a dynamically-loaded module instance (`mod`) rather than the
    shared `_hook` this test file's sibling uses, so each test here can
    load its own fresh copy and monkeypatch it (e.g. detect_harness)
    without leaking into other test files."""
    output_buf = io.StringIO()
    with (
        unittest.mock.patch.object(mod, "load_tripwires", return_value=tripwires or []),
        unittest.mock.patch.object(mod, "load_prevention_lessons", return_value=prevention_lessons or []),
        unittest.mock.patch.object(mod, "check_in_turn_recall", return_value=recall),
        unittest.mock.patch("sys.stdin", io.StringIO(json.dumps(hook_input))),
        unittest.mock.patch("sys.stdout", output_buf),
    ):
        try:
            mod.main()
        except SystemExit:
            pass
    return output_buf.getvalue()


def _seeded_hits(n: int, *, nudge_len: int = 100):
    """n distinct (pattern, nudge, node_id) tripwire tuples with strictly
    increasing pattern length (so specificity order -- descending pattern
    length -- is unambiguous, no ties), and deliberately long nudge text so
    a large `n` forces emit_context() to actually cut something rather than
    everything trivially fitting. Mirrors
    tests/test_lesson_tripwire.py's own TestTripwirePreviewCap fixture
    shape exactly, so results are directly comparable to that file's own
    20-match assertions."""
    patterns, nudges, node_ids = [], [], []
    for i in range(n):
        pattern = "wsboundsmark" + str(i).zfill(3) + ("Z" * i)
        nudge = f"NUDGE_{i:03d} " + ("x" * nudge_len)
        node_id = f"ls_wsb{i:03d}"
        patterns.append(pattern)
        nudges.append(nudge)
        node_ids.append(node_id)
    tripwires = [(patterns[i], nudges[i], node_ids[i]) for i in range(n)]
    command = " ".join(patterns)
    return tripwires, command, patterns, nudges, node_ids


class TestWorstCaseInjectionBounds(unittest.TestCase):
    """Item 1 of the WS2 spec's "New tests to add": a worst-case fixture
    forcing many simultaneous tripwire matches must never exceed the
    harness's hard budget."""

    def test_worst_case_many_matches_never_exceeds_hard_budget(self):
        mod = _load_hook()
        budgets = _load_harness_budgets("claude-code")
        unittest.mock.patch.object(mod, "detect_harness", lambda: "claude-code").start()
        self.addCleanup(unittest.mock.patch.stopall)

        n = 60
        tripwires, command, patterns, nudges, node_ids = _seeded_hits(n, nudge_len=200)

        out = _run_hook_main(
            mod, {"tool_name": "Bash", "tool_input": {"command": command}},
            tripwires=tripwires,
        )
        self.assertTrue(out.strip(), "expected JSON output given 60 seeded matches")
        response = json.loads(out.strip())
        additional_context = response["hookSpecificOutput"]["additionalContext"]

        self.assertLessEqual(
            len(additional_context), budgets["budget"],
            f"additionalContext ({len(additional_context)} chars) exceeded "
            f"the hard budget ({budgets['budget']}) under a 60-match worst case",
        )
        # A real cap must actually have fired (sanity on the fixture itself,
        # same discipline as test_lesson_tripwire.py's own 20-match test):
        # not all 60 large nudges can fit under target=1800.
        first_line = additional_context.split("\n", 1)[0]
        self.assertTrue(first_line.startswith("▸ "))
        self.assertIn(" cut", first_line)
        self.assertNotEqual(
            first_line.split("shown")[0].strip(), f"▸ {n}",
            "expected fewer than all 60 hits shown -- the fixture should "
            "force a real cut, not trivially fit everything",
        )


class TestHeadBoundarySpy(unittest.TestCase):
    """Item 2 of the WS2 spec's "New tests to add": spy on emit_context()
    and assert on the `head` argument DIRECTLY (not on the final rendered
    string, which could coincidentally look short due to trailer placement
    even if head-construction had a latent bug) -- stays within
    target=1800. For this hook `head` is the fixed TRIPWIRE_HEADER
    constant (never built from variable-length input), so this is a
    regression guard rather than a live risk today."""

    def test_head_argument_stays_within_target_even_under_worst_case(self):
        mod = _load_hook()
        budgets = _load_harness_budgets("claude-code")
        unittest.mock.patch.object(mod, "detect_harness", lambda: "claude-code").start()
        self.addCleanup(unittest.mock.patch.stopall)

        n = 60
        tripwires, command, patterns, nudges, node_ids = _seeded_hits(n, nudge_len=200)

        captured = {}
        real_emit_context = mod.emit_context

        def _spy_emit_context(head, body, **kwargs):
            captured["head"] = head
            captured["body"] = list(body)
            return real_emit_context(head, body, **kwargs)

        unittest.mock.patch.object(mod, "emit_context", _spy_emit_context).start()

        out = _run_hook_main(
            mod, {"tool_name": "Bash", "tool_input": {"command": command}},
            tripwires=tripwires,
        )
        self.assertTrue(out.strip())
        self.assertIn("head", captured, "emit_context was never called")
        self.assertEqual(captured["head"], mod.TRIPWIRE_HEADER)
        self.assertLessEqual(
            len(captured["head"]), budgets["head_budget"],
            f"head ({len(captured['head'])} chars) exceeded head_budget "
            f"({budgets['head_budget']})",
        )
        self.assertLessEqual(len(captured["head"]), 1800)


class TestSpecificityOrderingPreserved(unittest.TestCase):
    """Item 3 of the WS2 spec: reproduce
    tests/test_lesson_tripwire.py::TestTripwirePreviewCap's own 20-match
    specificity-ordering assertions through the SAME call path (main() ->
    emit_context()), confirming the fold onto the shared helper preserved
    byte-identical selection/ordering behavior -- not just "a cap
    happened", but that the SHOWN hits are exactly the longest-K patterns
    (the highest-index seeds, since pattern length increases with i)."""

    def test_20_match_seeded_case_preserves_specificity_order(self):
        mod = _load_hook()
        unittest.mock.patch.object(mod, "detect_harness", lambda: "claude-code").start()
        self.addCleanup(unittest.mock.patch.stopall)

        n = 20
        tripwires, command, patterns, nudges, node_ids = _seeded_hits(n, nudge_len=100)

        out = _run_hook_main(
            mod, {"tool_name": "Bash", "tool_input": {"command": command}},
            tripwires=tripwires,
        )
        response = json.loads(out.strip())
        additional_context = response["hookSpecificOutput"]["additionalContext"]

        first_line = additional_context.split("\n", 1)[0]
        import re as _re
        m = _re.match(r"▸ (\d+) shown · (\d+) cut", first_line)
        self.assertIsNotNone(m, f"unexpected trailer shape: {first_line!r}")
        shown_count = int(m.group(1))
        cut_count = int(m.group(2))
        self.assertEqual(shown_count + cut_count, n)
        self.assertGreater(cut_count, 0, "fixture must force some cuts")

        expected_shown_indices = set(range(n - shown_count, n))
        expected_cut_indices = set(range(0, n - shown_count))
        for i in expected_shown_indices:
            self.assertIn(nudges[i], additional_context, f"pattern index {i} should be SHOWN")
        for i in expected_cut_indices:
            self.assertNotIn(nudges[i], additional_context, f"pattern index {i} should be CUT")


class TestCommonCaseSanity(unittest.TestCase):
    """Item 4: the typical/no-op cases."""

    def test_single_small_match_zero_cut(self):
        mod = _load_hook()
        unittest.mock.patch.object(mod, "detect_harness", lambda: "claude-code").start()
        self.addCleanup(unittest.mock.patch.stopall)

        out = _run_hook_main(
            mod, {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}},
            tripwires=[(r"rm -rf", "Think twice before rm -rf.", "ls_rmrf")],
        )
        response = json.loads(out.strip())
        additional_context = response["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(additional_context.split("\n", 1)[0], "▸ 1 shown · 0 cut")
        self.assertIn("Think twice before rm -rf.", additional_context)

    def test_nothing_fires_produces_no_stdout(self):
        """Unlike engram-surface-hook.py (which prints `{}` when nothing
        fires), this hook's main() calls a bare sys.exit(0) with NO stdout
        at all when `parts` stays empty -- a pre-existing difference in
        this hook's own early-exit contract, unaffected by this
        migration."""
        mod = _load_hook()
        unittest.mock.patch.object(mod, "detect_harness", lambda: "claude-code").start()
        self.addCleanup(unittest.mock.patch.stopall)

        out = _run_hook_main(
            mod, {"tool_name": "Bash", "tool_input": {"command": "echo hello"}},
            tripwires=[],
        )
        self.assertEqual(out, "", "expected no stdout when nothing fires")


class TestBoundaryMarginKnownSizingGap(unittest.TestCase):
    """Documented regression guard for the known #2893 sizing gap (found by
    reviewer-fairy round 1 on PR #2890, systematic 3500-fixture sweep):
    `_hooklib.emit_context()`'s sizing loop measures against a generic
    13-char placeholder overflow-path name, but this hook's real
    `write_tripwire_overflow()` persists to a 17-char-named directory -- a
    4-char measurement/render delta that lets the rendered
    `additionalContext` land a few characters OVER the soft
    `target`/`TRIPWIRE_BLOCK_CHAR_CEILING` (1800) on some inputs, though
    NEVER over the hard `budget`. This is a pre-existing gap in the
    already-merged WS1 `_hooklib.py`, not introduced by this hook's
    migration and not this PR's to fix.

    Deliberately asserts neither `<= TRIPWIRE_BLOCK_CHAR_CEILING` (would
    fail intermittently given the known gap -- this fixture is chosen
    specifically because it lands at/past the ceiling) NOR `<= budget` (far
    too loose -- the hard budget is 9500 and is never at risk here; that
    assertion would pass even if the soft-ceiling overshoot grew far
    larger than the known gap). The margin below is chosen to comfortably
    cover the known 2-4 char overshoot with buffer, so this test is a real
    regression guard against the overshoot growing unexpectedly larger,
    not a rubber stamp on the current gap.

    Note on exactness: `write_tripwire_overflow()`'s on-disk path
    (`ENGRAM_HOME/tripwire-overflow/<unix-ts>-<pid>.txt`) feeds into the
    rendered trailer, so the EXACT overshoot for a given fixture shifts by
    a couple of characters with `ENGRAM_HOME`'s path length and the
    running process's pid digit-width (both environment-dependent, e.g.
    this repo's own conftest.py points `ENGRAM_HOME` at a per-invocation
    tempdir). The fixture below was chosen, and is checked, to land WITHIN
    a documented near-boundary band regardless of that variance -- the
    upper-bound assertion is the actual regression guard; the lower-bound
    sanity check only confirms the fixture is still meaningfully close to
    the boundary, without requiring it to literally cross 1800 on every
    machine.

    # TODO(#2893): tighten to exact TRIPWIRE_BLOCK_CHAR_CEILING once the
    # WS1 sizing gap is fixed.
    """

    # Margin above the soft ceiling: covers the known 2-4 char overshoot
    # (see #2893) plus buffer, while staying far below the hard budget
    # (9500) so a real regression (uncapped growth) still fails loudly.
    _KNOWN_SIZING_GAP_MARGIN = 10

    # How far below the ceiling the fixture is still considered
    # "near-boundary" for the sanity check -- wide enough to absorb the
    # environment-dependent path-length variance described above, narrow
    # enough that a fixture which drifted far from the boundary (e.g. due
    # to an unrelated future change to emit_context()'s sizing) still
    # fails this check rather than passing vacuously.
    _NEAR_BOUNDARY_TOLERANCE = 30

    def test_near_boundary_fixture_stays_within_documented_margin(self):
        mod = _load_hook()
        unittest.mock.patch.object(mod, "detect_harness", lambda: "claude-code").start()
        self.addCleanup(unittest.mock.patch.stopall)

        # Fixture chosen (by direct probe against this hook's own main(),
        # run through THIS repo's actual test harness/conftest.py) to land
        # at/just past TRIPWIRE_BLOCK_CHAR_CEILING under the known #2893
        # gap -- 13 hits, nudge_len=121 renders at ~1802 chars under this
        # test suite's ENGRAM_HOME, matching the reviewer's 1802-1803-char
        # range from the broader sweep.
        n = 13
        tripwires, command, patterns, nudges, node_ids = _seeded_hits(n, nudge_len=121)

        out = _run_hook_main(
            mod, {"tool_name": "Bash", "tool_input": {"command": command}},
            tripwires=tripwires,
        )
        self.assertTrue(out.strip(), "expected JSON output given 13 seeded matches")
        response = json.loads(out.strip())
        additional_context = response["hookSpecificOutput"]["additionalContext"]

        self.assertLessEqual(
            len(additional_context),
            mod.TRIPWIRE_BLOCK_CHAR_CEILING + self._KNOWN_SIZING_GAP_MARGIN,
            f"additionalContext ({len(additional_context)} chars) exceeded "
            f"TRIPWIRE_BLOCK_CHAR_CEILING ({mod.TRIPWIRE_BLOCK_CHAR_CEILING}) "
            f"+ the documented #2893 sizing-gap margin "
            f"({self._KNOWN_SIZING_GAP_MARGIN})",
        )
        # Sanity on the fixture itself: this case is only a meaningful
        # regression guard if it actually lands near the boundary --
        # otherwise it would be indistinguishable from a fixture that
        # never approached the boundary at all. Uses a tolerance band
        # (rather than a strict `> ceiling`) because the exact overshoot
        # is environment-sensitive (see class docstring); a fixture that
        # renders a few chars UNDER the ceiling on some machine is still a
        # valid near-boundary case, just not a demonstrated-overshoot one.
        self.assertGreaterEqual(
            len(additional_context),
            mod.TRIPWIRE_BLOCK_CHAR_CEILING - self._NEAR_BOUNDARY_TOLERANCE,
            "fixture no longer lands near the boundary -- if emit_context()'s "
            "sizing changed, re-probe for a fixture that lands near "
            f"TRIPWIRE_BLOCK_CHAR_CEILING ({mod.TRIPWIRE_BLOCK_CHAR_CEILING}); "
            "if the #2893 sizing gap itself was fixed, tighten this test per "
            "the TODO(#2893) above instead of leaving it stale",
        )


if __name__ == "__main__":
    unittest.main()
