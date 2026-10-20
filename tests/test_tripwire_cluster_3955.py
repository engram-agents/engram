"""Contract tests for alpha #3955 (part of #3911): the cluster block never drops the head nudge whole.

At 19-21 co-firing lessons the one-liners plus a median-length head nudge exceed the harness budget.
Before this change emit_context cut the nudge WHOLE while the header still said "full text of the
first below". Now the head nudge is rendered truncated to the REMAINING budget (cut from the end,
one explicit marker line, the overflow file keeps the full text) and the header says so; if not even
a short prefix fits, the one-liners win and the header says the nudge was not shown (never "below").

Written BEFORE the implementation (contract-first). Fixture ids are `lsx-NNN` (not the real node-id
shape); the nudge text is generic, line-structured, ~1.7k chars (the median real length).

Cells:
  T1  21 co-fires, 1.7k head nudge, unmeasured-floor budget: prefix inline, header names the
      truncation, 21 one-liners inline, overflow file holds the FULL nudge, block within budget
  T2  full nudge fits -> header has no truncation wording, nudge whole
  T3  budget too small for even a short prefix -> one-liners win, header says "not shown", no "below"
  T4  cut lands on valid text: no lone surrogate, exactly one marker line
  T5  at or below CLUSTER_THRESHOLD nothing changes; fallback on exception keeps today's bullets
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import unittest.mock
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HOOK_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-lesson-tripwire-hook.py"

CMD = "gh pr comment 1 --body x"
LINE_RE = re.compile(r"^  - (lsx-\d{3}): (.*)$", re.M)
FLOOR_BUDGET = 4000   # UNMEASURED_HARNESS_FLOOR["budget"]: what an unmeasured harness gets
MARKER_RE = re.compile(r"^  …\[nudge truncated[^\n]*\]$", re.M)


@pytest.fixture()
def hook(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("lesson_tripwire_hook_3955", str(_HOOK_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
    monkeypatch.setattr(mod, "LOADER_NOTICE_STAMP_PATH", tmp_path / "stamp.json", raising=False)
    monkeypatch.setattr(mod, "LESSON_RANKS_PATH", tmp_path / "lesson-fire-ranks.json", raising=False)
    return mod


def lid(i):
    return f"lsx-{i:03d}"


def long_nudge(tag, size=1700):
    """A median-length nudge: many short lines (highest-value first), ~size chars."""
    lines = [f"NUDGE-{tag} line {n:02d}: " + "word " * 12 for n in range(1, 60)]
    return "\n".join(lines)[:size]


def tw(i, nudge=None, summary=None):
    summary = summary or f"SUM-{lid(i)} " + "a realistic recall summary sentence of moderate length " * 2
    return ("gh pr comment" + "(?:)" * i, nudge if nudge is not None else long_nudge(lid(i)),
            lid(i), "normal", "full", summary, "claim")


def run(hook, monkeypatch, tws, budget=FLOOR_BUDGET):
    monkeypatch.setattr(hook, "load_tripwires", lambda: tws)
    monkeypatch.setattr(hook, "load_prevention_lessons", lambda: [])
    monkeypatch.setattr(hook, "check_in_turn_recall", lambda *a, **k: "")
    monkeypatch.setattr(hook, "_ws4_config", lambda: {"active": False, "seed": ""})
    monkeypatch.setattr(hook, "_append_prevention_ledger", lambda *a, **k: None)
    monkeypatch.setattr(hook, "get_harness_budgets", lambda h: {"budget": budget, "head_budget": 1800})
    out = io.StringIO()
    hi = {"tool_name": "Bash", "tool_input": {"command": CMD}}
    with unittest.mock.patch("sys.stdin", io.StringIO(json.dumps(hi))), unittest.mock.patch("sys.stdout", out):
        try:
            hook.main()
        except SystemExit:
            pass
    return json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]


def header_of(ctx):
    return [ln for ln in ctx.split("\n") if ln.startswith("[lesson-tripwire]")][0]


def head_lesson(ctx):
    found = re.findall(r"NUDGE-(lsx-\d{3})", ctx)
    return found[0] if found else None


def one_liners(ctx):
    return [m.group(1) for m in LINE_RE.finditer(ctx)]


class TestTruncatedHeadNudge:
    def test_21_cofires_long_nudge_prefix_inline_header_names_truncation(self, hook, monkeypatch, tmp_path):
        tws = [tw(i) for i in range(1, 22)]
        ctx = run(hook, monkeypatch, tws)
        assert len(ctx) <= FLOOR_BUDGET
        assert sorted(one_liners(ctx)) == [lid(i) for i in range(1, 22)]
        head = head_lesson(ctx)
        assert head == lid(21), "the head nudge must not be cut whole"
        full = long_nudge(head)
        shown = ctx.split("  • ", 1)[1].split("\n  …[nudge truncated")[0]
        assert 0 < len(shown) < len(full) and full.startswith(shown)
        hdr = header_of(ctx)
        assert "truncated" in hdr and "full text of the first below" not in hdr
        m = re.search(r"(\S*tripwire-overflow\S*\.txt)", ctx)
        assert m
        assert full in Path(m.group(1)).read_text()
        assert len(MARKER_RE.findall(ctx)) == 1
        assert re.search(r"▸ \d+ shown · 0 cut", ctx)

    def test_truncation_fills_the_remaining_budget(self, hook, monkeypatch, tmp_path):
        ctx = run(hook, monkeypatch, [tw(i) for i in range(1, 22)])
        assert FLOOR_BUDGET - len(ctx) < 400, "the prefix should use most of the remaining room"


class TestFullNudgeFits:
    def test_header_has_no_truncation_wording_and_nudge_is_whole(self, hook, monkeypatch, tmp_path):
        tws = [tw(i, nudge=f"NUDGE-{lid(i)} short") for i in range(1, 22)]
        ctx = run(hook, monkeypatch, tws)
        hdr = header_of(ctx)
        assert "one line each, full text of the first below; all 21 full texts: " in hdr
        assert "truncated" not in ctx and "not shown" not in hdr
        assert "  • NUDGE-lsx-021 short" in ctx

    def test_ample_budget_whole_nudge(self, hook, monkeypatch, tmp_path):
        ctx = run(hook, monkeypatch, [tw(i) for i in range(1, 22)], budget=20000)
        assert "truncated" not in ctx
        assert long_nudge(lid(21)) in ctx


class TestNothingFits:
    def test_header_says_not_shown_and_never_below(self, hook, monkeypatch, tmp_path):
        tws = [tw(i) for i in range(1, 22)]
        full = run(hook, monkeypatch, tws, budget=20000)
        lines_len = sum(len(ln) + 1 for ln in full.split("\n") if ln.startswith("  - "))
        hdr_len = len(full[: full.index("  - lsx")])
        budget = hdr_len + lines_len + 90     # one-liners fit (64 tail reserve + slack); a prefix + marker does not
        ctx = run(hook, monkeypatch, tws, budget=budget)
        assert len(ctx) <= budget
        assert sorted(one_liners(ctx)) == [lid(i) for i in range(1, 22)]
        assert head_lesson(ctx) is None
        hdr = header_of(ctx)
        assert "not shown" in hdr and "below" not in hdr


class TestCutValidity:
    def test_no_lone_surrogate_and_single_marker_line(self, hook, monkeypatch, tmp_path):
        nudge = "NUDGE-x " + ("\U0001F600" * 900)
        tws = [tw(i) for i in range(1, 21)] + [tw(21, nudge=nudge)]
        ctx = run(hook, monkeypatch, tws)
        ctx.encode("utf-8")   # raises on a lone surrogate
        assert len(MARKER_RE.findall(ctx)) == 1
        assert ctx.rstrip().endswith("]")
        assert len(ctx) <= FLOOR_BUDGET

    def test_hostile_lone_surrogate_in_nudge_does_not_raise(self, hook, monkeypatch, tmp_path):
        nudge = "NUDGE-x " + ("a" * 100 + "\ud83d") * 30
        tws = [tw(i) for i in range(1, 21)] + [tw(21, nudge=nudge)]
        ctx = run(hook, monkeypatch, tws)
        assert sorted(one_liners(ctx)) == [lid(i) for i in range(1, 22)]


class TestUnchanged:
    def test_three_hits_no_cluster_no_truncation(self, hook, monkeypatch, tmp_path):
        ctx = run(hook, monkeypatch, [tw(i, nudge=f"n-{lid(i)}") for i in (1, 2, 3)])
        assert "truncated" not in ctx and one_liners(ctx) == []

    def test_exception_in_cluster_path_falls_back_to_plain_bullets(self, hook, monkeypatch, tmp_path):
        def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(hook, "_cluster_order", boom)
        ctx = run(hook, monkeypatch, [tw(i, nudge=f"n-{lid(i)}") for i in range(1, 6)])
        assert ctx.count("  • n-lsx-") >= 1 and one_liners(ctx) == []
        assert "lessons matched" not in ctx

    def test_exception_in_truncation_planner_falls_back_too(self, hook, monkeypatch, tmp_path):
        def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(hook, "_cluster_head", boom)
        ctx = run(hook, monkeypatch, [tw(i, nudge=f"n-{lid(i)}") for i in range(1, 6)])
        assert ctx.count("  • n-lsx-") >= 1 and "lessons matched" not in ctx


# ----------------------------------------------------------------------------- round 1 (PR #3979)
# Cells that discriminate: each pins a behaviour a plausible-wrong implementation would break.

def shown_prefix(ctx):
    """The head-nudge text the block shows (after the bullet glyph, before the marker line)."""
    return ctx.split("  • ", 1)[1].split("\n  …[nudge truncated")[0]


def nothing_fits_budget(hook, monkeypatch, tws):
    """A budget at which the one-liners fit but no nudge prefix does (measured, as TestNothingFits)."""
    full = run(hook, monkeypatch, tws, budget=20000)
    lines_len = sum(len(ln) + 1 for ln in full.split("\n") if ln.startswith("  - "))
    return len(full[: full.index("  - lsx")]) + lines_len + 90


def plain_nudge():
    """One line, no whitespace runs: every character of the cut is a nudge character."""
    return "NUDGE-x " + "a" * 1700


def cut_tws(nudge):
    return [tw(i) for i in range(1, 21)] + [tw(21, nudge=nudge)]


class TestSurrogateAtTheCut:
    def test_lone_high_surrogate_at_the_cut_is_dropped_from_the_shown_prefix(self, hook, monkeypatch, tmp_path):
        base = plain_nudge()
        # A surrogate anywhere in a nudge makes the overflow-file write fail (utf-8), so the header and
        # marker then carry no path and the room differs from the plain run. Measure the cut length with
        # a surrogate at the START (same no-path layout, same length), then move it to the cut.
        n = len(shown_prefix(run(hook, monkeypatch, cut_tws("\ud83d" + base[1:]))))
        # put the lone high surrogate on the character that is LAST in the kept prefix
        nudge = base[: n - 1] + "\ud83d" + base[n:]
        ctx = run(hook, monkeypatch, cut_tws(nudge))
        assert not re.search("[\ud800-\udfff]", ctx), "a lone surrogate reached the shown prefix"
        ctx.encode("utf-8")   # raises on a lone surrogate
        assert shown_prefix(ctx) == base[: n - 1], "the guard drops exactly the one trailing surrogate"
        assert len(MARKER_RE.findall(ctx)) == 1


class TestMarkerAndPrefixShape:
    def test_marker_line_names_the_same_path_as_the_header_overflow_file(self, hook, monkeypatch, tmp_path):
        ctx = run(hook, monkeypatch, [tw(i) for i in range(1, 22)])
        hdr_path = re.search(r"all 21 full texts: (\S+):$", header_of(ctx)).group(1)
        marker_path = re.search(r"^  …\[nudge truncated; rest in (\S+)\]$", ctx, re.M).group(1)
        assert marker_path == hdr_path
        assert long_nudge(lid(21)) in Path(marker_path).read_text()

    def test_prefix_ending_before_a_newline_leaves_no_blank_line_before_the_marker(self, hook, monkeypatch, tmp_path):
        base = plain_nudge()
        n = len(shown_prefix(run(hook, monkeypatch, cut_tws(base))))
        nudge = base[: n - 1] + "\n" + base[n:]   # the last kept character is a newline
        ctx = run(hook, monkeypatch, cut_tws(nudge))
        assert "\n\n  …[nudge truncated" not in ctx
        assert shown_prefix(ctx) == base[: n - 1]
        assert len(MARKER_RE.findall(ctx)) == 1


class TestMinimumPrefixBoundary:
    def test_forty_characters_of_room_truncates_thirty_nine_says_not_shown(self, hook, monkeypatch, tmp_path):
        tws = cut_tws(plain_nudge())
        lo = nothing_fits_budget(hook, monkeypatch, tws)
        hi = FLOOR_BUDGET
        assert "not shown" in header_of(run(hook, monkeypatch, tws, budget=lo))
        assert "truncated" in header_of(run(hook, monkeypatch, tws, budget=hi))
        while hi - lo > 1:   # measured: the smallest budget at which the head nudge is shown truncated
            mid = (lo + hi) // 2
            if "truncated" in header_of(run(hook, monkeypatch, tws, budget=mid)):
                hi = mid
            else:
                lo = mid
        at_min = run(hook, monkeypatch, tws, budget=hi)
        below = run(hook, monkeypatch, tws, budget=hi - 1)
        assert "truncated" in header_of(at_min)
        assert len("  • " + shown_prefix(at_min)) == hook.CLUSTER_MIN_NUDGE_PREFIX == 40
        assert "not shown" in header_of(below) and head_lesson(below) is None
        assert not MARKER_RE.findall(below)


class TestPartialNudgeIsNotCitedAsAPrinciple:
    """A truncated or absent head nudge counts as NOT shown, so the principles section never cites
    its lesson (it would cite a reason the reader cannot see)."""

    def _capture(self, hook, monkeypatch):
        seen = {}

        def fake(shown, heads_out=None):
            seen["ids"] = [h["node_id"] for h in shown]
            return "[triggered principles]\n" + "\n".join(f"  ↑ {h['node_id']} head" for h in shown) if shown else ""

        monkeypatch.setattr(hook, "render_triggered_principles", fake)
        return seen

    def test_full_nudge_is_cited_control(self, hook, monkeypatch, tmp_path):
        seen = self._capture(hook, monkeypatch)
        ctx = run(hook, monkeypatch, [tw(i, nudge=f"NUDGE-{lid(i)} short") for i in range(1, 22)])
        assert seen["ids"] == [lid(21)] and "[triggered principles]" in ctx

    def test_truncated_nudge_is_not_cited(self, hook, monkeypatch, tmp_path):
        seen = self._capture(hook, monkeypatch)
        ctx = run(hook, monkeypatch, [tw(i) for i in range(1, 22)])
        assert "truncated" in header_of(ctx)
        assert seen["ids"] == []
        assert "[triggered principles]" not in ctx and f"↑ {lid(21)}" not in ctx

    def test_absent_nudge_is_not_cited(self, hook, monkeypatch, tmp_path):
        seen = self._capture(hook, monkeypatch)
        tws = [tw(i) for i in range(1, 22)]
        ctx = run(hook, monkeypatch, tws, budget=nothing_fits_budget(hook, monkeypatch, tws))
        assert "not shown" in header_of(ctx)
        assert seen["ids"] == []
        assert "[triggered principles]" not in ctx and f"↑ {lid(21)}" not in ctx


class TestHeadNoticeIsNotDoubledOrSliced:
    """The loader/owner notice rides above the cluster header exactly once, in every nudge state."""
    NOTICE = "NOTICE-LINE loader failed for a test"

    def _run(self, hook, monkeypatch, tws, budget=FLOOR_BUDGET):
        monkeypatch.setattr(hook, "_pending_loader_notice", lambda: (self.NOTICE, 1.0))
        return run(hook, monkeypatch, tws, budget=budget)

    def _assert_notice_once_above_header(self, ctx, budget):
        assert ctx.count(self.NOTICE) == 1
        assert f"{self.NOTICE}\n[lesson-tripwire] 21 lessons matched" in ctx
        assert len(ctx) <= budget

    def test_full_state(self, hook, monkeypatch, tmp_path):
        tws = [tw(i, nudge=f"NUDGE-{lid(i)} short") for i in range(1, 22)]
        ctx = self._run(hook, monkeypatch, tws)
        self._assert_notice_once_above_header(ctx, FLOOR_BUDGET)
        assert "full text of the first below" in header_of(ctx)

    def test_truncated_state_counts_the_notice_in_the_fit(self, hook, monkeypatch, tmp_path):
        ctx = self._run(hook, monkeypatch, [tw(i) for i in range(1, 22)])
        self._assert_notice_once_above_header(ctx, FLOOR_BUDGET)
        assert "first nudge truncated below" in header_of(ctx)
        assert FLOOR_BUDGET - len(ctx) < 400

    def test_absent_state(self, hook, monkeypatch, tmp_path):
        tws = [tw(i) for i in range(1, 22)]
        budget = nothing_fits_budget(hook, monkeypatch, tws) + len(self.NOTICE) + 1
        ctx = self._run(hook, monkeypatch, tws, budget=budget)
        self._assert_notice_once_above_header(ctx, budget)
        assert "first nudge not shown" in header_of(ctx)
