"""Tests for tools/dream_lottery.py — weight-aging lottery draw for dream fairies.

Covers:

  weighted_draw:
    1. Deterministic under a seeded RNG (same seed -> same pick).
    2. Distribution is proportional to weight over many draws (statistical).
    3. All-zero weights falls back to a uniform choice instead of raising.

  run_draw (algorithm correctness):
    4. Drawn type's weight resets to 0; every other type's weight += 1.
    5. Exactly one type drawn by default (count=1).
    6. count=2 draws two distinct types, never the same type twice in one night.
    7. First-ever draw (fresh default_state) has all types at weight 1.
    8. last_run_turn / last_run_at are stamped only on types that ran this cycle.
    9. Un-drawn types' last_run_turn / last_run_at are left untouched.

  cascade interrupt:
    10. Below threshold -> interrupt does not fire, draw proceeds normally.
    11. At/above threshold -> interrupt fires, forces the configured types in
        regardless of the random draw, and their weight still resets to 0.
    12. Interrupt-forced types are excluded from the random-draw pool (an
        interrupted type is never *also* separately drawn).
    13. count draws still happen even when the interrupt fires (frequency and
        urgency are independent, per the design).

  window_params (per-type watermark for window-scoped categories):
    14. A window-scoped category (7) that ran gets its PRE-update last_run_at
        as its window_params value (so "since" reflects the last time IT
        actually ran, not this run).
    15. A never-run window-scoped category gets None (no prior watermark ->
        caller falls back to full-history scan).
    16. A non-window-scoped category that ran does NOT appear in window_params.

  state persistence:
    17. save_state then load_state round-trips exactly.
    18. load_state on a missing file returns default_state().
    19. load_state on a state file missing a newer category key back-fills it
        (forward-compat with an added category).

  cascade counting:
    20. count_cascade_markers counts only the four cascade actions, not
        unrelated edit_history rows, and respects the since_iso cutoff.

  draw-history (append-only, issue #1909):
    21. A real `draw` CLI invocation appends exactly one JSONL line; three
        draws append three lines.
    22. `--dry-run` writes neither state nor history.
    23. The default history path derives correctly from a custom `--state`.
    24. A history-write failure (unwritable path) does not break the draw:
        the CLI still exits 0 and state is still saved.
    25. The `history` subcommand tally is correct and distinguishes
        drawn-by-lottery from interrupt-forced counts.
    26. A malformed/truncated line is skipped and counted in
        `skipped_malformed`, not fatal.
    27. A missing history file tallies as a valid, all-zero empty result.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools.dream_lottery import (  # noqa: E402
    CATEGORIES,
    WINDOW_SCOPED_CATEGORIES,
    count_cascade_markers,
    default_state,
    history_path_for_state,
    load_state,
    main,
    run_draw,
    save_state,
    tally_history,
    weighted_draw,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _warm_state(checked_at: str = "2026-08-03T00:00:00Z") -> dict:
    """default_state() with last_interrupt_check_at seeded -- simulates a
    state file that has already been through at least one draw, so the
    interrupt's since-filter has a real baseline (as opposed to the
    cold-start None case, which counts 0 by design -- see
    test_cold_start_does_not_replay_all_history_as_interrupt)."""
    state = default_state()
    state["last_interrupt_check_at"] = checked_at
    return state


def _make_edit_history_db(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    """rows: list of (action, timestamp_iso)."""
    db_path = tmp_path / "knowledge.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE edit_history (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            turn INTEGER,
            action TEXT,
            node_id TEXT,
            node_type TEXT,
            details TEXT
        )
        """
    )
    for i, (action, ts) in enumerate(rows):
        conn.execute(
            "INSERT INTO edit_history (timestamp, turn, action, node_id, node_type, details) "
            "VALUES (?, 1, ?, ?, 'observation', '{}')",
            (ts, action, f"ob_{i:04d}"),
        )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# weighted_draw
# ---------------------------------------------------------------------------

def test_weighted_draw_deterministic_under_seed():
    weights = {"1": 1, "2": 5, "3": 1}
    a = weighted_draw(weights, random.Random(42))
    b = weighted_draw(weights, random.Random(42))
    assert a == b


def test_weighted_draw_proportional_distribution():
    weights = {"1": 1, "2": 9}
    rng = random.Random(7)
    counts = {"1": 0, "2": 0}
    N = 4000
    for _ in range(N):
        counts[weighted_draw(weights, rng)] += 1
    # "2" has 9x the weight of "1" -> should land close to 90%.
    assert 0.80 < counts["2"] / N < 0.98


def test_weighted_draw_all_zero_falls_back_to_uniform():
    weights = {"1": 0, "2": 0, "3": 0}
    rng = random.Random(1)
    picks = {weighted_draw(weights, rng) for _ in range(50)}
    assert picks  # did not raise, produced a real key
    assert picks <= {"1", "2", "3"}


# ---------------------------------------------------------------------------
# run_draw — core algorithm
# ---------------------------------------------------------------------------

def test_drawn_type_resets_others_age(tmp_path):
    db = _make_edit_history_db(tmp_path, [])
    state = default_state()
    outcome = run_draw(state, db_path=str(db), turn=5, count=1, rng=random.Random(0), now_iso="2026-08-04T00:00:00Z")
    new_state = outcome["state"]
    drawn = outcome["result"]["drawn_types"]
    assert len(drawn) == 1
    drawn_key = str(drawn[0])
    for k, v in new_state["types"].items():
        if k == drawn_key:
            assert v["weight"] == 0
            assert v["last_run_turn"] == 5
            assert v["last_run_at"] == "2026-08-04T00:00:00Z"
        else:
            assert v["weight"] == 2  # started at 1, +1


def test_default_count_is_one(tmp_path):
    db = _make_edit_history_db(tmp_path, [])
    outcome = run_draw(default_state(), db_path=str(db), turn=1, rng=random.Random(3))
    assert len(outcome["result"]["drawn_types"]) == 1


def test_count_two_draws_two_distinct_types(tmp_path):
    db = _make_edit_history_db(tmp_path, [])
    outcome = run_draw(default_state(), db_path=str(db), turn=1, count=2, rng=random.Random(9))
    drawn = outcome["result"]["drawn_types"]
    assert len(drawn) == 2
    assert len(set(drawn)) == 2


def test_fresh_state_all_weight_one():
    state = default_state()
    assert all(v["weight"] == 1 for v in state["types"].values())
    assert set(state["types"].keys()) == {str(n) for n in CATEGORIES}


def test_unrun_types_untouched(tmp_path):
    db = _make_edit_history_db(tmp_path, [])
    state = default_state()
    state["types"]["3"]["last_run_turn"] = 2
    state["types"]["3"]["last_run_at"] = "2026-08-01T00:00:00Z"
    state["types"]["3"]["weight"] = 0  # deterministically excluded: P(draw)=0 while others > 0
    outcome = run_draw(state, db_path=str(db), turn=10, count=1, interrupt_types=frozenset(), rng=random.Random(0))
    new_state = outcome["state"]
    assert 3 not in outcome["result"]["types_to_run"]  # sanity: this test needs "3" to NOT be drawn
    assert new_state["types"]["3"]["last_run_turn"] == 2
    assert new_state["types"]["3"]["last_run_at"] == "2026-08-01T00:00:00Z"
    assert new_state["types"]["3"]["weight"] == 1  # aged by 1 from 0


# ---------------------------------------------------------------------------
# Cascade interrupt
# ---------------------------------------------------------------------------

def test_interrupt_does_not_fire_below_threshold(tmp_path):
    db = _make_edit_history_db(tmp_path, [("retracted", "2026-08-04T00:00:00Z")] * 2)
    outcome = run_draw(
        _warm_state(), db_path=str(db), turn=1, interrupt_threshold=3, rng=random.Random(0)
    )
    assert outcome["result"]["cascade_marker_count"] == 2
    assert outcome["result"]["interrupt_fired"] is False
    assert outcome["result"]["interrupt_types"] == []


def test_interrupt_fires_at_threshold_and_forces_types(tmp_path):
    rows = [("retracted", "2026-08-04T00:00:00Z")] * 2 + [("superseded", "2026-08-04T00:00:01Z")]
    db = _make_edit_history_db(tmp_path, rows)
    outcome = run_draw(
        _warm_state(),
        db_path=str(db),
        turn=1,
        interrupt_threshold=3,
        interrupt_types=frozenset({2, 5}),
        rng=random.Random(0),
    )
    r = outcome["result"]
    assert r["cascade_marker_count"] == 3
    assert r["interrupt_fired"] is True
    assert r["interrupt_types"] == [2, 5]
    assert {2, 5}.issubset(set(r["types_to_run"]))
    new_state = outcome["state"]
    assert new_state["types"]["2"]["weight"] == 0
    assert new_state["types"]["5"]["weight"] == 0


def test_interrupted_types_excluded_from_random_pool(tmp_path):
    """An interrupt-forced type must not ALSO show up in drawn_types (no
    double bookkeeping -- it ran because of the interrupt, not the draw)."""
    rows = [("retracted", "2026-08-04T00:00:00Z")] * 5
    db = _make_edit_history_db(tmp_path, rows)
    outcome = run_draw(
        _warm_state(),
        db_path=str(db),
        turn=1,
        interrupt_threshold=3,
        interrupt_types=frozenset({2, 5}),
        count=1,
        rng=random.Random(0),
    )
    r = outcome["result"]
    assert not set(r["drawn_types"]) & {2, 5}


def test_interrupt_and_draw_both_happen(tmp_path):
    rows = [("retracted", "2026-08-04T00:00:00Z")] * 5
    db = _make_edit_history_db(tmp_path, rows)
    outcome = run_draw(
        _warm_state(),
        db_path=str(db),
        turn=1,
        interrupt_threshold=3,
        interrupt_types=frozenset({2, 5}),
        count=1,
        rng=random.Random(0),
    )
    r = outcome["result"]
    assert len(r["drawn_types"]) == 1
    assert len(r["interrupt_types"]) == 2
    assert len(r["types_to_run"]) == 3  # 1 drawn + 2 interrupt, disjoint


# ---------------------------------------------------------------------------
# window_params
# ---------------------------------------------------------------------------

def test_window_scoped_category_gets_pre_update_watermark(tmp_path):
    db = _make_edit_history_db(tmp_path, [])
    state = default_state()
    state["types"]["7"]["last_run_at"] = "2026-07-20T00:00:00Z"
    state["types"]["7"]["last_run_turn"] = 30
    state["types"]["7"]["weight"] = 6  # make it near-certain to be drawn
    for k in state["types"]:
        if k != "7":
            state["types"][k]["weight"] = 0
    outcome = run_draw(state, db_path=str(db), turn=40, count=1, rng=random.Random(0))
    r = outcome["result"]
    assert 7 in r["types_to_run"]
    assert r["window_params"]["7"] == "2026-07-20T00:00:00Z"
    # and the NEW state's watermark has moved forward
    assert outcome["state"]["types"]["7"]["last_run_at"] != "2026-07-20T00:00:00Z"


def test_never_run_window_scoped_category_gets_none(tmp_path):
    db = _make_edit_history_db(tmp_path, [])
    state = default_state()
    state["types"]["6"]["weight"] = 6
    for k in state["types"]:
        if k != "6":
            state["types"][k]["weight"] = 0
    outcome = run_draw(state, db_path=str(db), turn=1, count=1, rng=random.Random(0))
    r = outcome["result"]
    assert 6 in r["types_to_run"]
    assert r["window_params"]["6"] is None


def test_non_window_scoped_category_absent_from_window_params(tmp_path):
    db = _make_edit_history_db(tmp_path, [])
    state = default_state()
    state["types"]["3"]["weight"] = 6
    for k in state["types"]:
        if k != "3":
            state["types"][k]["weight"] = 0
    outcome = run_draw(state, db_path=str(db), turn=1, count=1, rng=random.Random(0))
    r = outcome["result"]
    assert 3 in r["types_to_run"]
    assert "3" not in r["window_params"]
    assert 3 not in WINDOW_SCOPED_CATEGORIES


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def test_save_load_roundtrip(tmp_path):
    path = tmp_path / "dream-lottery.json"
    state = default_state()
    state["types"]["4"]["weight"] = 3
    save_state(state, str(path))
    reloaded = load_state(str(path))
    assert reloaded == state


def test_load_missing_file_returns_default(tmp_path):
    path = tmp_path / "nonexistent.json"
    assert load_state(str(path)) == default_state()


def test_load_backfills_missing_category(tmp_path):
    path = tmp_path / "dream-lottery.json"
    partial = default_state()
    del partial["types"]["9"]  # simulate an install whose state predates category 9
    path.write_text(json.dumps(partial))
    reloaded = load_state(str(path))
    assert "9" in reloaded["types"]
    assert reloaded["types"]["9"]["weight"] == 1


# ---------------------------------------------------------------------------
# Cascade counting
# ---------------------------------------------------------------------------

def test_cold_start_does_not_replay_all_history_as_interrupt(tmp_path):
    """Regression: a fresh state (last_interrupt_check_at=None) must NOT
    scan/count all-time cascade markers -- caught live via manual dry-run
    against the real graph, where 81 lifetime markers spuriously tripped a
    threshold=3 interrupt on turn 1. Cold start seeds the baseline to now
    (0 markers), same as the forum monitors' seed-to-current convention."""
    rows = [("retracted", "2020-01-01T00:00:00Z")] * 50  # lots of ancient history
    db = _make_edit_history_db(tmp_path, rows)
    state = default_state()
    assert state["last_interrupt_check_at"] is None
    outcome = run_draw(state, db_path=str(db), turn=1, interrupt_threshold=3, rng=random.Random(0))
    assert outcome["result"]["cascade_marker_count"] == 0
    assert outcome["result"]["interrupt_fired"] is False


def test_count_cascade_markers_filters_action_and_since(tmp_path):
    rows = [
        ("retracted", "2026-08-01T00:00:00Z"),
        ("superseded", "2026-08-02T00:00:00Z"),
        ("created", "2026-08-03T00:00:00Z"),  # not a cascade action
        ("stale_flagged", "2026-08-03T12:00:00Z"),
        ("support_lost", "2026-08-04T00:00:00Z"),
    ]
    db = _make_edit_history_db(tmp_path, rows)
    assert count_cascade_markers(str(db), None) == 4  # all cascade actions, any time
    assert count_cascade_markers(str(db), "2026-08-03T00:00:00Z") == 2  # stale_flagged + support_lost
    assert count_cascade_markers(str(db), "2026-08-10T00:00:00Z") == 0


# ---------------------------------------------------------------------------
# Draw history (append-only, issue #1909)
# ---------------------------------------------------------------------------

def _draw_cli(state_path, db_path, turn, seed=None, extra=None, dry_run=False):
    """Invoke `draw` through the real CLI entrypoint (main()), not run_draw
    directly -- these tests exercise the CLI-level history-append wiring in
    cmd_draw, not the pure algorithm (already covered above)."""
    argv = ["draw", "--state", str(state_path), "--db", str(db_path), "--turn", str(turn)]
    if seed is not None:
        argv += ["--seed", str(seed)]
    if dry_run:
        argv.append("--dry-run")
    if extra:
        argv += extra
    rc = main(argv)
    return rc


def test_history_path_derives_from_custom_state(tmp_path):
    state_path = tmp_path / "sub" / "dream-lottery.json"
    expected = tmp_path / "sub" / "dream-lottery-history.jsonl"
    assert history_path_for_state(str(state_path)) == str(expected)


def test_real_draw_appends_one_history_line(tmp_path, capsys):
    state_path = tmp_path / "dream-lottery.json"
    history_path = tmp_path / "dream-lottery-history.jsonl"
    db = _make_edit_history_db(tmp_path, [])
    rc = _draw_cli(state_path, db, turn=1, seed=1)
    capsys.readouterr()
    assert rc == 0
    assert state_path.exists()
    assert history_path.exists()
    lines = history_path.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["turn"] == 1
    assert "drawn_types" in rec and "now" in rec


def test_three_draws_append_three_lines(tmp_path, capsys):
    state_path = tmp_path / "dream-lottery.json"
    history_path = tmp_path / "dream-lottery-history.jsonl"
    db = _make_edit_history_db(tmp_path, [])
    for i, seed in enumerate((1, 2, 3), start=1):
        rc = _draw_cli(state_path, db, turn=i, seed=seed)
        assert rc == 0
    capsys.readouterr()
    lines = history_path.read_text().splitlines()
    assert len(lines) == 3


def test_dry_run_writes_neither_state_nor_history(tmp_path, capsys):
    state_path = tmp_path / "dream-lottery.json"
    history_path = tmp_path / "dream-lottery-history.jsonl"
    db = _make_edit_history_db(tmp_path, [])
    rc = _draw_cli(state_path, db, turn=1, seed=1, dry_run=True)
    capsys.readouterr()
    assert rc == 0
    assert not state_path.exists()
    assert not history_path.exists()


def test_history_write_failure_does_not_break_draw(tmp_path, capsys):
    """Simulate a history-write failure (unwritable directory) -- the draw
    must still exit 0 and state must still be saved (state persists BEFORE
    the history append is even attempted -- see module docstring)."""
    state_path = tmp_path / "dream-lottery.json"
    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir(mode=0o555)
    history_path = readonly_dir / "dream-lottery-history.jsonl"
    db = _make_edit_history_db(tmp_path, [])
    try:
        rc = _draw_cli(
            state_path, db, turn=1, seed=1, extra=["--history", str(history_path)]
        )
        out = capsys.readouterr()
        assert rc == 0
        assert state_path.exists()
        assert not history_path.exists()
        assert "warning" in out.err.lower()
        # stdout must still carry the draw result JSON (draw itself succeeded)
        result = json.loads(out.out)
        assert result["turn"] == 1
    finally:
        readonly_dir.chmod(0o755)  # allow tmp_path cleanup


def test_history_subcommand_distinguishes_drawn_from_interrupt_forced(tmp_path, capsys):
    state_path = tmp_path / "dream-lottery.json"
    db = _make_edit_history_db(tmp_path, [])

    # Draw 1: cold start, no interrupt possible (last_interrupt_check_at is
    # None on a fresh state -> cascade_marker_count is seeded to 0 by
    # design). Picks one category by the random draw. seed=0 draws category
    # 8 (verified against weighted_draw directly) -- deliberately NOT 2 or
    # 5, so draw 1's random pick and draw 2's interrupt-forced set below
    # stay disjoint and the drawn-vs-forced assertions are unambiguous.
    rc1 = _draw_cli(state_path, db, turn=1, seed=0)
    out1 = json.loads(capsys.readouterr().out)
    assert rc1 == 0
    assert out1["interrupt_fired"] is False

    # Add cascade rows timestamped at/after draw 1's own `now`, at the
    # default interrupt_threshold (3), so draw 2's since-filter (seeded
    # from draw 1's last_interrupt_check_at) picks them all up.
    conn = sqlite3.connect(str(db))
    for i in range(3):
        conn.execute(
            "INSERT INTO edit_history (timestamp, turn, action, node_id, node_type, details) "
            "VALUES (?, 1, 'retracted', ?, 'observation', '{}')",
            (out1["now"], f"ob_x{i:02d}"),
        )
    conn.commit()
    conn.close()

    rc2 = _draw_cli(state_path, db, turn=2, seed=2)
    out2 = json.loads(capsys.readouterr().out)
    assert rc2 == 0
    assert out2["interrupt_fired"] is True
    assert out2["interrupt_types"] == [2, 5]  # DEFAULT_INTERRUPT_TYPES

    history_path = tmp_path / "dream-lottery-history.jsonl"
    tally = tally_history(str(history_path))
    assert tally["cycles_counted"] == 2
    assert tally["interrupt_cycles"] == 1
    assert tally["skipped_malformed"] == 0

    by_cat = {c["category"]: c for c in tally["categories"]}
    drawn1 = out1["drawn_types"][0]
    drawn2 = out2["drawn_types"][0]

    assert by_cat[drawn1]["drawn_count"] >= 1
    assert by_cat[drawn2]["drawn_count"] >= 1
    assert by_cat[2]["interrupt_forced_count"] == 1
    assert by_cat[2]["drawn_count"] == 0
    assert by_cat[5]["interrupt_forced_count"] == 1
    assert by_cat[5]["drawn_count"] == 0
    # Drawn and interrupt-forced are tallied separately even for the same
    # category, and total_runs is their sum.
    for cat in tally["categories"]:
        assert cat["total_runs"] == cat["drawn_count"] + cat["interrupt_forced_count"]


def test_malformed_line_skipped_and_counted(tmp_path):
    history_path = tmp_path / "dream-lottery-history.jsonl"
    good = json.dumps(
        {
            "turn": 1,
            "now": "2026-08-04T00:00:00Z",
            "drawn_types": [1],
            "interrupt_types": [],
            "interrupt_fired": False,
        },
        sort_keys=True,
    )
    history_path.write_text(good + "\n" + '{"turn": 2, "now": "trunc' + "\n")
    tally = tally_history(str(history_path))
    assert tally["cycles_counted"] == 1
    assert tally["skipped_malformed"] == 1


def test_missing_history_file_returns_empty(tmp_path):
    history_path = tmp_path / "does-not-exist.jsonl"
    tally = tally_history(str(history_path))
    assert tally["cycles_counted"] == 0
    assert tally["skipped_malformed"] == 0
    assert tally["first_timestamp"] is None
    assert tally["last_timestamp"] is None
    assert all(c["total_runs"] == 0 for c in tally["categories"])
