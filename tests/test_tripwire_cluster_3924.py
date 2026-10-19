"""Contract tests for alpha #3924 (part of #3911): one cluster block for co-firing lessons.

When more than K=3 lessons match one call, the tripwire hook renders ONE cluster block instead of
K+ bullets: a header naming the trigger and count, one `  - <id>: <summary>` line per matched
lesson (none cut), then ONE full nudge for the first lesson in the ranking. Every full nudge still
goes to the overflow file. At K or fewer hits the output is byte-identical to the pre-change output.

Ranking inside one tripwire_priority (Borges, option C): ascending fires-per-1,000 from
$ENGRAM_HOME/lesson-fire-ranks.json, then longest pattern, then lesson id. A lesson absent from the
file sorts AFTER every rated peer, by length then id. A missing / unreadable / malformed / stale
(> 7 days) file means length order for all. The block's header says which order was used.

Written BEFORE the implementation (contract-first). Fixture ids are `lsx-NNN` on purpose: they do not
match the real-node-id shape the check-diff gate scans for.

Cells:
  C1  19 hits -> one block, each id once with its summary, one full nudge, overflow holds all 19
  C2  full nudge goes to top priority; tie -> lower rate; then longer pattern; then id
  C3  rarity order honoured; missing/malformed -> length order, no exception; unrated after rated;
      shuffled input -> identical output
  C4  order note: `order: rarity (ranks 2d old, 1 unrated)` / `order: length fallback (<reason>)`
  C5  staleness: 8 days -> `stale` + length order; 6 days -> rarity; bad generated_at -> malformed;
      future skew up to 1 day -> age 0, beyond -> malformed
  C6  budget: never exceeded; one-liners cut whole from the tail with a count trailer
  C7  summary fallback: claim cut to 140; nothing -> `(no summary)`
  C8  K or fewer: byte-identical golden (captured on unmodified origin/dev); 3 -> no cluster, 4 -> cluster;
      the rank file is not read below the threshold
  C9  discriminating control: 19 lessons visible inline after, 1 on the pre-change renderer
  C10 principles section cites only the lesson whose nudge is visible
  C11 loader: recall_summary + claim are the 6th/7th row elements, first five unchanged
"""

from __future__ import annotations

import importlib.util
import io
import json
import random
import re
import sqlite3
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HOOK_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-lesson-tripwire-hook.py"

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
CMD = "gh pr create --title x"
LINE_RE = re.compile(r"^  - (lsx-\d{3}): (.*)$", re.M)


@pytest.fixture()
def hook(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("lesson_tripwire_hook_3924", str(_HOOK_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ENGRAM_HOME", str(tmp_path))
    monkeypatch.setattr(mod, "LOADER_NOTICE_STAMP_PATH", tmp_path / "stamp.json", raising=False)
    monkeypatch.setattr(mod, "LESSON_RANKS_PATH", tmp_path / "lesson-fire-ranks.json", raising=False)
    monkeypatch.setattr(mod, "_ranks_now", lambda: NOW.timestamp(), raising=False)
    return mod


def lid(i):
    return f"lsx-{i:03d}"


def tw(i, prio="normal", summary="auto", claim="claim text", nudge=None, pattern=None):
    """A load_tripwires-shaped 7-tuple. Pattern length grows with i (longer = larger i)."""
    pattern = pattern if pattern is not None else "gh pr create" + "(?:)" * i
    nudge = nudge if nudge is not None else f"NUDGE-{lid(i)} " + "x" * 300
    summary = f"SUM-{lid(i)} short summary" if summary == "auto" else summary
    return (pattern, nudge, lid(i), prio, "full", summary, claim)


def write_ranks(tmp_path, ranks, generated_at=None, **extra):
    doc = {"generated_at": generated_at or NOW.isoformat(), "window": "6 transcripts",
           "targets": 1000, "ranks": ranks}
    doc.update(extra)
    p = tmp_path / "lesson-fire-ranks.json"
    p.write_text(json.dumps(doc))
    return p


def run(hook, monkeypatch, tmp_path, tripwires, cmd=CMD, budget=None, hook_input=None):
    monkeypatch.setattr(hook, "load_tripwires", lambda: tripwires)
    monkeypatch.setattr(hook, "load_prevention_lessons", lambda: [])
    monkeypatch.setattr(hook, "check_in_turn_recall", lambda *a, **k: "")
    monkeypatch.setattr(hook, "_ws4_config", lambda: {"active": False, "seed": ""})
    monkeypatch.setattr(hook, "_append_prevention_ledger", lambda *a, **k: None)
    if budget is not None:
        monkeypatch.setattr(hook, "get_harness_budgets", lambda h: {"budget": budget, "head_budget": 1800})
    hi = hook_input or {"tool_name": "Bash", "tool_input": {"command": cmd}}
    out = io.StringIO()
    with unittest.mock.patch("sys.stdin", io.StringIO(json.dumps(hi))), unittest.mock.patch("sys.stdout", out):
        try:
            hook.main()
        except SystemExit:
            pass
    raw = out.getvalue()
    return raw, (json.loads(raw)["hookSpecificOutput"]["additionalContext"] if raw else "")


def ids_in_order(ctx):
    return [m.group(1) for m in LINE_RE.finditer(ctx)]


def norm_path(ctx):
    return re.sub(r"\S*tripwire-overflow\S*", "<P>", ctx)


def nudge_ids(ctx):
    return re.findall(r"NUDGE-(lsx-\d{3})", ctx)


# ----------------------------------------------------------------------------- C1
class TestOneBlock:
    def test_19_hits_one_block_each_id_once_one_full_nudge_overflow_holds_all(self, hook, monkeypatch, tmp_path):
        tws = [tw(i) for i in range(1, 20)]
        _, ctx = run(hook, monkeypatch, tmp_path, tws)
        assert sorted(ids_in_order(ctx)) == [lid(i) for i in range(1, 20)]
        for i in range(1, 20):
            assert ctx.count(f"  - {lid(i)}:") == 1, lid(i)
            assert f"  - {lid(i)}: SUM-{lid(i)} short summary" in ctx
        assert len(nudge_ids(ctx)) == 1
        assert ctx.count("  • ") == 1
        m = re.search(r"(\S*tripwire-overflow\S*\.txt)", ctx)
        assert m, ctx
        body = Path(m.group(1)).read_text()
        for i in range(1, 20):
            assert f"[{lid(i)}] NUDGE-{lid(i)} " in body
        assert body.count("NUDGE-lsx-") == 19

    def test_header_names_trigger_and_count_bash_verb(self, hook, monkeypatch, tmp_path):
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 20)])
        header = [ln for ln in ctx.split("\n") if ln.startswith("[lesson-tripwire]")][0]
        assert "19" in header and "gh" in header

    def test_header_names_trigger_mcp_tool(self, hook, monkeypatch, tmp_path):
        tws = [tw(i, pattern="mcp__x__send_crossing" + "(?:)" * i) for i in range(1, 6)]
        hi = {"tool_name": "mcp__x__send_crossing", "tool_input": {"body": "hi"}}
        _, ctx = run(hook, monkeypatch, tmp_path, tws, hook_input=hi)
        header = [ln for ln in ctx.split("\n") if ln.startswith("[lesson-tripwire]")][0]
        assert "mcp__x__send_crossing" in header and "5" in header

    def test_each_summary_is_one_line(self, hook, monkeypatch, tmp_path):
        tws = [tw(i, summary="two\nline   summary") for i in range(1, 6)]
        _, ctx = run(hook, monkeypatch, tmp_path, tws)
        assert "  - lsx-001: two line summary" in ctx


# ----------------------------------------------------------------------------- C2
class TestFullNudgeChoice:
    def test_top_priority_gets_the_full_nudge(self, hook, monkeypatch, tmp_path):
        tws = [tw(i) for i in range(1, 7)] + [tw(0, prio="high")]
        write_ranks(tmp_path, {lid(i): float(i) for i in range(0, 7)})
        _, ctx = run(hook, monkeypatch, tmp_path, tws)
        assert nudge_ids(ctx) == [lid(0)]
        assert ids_in_order(ctx)[0] == lid(0)

    def test_priority_tie_lower_rate_wins(self, hook, monkeypatch, tmp_path):
        tws = [tw(1), tw(2), tw(3), tw(4), tw(5)]
        write_ranks(tmp_path, {lid(1): 9.0, lid(2): 8.0, lid(3): 0.5, lid(4): 7.0, lid(5): 6.0})
        _, ctx = run(hook, monkeypatch, tmp_path, tws)
        assert nudge_ids(ctx) == [lid(3)]

    def test_rate_tie_longer_pattern_wins_then_id(self, hook, monkeypatch, tmp_path):
        tws = [tw(1), tw(2), tw(3), tw(4), tw(5)]
        write_ranks(tmp_path, {lid(i): 1.0 for i in range(1, 6)})
        _, ctx = run(hook, monkeypatch, tmp_path, tws)
        assert ids_in_order(ctx) == [lid(5), lid(4), lid(3), lid(2), lid(1)]
        same_len = [tw(i, pattern="gh pr create") for i in (4, 2, 3, 1)]
        write_ranks(tmp_path, {lid(i): 1.0 for i in range(1, 5)})
        _, ctx = run(hook, monkeypatch, tmp_path, same_len)
        assert ids_in_order(ctx) == [lid(1), lid(2), lid(3), lid(4)]

    def test_high_before_normal_even_against_a_rarer_normal(self, hook, monkeypatch, tmp_path):
        tws = [tw(1), tw(2), tw(3), tw(4, prio="high")]
        write_ranks(tmp_path, {lid(1): 0.0, lid(2): 0.0, lid(3): 0.0, lid(4): 99.0})
        _, ctx = run(hook, monkeypatch, tmp_path, tws)
        assert ids_in_order(ctx)[0] == lid(4)


# ----------------------------------------------------------------------------- C3
class TestRanking:
    def test_rarity_order_is_honoured(self, hook, monkeypatch, tmp_path):
        tws = [tw(i) for i in range(1, 7)]
        rates = {lid(1): 6.0, lid(2): 1.0, lid(3): 5.0, lid(4): 2.0, lid(5): 4.0, lid(6): 3.0}
        write_ranks(tmp_path, rates)
        _, ctx = run(hook, monkeypatch, tmp_path, tws)
        assert ids_in_order(ctx) == sorted(rates, key=rates.get)

    def test_missing_file_is_length_order(self, hook, monkeypatch, tmp_path):
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 7)])
        assert ids_in_order(ctx) == [lid(i) for i in range(6, 0, -1)]

    @pytest.mark.parametrize("text", ["{not json", "[]", '"x"', '{"ranks": 3}', "", '{"generated_at": "x"}'])
    def test_malformed_file_is_length_order_and_never_raises(self, hook, monkeypatch, tmp_path, text):
        (tmp_path / "lesson-fire-ranks.json").write_text(text)
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 7)])
        assert ids_in_order(ctx) == [lid(i) for i in range(6, 0, -1)]
        assert "length fallback" in ctx

    def test_unreadable_file_is_length_order(self, hook, monkeypatch, tmp_path):
        (tmp_path / "lesson-fire-ranks.json").mkdir()  # a directory: open fails with OSError
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 7)])
        assert ids_in_order(ctx) == [lid(i) for i in range(6, 0, -1)]
        assert "length fallback (unreadable)" in ctx

    def test_a_non_numeric_rate_is_treated_as_unrated(self, hook, monkeypatch, tmp_path):
        write_ranks(tmp_path, {lid(1): "high", lid(2): 1.0, lid(3): True, lid(4): 2.0})
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 5)])
        assert ids_in_order(ctx)[:2] == [lid(2), lid(4)]
        assert "2 unrated" in ctx

    def test_unrated_lesson_sorts_after_rated_peers_by_length_then_id(self, hook, monkeypatch, tmp_path):
        tws = [tw(1), tw(2), tw(3), tw(4), tw(5), tw(6, pattern="gh pr create" + "(?:)" * 5)]
        # lsx-005 and lsx-006 have equal pattern length; both unrated; 2 and 4 rated
        write_ranks(tmp_path, {lid(2): 50.0, lid(4): 10.0, lid(1): 70.0})
        _, ctx = run(hook, monkeypatch, tmp_path, tws)
        # rated first (ascending rate): 4, 2, 1 ; then unrated by length desc then id: 5, 6 (equal len), 3
        assert ids_in_order(ctx) == [lid(4), lid(2), lid(1), lid(5), lid(6), lid(3)]

    def test_unrated_normal_still_loses_to_rated_high_priority_first(self, hook, monkeypatch, tmp_path):
        write_ranks(tmp_path, {lid(1): 0.1})
        tws = [tw(1), tw(2), tw(3), tw(4, prio="high")]
        _, ctx = run(hook, monkeypatch, tmp_path, tws)
        assert ids_in_order(ctx)[0] == lid(4)

    def test_shuffled_input_gives_identical_output(self, hook, monkeypatch, tmp_path):
        base = [tw(i) for i in range(1, 12)] + [tw(12, prio="high")]
        write_ranks(tmp_path, {lid(i): float(i % 3) for i in range(1, 9)})
        _, want = run(hook, monkeypatch, tmp_path, base)
        rng = random.Random(7)
        for _ in range(6):
            sh = list(base)
            rng.shuffle(sh)
            _, got = run(hook, monkeypatch, tmp_path, sh)
            assert norm_path(got) == norm_path(want)

    def test_shuffled_input_identical_with_no_rank_file_and_equal_lengths(self, hook, monkeypatch, tmp_path):
        base = [tw(i, pattern="gh pr create") for i in range(1, 8)]
        _, want = run(hook, monkeypatch, tmp_path, base)
        sh = list(reversed(base))
        _, got = run(hook, monkeypatch, tmp_path, sh)
        assert norm_path(got) == norm_path(want)
        assert ids_in_order(want) == [lid(i) for i in range(1, 8)]


# ----------------------------------------------------------------------------- C4 / C5
def iso_ago(days, z=False):
    t = NOW - timedelta(days=days)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ") if z else t.isoformat()


class TestOrderNote:
    def test_rarity_note_age_and_unrated_count(self, hook, monkeypatch, tmp_path):
        write_ranks(tmp_path, {lid(i): float(i) for i in range(1, 6)}, generated_at=iso_ago(2))
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 7)])
        assert "order: rarity (ranks 2d old, 1 unrated)" in ctx

    def test_all_rated_says_zero_unrated(self, hook, monkeypatch, tmp_path):
        write_ranks(tmp_path, {lid(i): float(i) for i in range(1, 6)}, generated_at=iso_ago(0))
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 6)])
        assert "order: rarity (ranks 0d old, 0 unrated)" in ctx

    def test_trailing_z_timestamp_parses(self, hook, monkeypatch, tmp_path):
        write_ranks(tmp_path, {lid(i): 1.0 for i in range(1, 6)}, generated_at=iso_ago(3, z=True))
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 6)])
        assert "order: rarity (ranks 3d old, 0 unrated)" in ctx

    def test_offset_timestamp_parses(self, hook, monkeypatch, tmp_path):
        t = (NOW - timedelta(days=4)).astimezone(timezone(timedelta(hours=5))).isoformat()
        write_ranks(tmp_path, {lid(i): 1.0 for i in range(1, 6)}, generated_at=t)
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 6)])
        assert "ranks 4d old" in ctx

    def test_no_file_note(self, hook, monkeypatch, tmp_path):
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 6)])
        assert "order: length fallback (no file)" in ctx

    def test_malformed_note(self, hook, monkeypatch, tmp_path):
        (tmp_path / "lesson-fire-ranks.json").write_text("{nope")
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 6)])
        assert "order: length fallback (malformed)" in ctx


class TestStaleness:
    def test_8_day_old_file_is_length_order_and_says_stale(self, hook, monkeypatch, tmp_path):
        write_ranks(tmp_path, {lid(1): 0.0, lid(2): 1.0, lid(3): 2.0, lid(4): 3.0, lid(5): 4.0},
                    generated_at=iso_ago(8))
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 6)])
        assert "order: length fallback (stale)" in ctx
        assert ids_in_order(ctx) == [lid(i) for i in range(5, 0, -1)]

    def test_6_day_old_file_is_still_rarity(self, hook, monkeypatch, tmp_path):
        write_ranks(tmp_path, {lid(1): 0.0, lid(2): 1.0, lid(3): 2.0, lid(4): 3.0, lid(5): 4.0},
                    generated_at=iso_ago(6))
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 6)])
        assert "order: rarity (ranks 6d old, 0 unrated)" in ctx
        assert ids_in_order(ctx) == [lid(i) for i in range(1, 6)]

    def test_exactly_7_days_is_not_stale_just_past_is(self, hook, monkeypatch, tmp_path):
        r = {lid(i): float(i) for i in range(1, 6)}
        write_ranks(tmp_path, r, generated_at=iso_ago(7))
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 6)])
        assert "order: rarity" in ctx
        write_ranks(tmp_path, r, generated_at=(NOW - timedelta(days=7, seconds=1)).isoformat())
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 6)])
        assert "(stale)" in ctx

    @pytest.mark.parametrize("bad", ["yesterday", "", None, 12345, "2026-13-45T00:00:00Z"])
    def test_unparseable_generated_at_is_malformed_length_fallback(self, hook, monkeypatch, tmp_path, bad):
        p = write_ranks(tmp_path, {lid(i): float(i) for i in range(1, 6)})
        doc = json.loads(p.read_text())
        doc["generated_at"] = bad
        p.write_text(json.dumps(doc))
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 6)])
        assert "order: length fallback (malformed)" in ctx
        assert ids_in_order(ctx) == [lid(i) for i in range(5, 0, -1)]

    def test_missing_generated_at_is_malformed(self, hook, monkeypatch, tmp_path):
        (tmp_path / "lesson-fire-ranks.json").write_text(json.dumps({"ranks": {lid(1): 1.0}}))
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 6)])
        assert "(malformed)" in ctx

    def test_future_within_a_day_counts_as_age_zero(self, hook, monkeypatch, tmp_path):
        write_ranks(tmp_path, {lid(i): float(i) for i in range(1, 6)},
                    generated_at=(NOW + timedelta(hours=23)).isoformat())
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 6)])
        assert "order: rarity (ranks 0d old, 0 unrated)" in ctx

    def test_future_beyond_a_day_is_malformed(self, hook, monkeypatch, tmp_path):
        write_ranks(tmp_path, {lid(i): float(i) for i in range(1, 6)},
                    generated_at=(NOW + timedelta(days=1, hours=1)).isoformat())
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 6)])
        assert "order: length fallback (malformed)" in ctx

    def test_stale_file_ignores_ranks_even_for_the_nudge_choice(self, hook, monkeypatch, tmp_path):
        write_ranks(tmp_path, {lid(1): 0.0, lid(5): 99.0, lid(2): 5.0, lid(3): 5.0, lid(4): 5.0},
                    generated_at=iso_ago(30))
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 6)])
        assert nudge_ids(ctx) == [lid(5)]


# ----------------------------------------------------------------------------- C6
class TestBudget:
    def test_block_never_exceeds_budget_and_lines_cut_whole_from_tail(self, hook, monkeypatch, tmp_path):
        tws = [tw(i) for i in range(1, 20)]
        full_lines = {f"  - {lid(i)}: SUM-{lid(i)} short summary" for i in range(1, 20)}
        _, big = run(hook, monkeypatch, tmp_path, tws)
        order = ids_in_order(big)
        # Budgets derived from the MEASURED block, never fixed numbers: the header carries the
        # overflow path, whose length is pytest's tmp_path, so a fixed 1200 cut on a Mac (long
        # /private/var/... path) and cut nothing on a Linux runner (short /tmp path) -- the merge
        # seat saw this cell red deterministically with --basetemp=/tmp/bt. Each budget here is
        # below header + ALL one-liners, so at least one one-liner is cut on every host.
        one_liners = [ln for ln in big.split("\n") if ln.startswith("  - ")]
        header_len = len(big[: big.index(one_liners[0])])
        lines_len = sum(len(ln) + 1 for ln in one_liners)
        budgets = [header_len + int(lines_len * f) for f in (0.9, 0.7, 0.5, 0.3)]
        assert len(set(budgets)) == 4 and all(b < header_len + lines_len for b in budgets)
        for budget in budgets:
            _, ctx = run(hook, monkeypatch, tmp_path, tws, budget=budget)
            assert len(ctx) <= budget, (budget, len(ctx))
            got = ids_in_order(ctx)
            assert got == order[: len(got)], "cut must come from the tail"
            assert len(got) < 19
            for ln in ctx.split("\n"):
                if ln.startswith("  - "):
                    assert ln in full_lines, f"mid-line cut: {ln!r}"
            m = re.search(r"▸ (\d+) shown · (\d+) cut", ctx)
            assert m and int(m.group(2)) > 0

    def test_ample_budget_cuts_nothing(self, hook, monkeypatch, tmp_path):
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 20)])
        assert re.search(r"▸ \d+ shown · 0 cut", ctx)
        assert len(ids_in_order(ctx)) == 19

    def test_overflow_still_holds_all_nudges_when_lines_are_cut(self, hook, monkeypatch, tmp_path):
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 20)], budget=700)
        m = re.search(r"(\S*tripwire-overflow\S*\.txt)", ctx)
        assert m
        assert Path(m.group(1)).read_text().count("NUDGE-lsx-") == 19


# ----------------------------------------------------------------------------- C7
class TestSummaryCap:
    def test_oversized_summary_is_capped_and_does_not_cut_the_other_lessons(self, hook, monkeypatch, tmp_path):
        """Review round 1 (PR 3948): a 6,000-char recall_summary on the first-ranked lesson cut the
        block mid-line and dropped the rest at a tight budget."""
        tws = [tw(i) for i in range(1, 20)]
        tws[18] = tw(19, summary="S" * 6000)          # the longest pattern: ranked first without a rank file
        _, ctx = run(hook, monkeypatch, tmp_path, tws, budget=3000)
        assert len(ids_in_order(ctx)) == 19, "every lesson stays inline"
        for ln in ctx.split("\n"):
            if ln.startswith("  - "):
                assert len(ln) <= len("  - ") + len(lid(1)) + 2 + hook.SUMMARY_LINE_CAP, len(ln)

    def test_cap_constant(self, hook):
        assert hook.SUMMARY_LINE_CAP == 200 and hook.SUMMARY_LINE_CAP > hook.SUMMARY_CLAIM_CUT

    def test_control_a_normal_summary_is_not_cut(self, hook, monkeypatch, tmp_path):
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i, summary="s" * 150) for i in range(1, 6)])
        assert ("s" * 150) in ctx


class TestSummaryFallback:
    def test_no_summary_uses_claim_cut_to_140(self, hook, monkeypatch, tmp_path):
        claim = "c" * 300
        tws = [tw(1, summary=None, claim=claim)] + [tw(i) for i in range(2, 6)]
        _, ctx = run(hook, monkeypatch, tmp_path, tws)
        assert f"  - lsx-001: {'c' * 140}\n" in ctx + "\n"
        assert "c" * 141 not in ctx

    def test_empty_and_whitespace_summary_fall_back_to_claim(self, hook, monkeypatch, tmp_path):
        tws = [tw(1, summary="", claim="from claim"), tw(2, summary="   ", claim="from claim two")] + [
            tw(i) for i in range(3, 6)]
        _, ctx = run(hook, monkeypatch, tmp_path, tws)
        assert "  - lsx-001: from claim" in ctx
        assert "  - lsx-002: from claim two" in ctx

    def test_no_summary_and_no_claim_is_a_placeholder_never_empty_never_dropped(self, hook, monkeypatch, tmp_path):
        tws = [tw(1, summary=None, claim=None), tw(2, summary="", claim="")] + [tw(i) for i in range(3, 6)]
        _, ctx = run(hook, monkeypatch, tmp_path, tws)
        assert "  - lsx-001: (no summary)" in ctx
        assert "  - lsx-002: (no summary)" in ctx
        for m in LINE_RE.finditer(ctx):
            assert m.group(2).strip()

    def test_bare_five_tuples_still_render_every_lesson(self, hook, monkeypatch, tmp_path):
        tws = [t[:5] for t in (tw(i) for i in range(1, 7))]
        _, ctx = run(hook, monkeypatch, tmp_path, tws)
        assert sorted(ids_in_order(ctx)) == [lid(i) for i in range(1, 7)]
        assert "(no summary)" in ctx


# ----------------------------------------------------------------------------- C8
# Golden captured from the UNMODIFIED hook (origin/dev 938e704c) for three matching 5-tuples whose
# nudges are "n-lsx-00N" (K = 3: no cluster).
GOLDEN_K3 = '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "\\u25b8 3 shown \\u00b7 0 cut\\n[lesson-tripwire] Action-moment pattern matched \\u2014 remember:\\n  \\u2022 n-lsx-003\\n  \\u2022 n-lsx-002\\n  \\u2022 n-lsx-001"}}\n'


class TestThreshold:
    def test_three_hits_is_byte_identical_to_pre_change(self, hook, monkeypatch, tmp_path):
        tws = [tw(i, nudge=f"n-{lid(i)}") for i in (1, 2, 3)]
        raw, _ = run(hook, monkeypatch, tmp_path, tws)
        assert raw == GOLDEN_K3

    def test_three_hits_bare_5_tuples_same_golden(self, hook, monkeypatch, tmp_path):
        tws = [tw(i, nudge=f"n-{lid(i)}")[:5] for i in (1, 2, 3)]
        raw, _ = run(hook, monkeypatch, tmp_path, tws)
        assert raw == GOLDEN_K3

    def test_threshold_constant(self, hook):
        assert hook.CLUSTER_THRESHOLD == 3

    def test_four_hits_is_a_cluster_block(self, hook, monkeypatch, tmp_path):
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i, nudge=f"n-{lid(i)}") for i in (1, 2, 3, 4)])
        assert len(ids_in_order(ctx)) == 4

    def test_three_hits_has_no_one_liners_and_no_order_note(self, hook, monkeypatch, tmp_path):
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i, nudge=f"n-{lid(i)}") for i in (1, 2, 3)])
        assert ids_in_order(ctx) == [] and "order:" not in ctx

    def test_rank_file_is_not_read_below_the_threshold(self, hook, monkeypatch, tmp_path):
        write_ranks(tmp_path, {lid(1): 1.0})
        seen = []
        real = hook.read_regular_text

        def spy(path, *a, **k):
            seen.append(str(path))
            return real(path, *a, **k)

        monkeypatch.setattr(hook, "read_regular_text", spy)
        run(hook, monkeypatch, tmp_path, [tw(i, nudge=f"n-{lid(i)}") for i in (1, 2, 3)])
        assert not any("lesson-fire-ranks" in p for p in seen)
        run(hook, monkeypatch, tmp_path, [tw(i, nudge=f"n-{lid(i)}") for i in (1, 2, 3, 4)])
        assert sum("lesson-fire-ranks" in p for p in seen) == 1, "once per fire, on the cluster path only"

    def test_below_threshold_order_is_todays_length_order_even_with_a_rank_file(self, hook, monkeypatch, tmp_path):
        write_ranks(tmp_path, {lid(1): 0.0, lid(2): 5.0, lid(3): 9.0})
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i, nudge=f"n-{lid(i)}") for i in (1, 2, 3)])
        assert ctx.index("n-lsx-003") < ctx.index("n-lsx-002") < ctx.index("n-lsx-001")


# ----------------------------------------------------------------------------- C9
class TestDiscriminatingControl:
    @staticmethod
    def _tws():
        # a publish-verb call that fires 19 lessons, each with a large (~1.7k) nudge
        return [tw(i, nudge=f"NUDGE-{lid(i)} " + "y" * 1700) for i in range(1, 20)]

    def test_19_lessons_visible_inline_after_vs_1_on_the_pre_change_renderer(self, hook, monkeypatch, tmp_path):
        tws = self._tws()
        # AFTER: the hook's real output.
        _, ctx = run(hook, monkeypatch, tmp_path, tws)
        after = sum(1 for i in range(1, 20) if f"SUM-{lid(i)}" in ctx)
        assert after == 19
        # BEFORE: a copy of the pre-change selection logic (priority, -len(pattern), then emit_context
        # over the per-hit bullets at the 1800-char ceiling), run on the same hits.
        hits = hook.check_command(CMD, tws)
        hits = sorted(hits, key=lambda h: (0 if h.get("priority") == "high" else 1, -len(h.get("pattern") or "")))
        old = hook.emit_context(
            hook.TRIPWIRE_HEADER, [hook._tripwire_bullet(h) for h in hits],
            budget=9500, target=hook.TRIPWIRE_BLOCK_CHAR_CEILING, allow_single_overflow=True,
            overflow_writer=lambda items: "/x/overflow.txt")
        before = sum(1 for i in range(1, 20) if f"NUDGE-{lid(i)}" in old)
        assert before == 1


# ----------------------------------------------------------------------------- C10
class TestShownHits:
    def _capture(self, hook, monkeypatch):
        seen = {}

        def fake(shown, heads_out=None):
            seen["ids"] = [h["node_id"] for h in shown]
            return ""

        monkeypatch.setattr(hook, "render_triggered_principles", fake)
        return seen

    def test_cluster_mode_only_the_full_nudge_hit_counts_as_shown(self, hook, monkeypatch, tmp_path):
        seen = self._capture(hook, monkeypatch)
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 8)])
        assert seen["ids"] == nudge_ids(ctx) == [lid(7)]

    def test_below_threshold_all_shown_hits_still_count(self, hook, monkeypatch, tmp_path):
        seen = self._capture(hook, monkeypatch)
        run(hook, monkeypatch, tmp_path, [tw(i, nudge=f"n-{lid(i)}") for i in (1, 2, 3)])
        assert sorted(seen["ids"]) == [lid(1), lid(2), lid(3)]

    def test_cluster_mode_nudge_cut_by_budget_means_nobody_is_shown(self, hook, monkeypatch, tmp_path):
        seen = self._capture(hook, monkeypatch)
        _, ctx = run(hook, monkeypatch, tmp_path, [tw(i) for i in range(1, 20)], budget=700)
        assert nudge_ids(ctx) == []
        assert seen.get("ids") == []


# ----------------------------------------------------------------------------- C11
class TestLoaderColumns:
    def _db(self, tmp_path, rows):
        p = tmp_path / "knowledge.db"
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE nodes (id TEXT, type TEXT, claim TEXT, metadata TEXT, is_current INT, "
                  "memory_status TEXT, recall_summary TEXT)")
        for nid, meta, rs, claim in rows:
            c.execute("INSERT INTO nodes VALUES (?, 'lesson', ?, ?, 1, 'active', ?)",
                      (nid, claim, json.dumps(meta), rs))
        c.commit()
        c.close()
        return p

    def test_recall_summary_and_claim_are_the_6th_and_7th_elements(self, hook, tmp_path):
        p = self._db(tmp_path, [
            ("lsx-001", {"situation_pattern": "aaa", "scaffolding_nudge": "nud", "tripwire_priority": "high",
                         "match_scope": "command"}, "the summary", "the claim"),
            ("lsx-002", {"situation_pattern": "bbb", "scaffolding_nudge": "nud2"}, None, "claim two"),
        ])
        got = {r[2]: r for r in hook.load_tripwires(p)}
        assert got["lsx-001"] == ("aaa", "nud", "lsx-001", "high", "command", "the summary", "the claim")
        assert got["lsx-002"][:5] == ("bbb", "nud2", "lsx-002", "normal", "full")
        assert got["lsx-002"][5:] == (None, "claim two")

    def test_loader_rows_feed_the_cluster_renderer_end_to_end(self, hook, monkeypatch, tmp_path):
        rows = [(lid(i), {"situation_pattern": "gh pr create" + "(?:)" * i, "scaffolding_nudge": f"NUDGE-{lid(i)}"},
                 f"SUM-{lid(i)}", "c") for i in range(1, 7)]
        p = self._db(tmp_path, rows)
        orig = hook.load_tripwires
        monkeypatch.setattr(hook, "load_tripwires", lambda: orig(p))
        monkeypatch.setattr(hook, "load_prevention_lessons", lambda: [])
        monkeypatch.setattr(hook, "check_in_turn_recall", lambda *a, **k: "")
        monkeypatch.setattr(hook, "_ws4_config", lambda: {"active": False, "seed": ""})
        monkeypatch.setattr(hook, "_append_prevention_ledger", lambda *a, **k: None)
        out = io.StringIO()
        with unittest.mock.patch("sys.stdin", io.StringIO(json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": CMD}}))), unittest.mock.patch("sys.stdout", out):
            try:
                hook.main()
            except SystemExit:
                pass
        ctx = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        assert all(f"SUM-{lid(i)}" in ctx for i in range(1, 7))
