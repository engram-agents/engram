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

  window_params (per-type watermark for window-scoped categories):
    10. A window-scoped category (7) that ran gets its PRE-update last_run_at
        as its window_params value (so "since" reflects the last time IT
        actually ran, not this run).
    11. A never-run window-scoped category gets None (no prior watermark ->
        caller falls back to full-history scan).
    12. A non-window-scoped category that ran does NOT appear in window_params.

  state persistence:
    13. save_state then load_state round-trips exactly.
    14. load_state on a missing file returns default_state().
    15. load_state on a state file missing a newer category key back-fills it
        (forward-compat with an added category).

  draw-history (append-only, issue #1909):
    16. A real `draw` CLI invocation appends exactly one JSONL line; three
        draws append three lines.
    17. `--dry-run` writes neither state nor history.
    18. The default history path derives correctly from a custom `--state`.
    19. A history-write failure (unwritable path) does not break the draw:
        the CLI still exits 0 and state is still saved.
    20. The `history` subcommand tally is correct.
    21. A malformed/truncated line is skipped and counted in
        `skipped_malformed`, not fatal.
    22. A missing history file tallies as a valid, all-zero empty result.

  re-entrancy guard (issue #1907):
    23. A second real draw for the same --turn is refused (non-zero exit,
        no second state/history write) unless --force is passed.
    24. --force overrides the guard and the second draw proceeds normally.
    25. --dry-run is exempt from the guard even when a real draw already
        ran this turn (dry-run never persists, so nothing to corrupt).
    26. A draw for a genuinely different --turn is never blocked.

  history --limit false-clean fix (issue #1911):
    27. --limit 0 means "no limit", not "return nothing".
    28. A negative --limit also means "no limit".
    29. A positive --limit still restricts to the most recent N raw lines
        (sanity-checks the existing behavior stays intact).

  --count validation (reviewer-fairy finding on #1912's own review):
    30. --count 0 is rejected outright, before any write.
    31. A negative --count is rejected outright, before any write.
    32. Regression: two real --count 0 draws for the same turn both used
        to succeed silently (the guard's blind spot) -- both must now be
        rejected.

  cascade-interrupt removal (issue #2399, 2026-09-11 -- Lei's ruling: the
  #2013 cascade-frontier machine made nightly marker cleanup an
  unconditional dream-master obligation, so the urgency-interrupt
  mechanism was retired rather than re-pointed):
    33. run_draw() no longer accepts interrupt_types / interrupt_threshold
        -- passing either raises TypeError.
    34. tally_history() reads an OLD-format history line (written by
        pre-#2399 code, still carrying interrupt_types/interrupt_fired
        keys) without error and without surfacing an interrupt_cycles key.
    35. A draw result dict never contains cascade_marker_count /
        interrupt_fired / interrupt_types / interrupt_threshold.
    36. --interrupt-types / --interrupt-threshold are no longer valid
        `draw` CLI flags -- argparse rejects them.
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
    outcome = run_draw(state, db_path=str(db), turn=10, count=1, rng=random.Random(0))
    new_state = outcome["state"]
    assert 3 not in outcome["result"]["types_to_run"]  # sanity: this test needs "3" to NOT be drawn
    assert new_state["types"]["3"]["last_run_turn"] == 2
    assert new_state["types"]["3"]["last_run_at"] == "2026-08-01T00:00:00Z"
    assert new_state["types"]["3"]["weight"] == 1  # aged by 1 from 0


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


# ---------------------------------------------------------------------------
# Re-entrancy guard (issue #1907)
# ---------------------------------------------------------------------------

def test_second_draw_same_turn_is_refused(tmp_path, capsys):
    state_path = tmp_path / "dream-lottery.json"
    history_path = tmp_path / "dream-lottery-history.jsonl"
    db = _make_edit_history_db(tmp_path, [])
    rc1 = _draw_cli(state_path, db, turn=5, seed=1)
    state_after_first = state_path.read_text()
    capsys.readouterr()
    assert rc1 == 0

    rc2 = _draw_cli(state_path, db, turn=5, seed=2)
    out = capsys.readouterr()
    assert rc2 != 0
    assert "already ran" in out.err.lower()
    # Neither state nor history changed as a result of the refused draw.
    assert state_path.read_text() == state_after_first
    assert len(history_path.read_text().splitlines()) == 1


def test_force_overrides_reentrancy_guard(tmp_path, capsys):
    state_path = tmp_path / "dream-lottery.json"
    history_path = tmp_path / "dream-lottery-history.jsonl"
    db = _make_edit_history_db(tmp_path, [])
    rc1 = _draw_cli(state_path, db, turn=5, seed=1)
    capsys.readouterr()
    assert rc1 == 0

    rc2 = _draw_cli(state_path, db, turn=5, seed=2, extra=["--force"])
    capsys.readouterr()
    assert rc2 == 0
    # The forced second draw for the same turn re-applied the aging step --
    # two history lines now exist, both stamped turn=5.
    lines = history_path.read_text().splitlines()
    assert len(lines) == 2
    assert all(json.loads(ln)["turn"] == 5 for ln in lines)


def test_dry_run_bypasses_reentrancy_guard(tmp_path, capsys):
    state_path = tmp_path / "dream-lottery.json"
    db = _make_edit_history_db(tmp_path, [])
    rc1 = _draw_cli(state_path, db, turn=5, seed=1)
    capsys.readouterr()
    assert rc1 == 0

    rc2 = _draw_cli(state_path, db, turn=5, seed=2, dry_run=True)
    out = capsys.readouterr()
    assert rc2 == 0
    result = json.loads(out.out)
    assert result["turn"] == 5


def test_different_turn_draw_is_allowed(tmp_path, capsys):
    state_path = tmp_path / "dream-lottery.json"
    history_path = tmp_path / "dream-lottery-history.jsonl"
    db = _make_edit_history_db(tmp_path, [])
    rc1 = _draw_cli(state_path, db, turn=5, seed=1)
    capsys.readouterr()
    assert rc1 == 0

    rc2 = _draw_cli(state_path, db, turn=6, seed=2)
    capsys.readouterr()
    assert rc2 == 0
    assert len(history_path.read_text().splitlines()) == 2


# ---------------------------------------------------------------------------
# history --limit false-clean fix (issue #1911)
# ---------------------------------------------------------------------------

def _write_n_history_lines(path: Path, n: int) -> None:
    lines = [
        json.dumps(
            {
                "turn": i,
                "now": f"2026-08-0{i}T00:00:00Z",
                "drawn_types": [1],
                "interrupt_types": [],
                "interrupt_fired": False,
            },
            sort_keys=True,
        )
        for i in range(1, n + 1)
    ]
    path.write_text("\n".join(lines) + "\n")


def test_limit_zero_means_no_limit(tmp_path):
    history_path = tmp_path / "dream-lottery-history.jsonl"
    _write_n_history_lines(history_path, 3)
    tally = tally_history(str(history_path), limit=0)
    assert tally["cycles_counted"] == 3


def test_limit_negative_means_no_limit(tmp_path):
    history_path = tmp_path / "dream-lottery-history.jsonl"
    _write_n_history_lines(history_path, 3)
    tally = tally_history(str(history_path), limit=-1)
    assert tally["cycles_counted"] == 3


def test_limit_positive_restricts_to_last_n(tmp_path):
    history_path = tmp_path / "dream-lottery-history.jsonl"
    _write_n_history_lines(history_path, 5)
    tally = tally_history(str(history_path), limit=2)
    assert tally["cycles_counted"] == 2
    # the last 2 of 5 lines are turns 4 and 5
    assert tally["categories"][0]["last_run_turn"] == 5


# ---------------------------------------------------------------------------
# --count validation (reviewer-fairy finding on #1912's own review):
# --count 0 (or negative) with no interrupt firing leaves ran_keys empty,
# so no category's last_run_turn gets stamped and the re-entrancy guard
# silently never fires -- verified live via a real two-invocation CLI
# reproduction before this fix shipped.
# ---------------------------------------------------------------------------

def test_count_zero_is_rejected(tmp_path, capsys):
    state_path = tmp_path / "dream-lottery.json"
    db = _make_edit_history_db(tmp_path, [])
    rc = _draw_cli(state_path, db, turn=5, extra=["--count", "0"])
    out = capsys.readouterr()
    assert rc != 0
    assert "count must be" in out.err.lower()
    assert not state_path.exists()


def test_count_negative_is_rejected(tmp_path, capsys):
    state_path = tmp_path / "dream-lottery.json"
    db = _make_edit_history_db(tmp_path, [])
    rc = _draw_cli(state_path, db, turn=5, extra=["--count", "-3"])
    out = capsys.readouterr()
    assert rc != 0
    assert "count must be" in out.err.lower()
    assert not state_path.exists()


def test_count_zero_no_longer_defeats_reentrancy_guard(tmp_path, capsys):
    """Regression test for the reviewer-fairy's exact reproduction: two
    real `draw --turn 5 --count 0` calls against the same state file used
    to both succeed (rc=0), silently double-aging every category's weight
    with no category's last_run_turn ever getting stamped. Both calls must
    now be rejected before touching state at all."""
    state_path = tmp_path / "dream-lottery.json"
    db = _make_edit_history_db(tmp_path, [])
    rc1 = _draw_cli(state_path, db, turn=5, extra=["--count", "0"])
    rc2 = _draw_cli(state_path, db, turn=5, extra=["--count", "0"])
    capsys.readouterr()
    assert rc1 != 0
    assert rc2 != 0
    assert not state_path.exists()


# ---------------------------------------------------------------------------
# cascade-interrupt removal (issue #2399, 2026-09-11)
# ---------------------------------------------------------------------------

def test_run_draw_no_longer_accepts_interrupt_types(tmp_path):
    db = _make_edit_history_db(tmp_path, [])
    with pytest.raises(TypeError):
        run_draw(
            default_state(),
            db_path=str(db),
            turn=1,
            interrupt_types=frozenset({2, 5}),
            rng=random.Random(0),
        )


def test_run_draw_no_longer_accepts_interrupt_threshold(tmp_path):
    db = _make_edit_history_db(tmp_path, [])
    with pytest.raises(TypeError):
        run_draw(
            default_state(),
            db_path=str(db),
            turn=1,
            interrupt_threshold=3,
            rng=random.Random(0),
        )


def test_tally_history_handles_old_format_line_with_interrupt_fields(tmp_path):
    """Backward compat: `dream-lottery-history.jsonl` lines written by
    pre-#2399 code still carry `interrupt_types`/`interrupt_fired` keys.
    tally_history() must read such a line without error and must not
    surface an `interrupt_cycles` key in its own output."""
    history_path = tmp_path / "dream-lottery-history.jsonl"
    old_format_line = json.dumps(
        {
            "turn": 1,
            "now": "2026-08-04T00:00:00Z",
            "drawn_types": [1],
            "types_to_run": [1],
            "interrupt_types": [2, 5],
            "interrupt_fired": True,
            "interrupt_threshold": 3,
            "cascade_marker_count": 7,
        },
        sort_keys=True,
    )
    history_path.write_text(old_format_line + "\n")
    tally = tally_history(str(history_path))
    assert tally["cycles_counted"] == 1
    assert tally["skipped_malformed"] == 0
    assert "interrupt_cycles" not in tally
    by_cat = {c["category"]: c for c in tally["categories"]}
    assert by_cat[1]["drawn_count"] == 1


def test_draw_result_omits_interrupt_and_cascade_keys(tmp_path):
    db = _make_edit_history_db(tmp_path, [])
    outcome = run_draw(default_state(), db_path=str(db), turn=1, rng=random.Random(0))
    r = outcome["result"]
    for removed_key in (
        "cascade_marker_count",
        "interrupt_fired",
        "interrupt_types",
        "interrupt_threshold",
    ):
        assert removed_key not in r
    assert "last_interrupt_check_at" not in outcome["state"]


def test_interrupt_cli_flags_no_longer_valid(tmp_path, capsys):
    state_path = tmp_path / "dream-lottery.json"
    db = _make_edit_history_db(tmp_path, [])
    with pytest.raises(SystemExit) as exc1:
        main(
            [
                "draw",
                "--state",
                str(state_path),
                "--db",
                str(db),
                "--turn",
                "1",
                "--interrupt-types",
                "2,5",
            ]
        )
    capsys.readouterr()
    assert exc1.value.code == 2
    with pytest.raises(SystemExit) as exc2:
        main(
            [
                "draw",
                "--state",
                str(state_path),
                "--db",
                str(db),
                "--turn",
                "1",
                "--interrupt-threshold",
                "3",
            ]
        )
    capsys.readouterr()
    assert exc2.value.code == 2
