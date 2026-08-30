#!/usr/bin/env python3
"""dream_lottery.py — Weight-aging lottery draw for the sleep-cycle dream fairies.

Design of record: Lei, converged with Kepler 2026-08-03 (engram-alpha#1901),
refined same night to a three-class split (comment on #1901):

  1. **Always-on** — the batch-summary (recall_summary) cohort step. Its work
     scales with the day's new-node cohort and is essential whenever nodes
     were created. NOT touched by this module — cohort_dispatch.py's
     ``prev-sleep`` cadence is already correct for an every-night step.
  2. **Lottery-rotated** — the scan categories in CATEGORIES below (the
     dream-fairy set plus person-node-maintenance and support-lost; the
     dict is the count's single source of truth — prose counts here have
     drifted twice). Exactly ONE type is drawn per sleep (``--count`` can
     raise this later; the design note says "maybe two later"). All-8-nightly
     was the heaviest recurring token spend for a mostly-zero-findings scan
     at current graph sizes.
  3. **Urgency interrupt** — if the day's retraction/supersede-cascade
     markers (``edit_history.action IN ('retracted', 'superseded',
     'stale_flagged', 'support_lost')``) meet or exceed a threshold, the
     cascade-sensitive categories are force-included regardless of the draw.
     Lottery = frequency; interrupt = urgency (issue's own framing).

Algorithm (weight-aging lottery, issue #1901 design 1):
  - All types start weight 1.
  - Draw proportional to weight (a type with weight W has P = W / sum(weights)).
  - The drawn type's weight resets to 0; every OTHER type's weight += 1.
  - Interrupt-forced types are treated the same as a draw for weight-reset
    purposes ("its weight still resets") but do not consume the random draw.
  - Properties: roughly uniform draw frequency over time: no starvation
    (an overdue type's weight climbs without bound until it is drawn with
    near-certainty); no phase-locking with any fixed-period external rhythm,
    because the draw is a function of accumulated skips, not calendar day.

State file: ``~/.engram/dream-lottery.json`` — schema in DEFAULT_STATE below.
Written eagerly at draw time (weights-in -> weights-out is one atomic
event, matching the issue's own "logged draws" framing: weights-in, type
drawn, weights-out are a single record). Known limitation, not fixed by
this module: if a drawn fairy times out before returning a report (see
engram-sleep SKILL.md Step 7's fairy-timeout table), its watermark still
advances as if it had scanned the window -- the same exposure Category 7's
existing single-scoped fairy already carries under an all-8-nightly regime,
now spread across a longer inter-draw gap. Flagged in the audit, not solved
here; a timed-out lottery draw is a rare event, not a routine correctness
gap.

Re-entrancy (issue #1907): a real (non-dry-run) `draw` refuses to run a
second time for the same ``--turn`` -- re-running would double-apply the
aging step and silently corrupt the sequence. Override with ``--force``
if you genuinely mean it (e.g. recovering from a bad prior run). Dry-runs
are exempt since they never persist. The guard's signal is "did any
category's ``last_run_turn`` get stamped this turn" -- which only happens
for types in ``ran_keys`` (drawn ∪ interrupt-forced). A ``--count 0`` draw
with no interrupt firing would leave ``ran_keys`` empty and silently
defeat the guard (caught by reviewer-fairy on #1912's own review, verified
live before shipping); ``--count`` is therefore rejected below 1.

History file (issue #1909): ``~/.engram/dream-lottery-history.jsonl`` —
derived from the state path (``dream-lottery.json`` ->
``dream-lottery-history.jsonl``, same directory), not a fixed path, so a
``--state`` override (tests, alternate installs) stays isolated end to end.
Override explicitly with ``--history``. One compact JSON line is appended
per draw — the full ``result`` record from ``run_draw`` (it already carries
``turn`` and ``now``), so the rich per-draw shape (``weights_before``,
``weights_after``, ``drawn_types``, ``types_to_run``, ``interrupt_fired``,
``cascade_marker_count``) that used to be printed to stdout and lost is now
durable and queryable via the ``history`` subcommand. No rotation or
pruning: one line a night is ~365 lines/year, cheap indefinitely — do not
add log rotation here without a real reason to.

**Ordering is load-bearing: ``save_state`` runs FIRST, the history append
SECOND.** State is required for the aging sequence to be correct; history
is observability on top of an already-correct draw. A logged draw that
never took effect (state write failed/skipped) would be worse than an
effective draw that went unlogged (history write failed) — so state
commits before we even attempt to log it, and a history-write failure is
caught and downgraded to a stderr warning: it must never fail the command
or imply the draw itself didn't happen. See ``cmd_draw``.

The `INTERRUPT_TYPES` default ({2, 5} -- contradictions + tainted-valid) is
this module's own reading of the issue's "cascade/contradiction fairy"
phrase, not literally specified there (Kepler's formal spec had not landed
as of #1901's filing). Flagged for Kepler/Lei's read; override via
--interrupt-types if the call should differ.

Usage:
    python -m tools.dream_lottery draw --db ~/.engram/knowledge.db --turn 39
    python -m tools.dream_lottery draw --turn 39 --force  # override the re-entrancy guard
    python -m tools.dream_lottery status
    python -m tools.dream_lottery history --limit 30
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STATE_PATH = os.path.expanduser("~/.engram/dream-lottery.json")
DEFAULT_DB_PATH = os.path.expanduser("~/.engram/knowledge.db")


def history_path_for_state(state_path: str) -> str:
    """Derive the append-only draw-history path from a state path.

    ``dream-lottery.json`` -> ``dream-lottery-history.jsonl``, same
    directory. Deriving from the state path (rather than a fixed constant)
    keeps a ``--state`` override (tests, alternate installs) isolated end
    to end -- a test pointing ``--state`` at tmp_path must not also leak a
    history line into the real ``~/.engram/``.
    """
    p = Path(state_path)
    return str(p.with_name(f"{p.stem}-history.jsonl"))


DEFAULT_HISTORY_PATH = history_path_for_state(DEFAULT_STATE_PATH)

# Canonical category table -- mirrors engram-sleep/SKILL.md Step 6 and
# engram-dream-fairy.md's category numbering. Update both when adding,
# renaming, or renumbering a category (same discipline the SKILL.md
# dispatch-table comment already asks for).
CATEGORIES: dict[int, str] = {
    1: "open-questions",
    2: "contradictions",
    3: "stale-load-bearing",
    4: "cornerstone-candidates",
    5: "tainted-valid",
    6: "resolution-echoes",
    7: "missing-edges",
    8: "stale-task-refs",
    9: "coverage-gaps",
    10: "person-node-maintenance",
    11: "support-lost",
}

# Categories whose scan is genuinely window-scoped (per the #1901 audit --
# see docs/ audit note / PR description). Everything else runs a full-graph
# scan every time it's drawn, so a per-type watermark is informational only
# for those, not load-bearing. Category 6's watermark specifically backs
# its INLINE FALLBACK path only -- the primary engram_query_pattern path is
# self-relative and needs no watermark at all (Luria's #1903 review note);
# category 7 is watermark-scoped throughout, no such split.
WINDOW_SCOPED_CATEGORIES: frozenset[int] = frozenset({6, 7})

# Emptied 2026-08-19 per Lei + #1992 -- the {2,5} force-set checked
# contradiction/taint NODES, not the day's stale/taint MARKERS the interrupt
# intends, and dominated rotation (12 of 19 fairy runs forced over the first
# 11 draws). Re-point via --interrupt-types when the #2013 cascade machine
# gives dreams a principled marker-resolution path.
DEFAULT_INTERRUPT_TYPES: frozenset[int] = frozenset()

# #1901 says only "a small threshold" -- 3 is this module's own calibration
# call, not a value the issue specifies. Override with --interrupt-threshold;
# revisit once there's real data on how often 3 fires in practice.
DEFAULT_INTERRUPT_THRESHOLD = 3

CASCADE_ACTIONS = ("retracted", "superseded", "stale_flagged", "support_lost")

STATE_SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_state() -> dict:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "types": {
            str(n): {"weight": 1, "last_run_turn": None, "last_run_at": None}
            for n in CATEGORIES
        },
        "last_interrupt_check_at": None,
    }


def load_state(path: str = DEFAULT_STATE_PATH) -> dict:
    p = Path(path)
    if not p.exists():
        return default_state()
    try:
        state = json.loads(p.read_text())
    except (ValueError, OSError):
        return default_state()
    # Forward-compat: fill in any category added since this state file was
    # last written, rather than erroring or silently ignoring it.
    base = default_state()
    if "types" not in state:
        state["types"] = base["types"]
    for k, v in base["types"].items():
        state["types"].setdefault(k, v)
    state.setdefault("last_interrupt_check_at", None)
    state.setdefault("schema_version", STATE_SCHEMA_VERSION)
    return state


def save_state(state: dict, path: str = DEFAULT_STATE_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(p)  # atomic on POSIX


def append_history(result: dict, path: str = DEFAULT_HISTORY_PATH) -> None:
    """Append one compact JSON line for this draw's ``result`` record.

    Append-only -- no rotation/pruning here (see module docstring: ~365
    lines/year is cheap indefinitely). Callers must NOT rely on this for
    draw correctness: it is observability layered on top of an already-
    persisted state write (see the ordering note on ``cmd_draw``), and a
    caller should treat any exception from this function as non-fatal.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, sort_keys=True) + "\n")


def count_cascade_markers(db_path: str, since_iso: str | None) -> int:
    """Count edit_history rows for cascade-producing actions since `since_iso`.

    `since_iso=None` counts across all of history. NOT reached by
    `run_draw`'s cold-start path -- that branch short-circuits to a
    baseline of 0 rather than ever calling this function with `None` (see
    the cold-start comment in `run_draw`; caught by Borges reviewing
    #1910, #1907). This all-history mode is exercised only by a direct
    call (e.g. tests) or a future explicit audit path, not by the normal
    draw flow.
    """
    conn = sqlite3.connect(db_path)
    try:
        placeholders = ",".join("?" for _ in CASCADE_ACTIONS)
        if since_iso:
            row = conn.execute(
                f"SELECT COUNT(*) FROM edit_history "
                f"WHERE action IN ({placeholders}) AND timestamp >= ?",
                (*CASCADE_ACTIONS, since_iso),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT COUNT(*) FROM edit_history WHERE action IN ({placeholders})",
                CASCADE_ACTIONS,
            ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def weighted_draw(weights: dict[str, float], rng: random.Random) -> str:
    """Pick one key from `weights`, probability proportional to its value.

    All-zero weights (every type just ran once, degenerate first-cycle edge
    case) fall back to a uniform draw over all keys rather than raising --
    a lottery with no live tickets still has to pick someone.
    """
    keys = list(weights.keys())
    total = sum(weights.values())
    if total <= 0:
        return rng.choice(keys)
    pick = rng.uniform(0, total)
    upto = 0.0
    for k in keys:
        upto += weights[k]
        if pick <= upto:
            return k
    return keys[-1]  # float-rounding fallback


def run_draw(
    state: dict,
    db_path: str,
    turn: int,
    count: int = 1,
    interrupt_types: frozenset[int] = DEFAULT_INTERRUPT_TYPES,
    interrupt_threshold: int = DEFAULT_INTERRUPT_THRESHOLD,
    rng: random.Random | None = None,
    now_iso: str | None = None,
) -> dict:
    """Pure(ish) draw step: mutates and returns a NEW state dict + a result
    record. Does not touch disk -- caller persists via save_state.
    """
    rng = rng or random.Random()
    now_iso = now_iso or _now_iso()

    weights_before = {k: v["weight"] for k, v in state["types"].items()}

    prev_check = state.get("last_interrupt_check_at")
    if prev_check is None:
        # Cold start (state file never recorded a check): seed the baseline
        # to now rather than scanning all of history. Matches this repo's
        # own seed-to-current convention for the forum monitors -- and
        # Kepler's own pilot note on #1901 ("night one needs no data --
        # uniform draw"). Scanning full history here would spuriously fire
        # the interrupt on turn 1 of any install with real cascade activity
        # in its past (caught live: 81 lifetime markers tripped a
        # threshold=3 interrupt on a fresh dry-run against this graph).
        cascade_count = 0
    else:
        cascade_count = count_cascade_markers(db_path, prev_check)
    # An empty interrupt_types set has nothing to force-include, so it must
    # never report interrupt_fired=True -- the cascade count is still
    # computed and recorded below regardless (result["cascade_marker_count"]),
    # only the force-triggering is gated on there being types to force.
    interrupt_fired = bool(interrupt_types) and cascade_count >= interrupt_threshold
    interrupt_keys = {str(n) for n in interrupt_types} if interrupt_fired else set()

    weights_for_draw = {k: v for k, v in weights_before.items() if k not in interrupt_keys}
    drawn: list[str] = []
    pool = dict(weights_for_draw)
    for _ in range(max(count, 0)):
        if not pool:
            break
        pick = weighted_draw(pool, rng)
        drawn.append(pick)
        del pool[pick]  # don't draw the same type twice in one night

    ran_keys = set(drawn) | interrupt_keys

    new_types = {}
    for k, v in state["types"].items():
        if k in ran_keys:
            new_types[k] = {
                "weight": 0,
                "last_run_turn": turn,
                "last_run_at": now_iso,
            }
        else:
            new_types[k] = {
                "weight": v["weight"] + 1,
                "last_run_turn": v["last_run_turn"],
                "last_run_at": v["last_run_at"],
            }

    new_state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "types": new_types,
        "last_interrupt_check_at": now_iso,
    }

    window_params = {}
    for k in sorted(ran_keys, key=int):
        cat_num = int(k)
        if cat_num in WINDOW_SCOPED_CATEGORIES:
            window_params[k] = state["types"][k]["last_run_at"]  # pre-update watermark

    result = {
        "turn": turn,
        "now": now_iso,
        "cascade_marker_count": cascade_count,
        "interrupt_threshold": interrupt_threshold,
        "interrupt_fired": interrupt_fired,
        "interrupt_types": sorted(int(k) for k in interrupt_keys),
        "drawn_types": sorted(int(k) for k in drawn),
        "types_to_run": sorted(int(k) for k in ran_keys),
        "types_to_run_slugs": {k: CATEGORIES[int(k)] for k in ran_keys},
        "window_scoped_running": sorted(
            int(k) for k in ran_keys if int(k) in WINDOW_SCOPED_CATEGORIES
        ),
        "window_params": window_params,  # {category_num_str: prev last_run_at or null}
        "weights_before": {int(k): v for k, v in weights_before.items()},
        "weights_after": {int(k): new_types[k]["weight"] for k in new_types},
    }

    return {"state": new_state, "result": result}


def _already_drawn_this_turn(state: dict, turn: int) -> bool:
    """True if some category's `last_run_turn` already equals `turn` --
    i.e. a real (non-dry-run) draw has already run for this turn.

    Re-running `draw` for the same turn would double-apply the aging step
    (this turn's winners get re-reset to 0, this turn's losers age a
    second time before the next real turn even happens), silently
    corrupting the weight-aging sequence. See #1907.
    """
    return any(v.get("last_run_turn") == turn for v in state["types"].values())


def _validate_draw_args(args: argparse.Namespace) -> str | None:
    """Reject nonsensical integer CLI args before any work happens.

    Returns an error message string if invalid, ``None`` if OK. This is a
    class-level fix, not a one-off: this module has several unvalidated
    integer CLI args (``--count``, ``--limit``, ``--interrupt-threshold``,
    ``--turn``), and three independent failures were found in one review
    pass on #1912 (Sol's reviewer-fairy: ``--count 0`` silently defeats the
    re-entrancy guard; Borges: ``--interrupt-threshold 0`` fires the
    interrupt unconditionally on a night with zero cascade activity,
    silently disabling the lottery's whole rotation premise). Each arg
    gets an explicit, named decision here rather than three independent
    patches, so a fourth bad value doesn't wait for a fourth reviewer to
    notice it by hand:

    - ``--count < 1``: REJECTED. A draw that draws nothing has no design
      purpose, and when no interrupt fires it also silently defeats the
      re-entrancy guard (no category's ``last_run_turn`` gets stamped, so
      a same-turn re-run is never detected).
    - ``--interrupt-threshold < 1``: REJECTED. ``interrupt_fired =
      bool(interrupt_types) and cascade_count >= interrupt_threshold`` is
      unconditionally true for any ``threshold <= 0`` *when a force-set is
      configured* (cascade_count is never negative) -- the exact value
      someone would plausibly type meaning "no threshold", the opposite of
      what it actually does. On the default empty ``interrupt_types`` this
      guard is currently inert (nothing to force-include either way), but
      it stays for anyone who passes ``--interrupt-types``, which is
      exactly the configuration #2013 will restore.
    - ``--limit`` (history subcommand, not draw): deliberately NOT a
      reject case. ``<= 0`` is a documented sentinel meaning "no limit"
      (issue #1911) -- handled in ``tally_history``, not here.
    - ``--turn``: NOT validated here. No concrete failure mode has been
      found for a non-positive turn number (the re-entrancy guard and the
      aging logic are turn-sign-agnostic) -- left open pending a real
      case rather than guarded against a hypothetical one.
    """
    if args.count < 1:
        return (
            f"--count must be >= 1 (got {args.count}). A draw that draws nothing "
            "has no design purpose and, when no interrupt fires, silently defeats "
            "the re-entrancy guard (no category's last_run_turn gets set, so a "
            "same-turn re-run is never detected)."
        )
    if args.interrupt_threshold < 1:
        return (
            f"--interrupt-threshold must be >= 1 (got {args.interrupt_threshold}). "
            "interrupt_fired = bool(interrupt_types) and cascade_count >= "
            "interrupt_threshold is unconditionally true for threshold <= 0 "
            "when a force-set is configured (--interrupt-types), silently "
            "forcing the interrupt-mapped categories in every night regardless "
            "of real cascade activity. On the default empty interrupt_types "
            "this has no effect."
        )
    return None


def cmd_draw(args: argparse.Namespace) -> int:
    validation_error = _validate_draw_args(args)
    if validation_error:
        print(f"error: {validation_error}", file=sys.stderr)
        return 1
    state = load_state(args.state)
    if not args.dry_run and not args.force and _already_drawn_this_turn(state, args.turn):
        print(
            f"error: a draw already ran for turn {args.turn} (state: {args.state}). "
            "Re-running would double-apply the aging step and corrupt the sequence. "
            "Pass --force to override.",
            file=sys.stderr,
        )
        return 1
    interrupt_types = (
        frozenset(int(x) for x in args.interrupt_types.split(","))
        if args.interrupt_types
        else DEFAULT_INTERRUPT_TYPES
    )
    rng = random.Random(args.seed) if args.seed is not None else random.Random()
    outcome = run_draw(
        state,
        db_path=args.db,
        turn=args.turn,
        count=args.count,
        interrupt_types=interrupt_types,
        interrupt_threshold=args.interrupt_threshold,
        rng=rng,
    )
    history_path = args.history or history_path_for_state(args.state)
    if not args.dry_run:
        # Ordering is load-bearing -- see module docstring. save_state
        # first: it is required for the aging sequence to be correct.
        # append_history second, wrapped so a history-write failure can
        # never break the draw: state is already persisted by this point,
        # so a logged draw that didn't take effect would be worse than an
        # effective draw that wasn't logged.
        save_state(outcome["state"], args.state)
        try:
            append_history(outcome["result"], history_path)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad; see docstring
            print(
                f"warning: failed to append dream-lottery draw history to "
                f"{history_path}: {exc}",
                file=sys.stderr,
            )
    print(json.dumps(outcome["result"], indent=2, sort_keys=True))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    rows = []
    for k in sorted(state["types"].keys(), key=int):
        v = state["types"][k]
        rows.append(
            {
                "category": int(k),
                "slug": CATEGORIES.get(int(k), "?"),
                "weight": v["weight"],
                "last_run_turn": v["last_run_turn"],
                "last_run_at": v["last_run_at"],
                "window_scoped": int(k) in WINDOW_SCOPED_CATEGORIES,
            }
        )
    print(
        json.dumps(
            {"state_path": args.state, "last_interrupt_check_at": state["last_interrupt_check_at"], "types": rows},
            indent=2,
        )
    )
    return 0


def tally_history(path: str, limit: int | None = None) -> dict:
    """Read the append-only draw-history file and tally per-category and
    per-cycle stats.

    Missing file -> a valid, all-zero result (not an error): a fresh
    install or a soak that hasn't drawn yet is a legitimate empty state,
    not a failure. Malformed/truncated lines (a crash mid-append can leave
    a partial trailing line) are skipped and counted in
    ``skipped_malformed`` rather than raising -- one bad line must not hide
    every good one before/after it.

    ``limit`` (if given and positive) restricts the tally to the most
    recent N raw lines in the file (append-only, so "most recent" ==
    "last N lines"), matching before parsing/validity-filtering --
    consistent with "the most recent N cycles" read as "the most recent N
    draw attempts", malformed or not. `limit=None` OR `limit <= 0` both
    mean NO restriction -- `--limit 0` is the natural way to type "no
    limit", and a silent empty tally there would be a false-clean in a
    diagnostic tool (indistinguishable from "no draws recorded"); see
    #1911.
    """
    p = Path(path)
    if p.exists():
        lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
    else:
        lines = []
    if limit is not None and limit > 0:
        lines = lines[-limit:]

    per_category = {
        str(n): {
            "category": n,
            "slug": CATEGORIES[n],
            "drawn_count": 0,
            "interrupt_forced_count": 0,
            "total_runs": 0,
            "last_run_at": None,
            "last_run_turn": None,
        }
        for n in CATEGORIES
    }

    cycles_counted = 0
    interrupt_cycles = 0
    skipped_malformed = 0
    first_timestamp: str | None = None
    last_timestamp: str | None = None

    for line in lines:
        try:
            rec = json.loads(line)
            drawn = rec["drawn_types"]
            interrupt = rec["interrupt_types"]
            turn = rec["turn"]
            now = rec["now"]
            interrupt_fired = rec["interrupt_fired"]
        except (ValueError, TypeError, KeyError):
            skipped_malformed += 1
            continue

        cycles_counted += 1
        if interrupt_fired:
            interrupt_cycles += 1
        if first_timestamp is None:
            first_timestamp = now
        last_timestamp = now

        for cat_num in drawn:
            entry = per_category.get(str(cat_num))
            if entry is None:
                continue
            entry["drawn_count"] += 1
            entry["total_runs"] += 1
            entry["last_run_at"] = now
            entry["last_run_turn"] = turn
        for cat_num in interrupt:
            entry = per_category.get(str(cat_num))
            if entry is None:
                continue
            entry["interrupt_forced_count"] += 1
            entry["total_runs"] += 1
            entry["last_run_at"] = now
            entry["last_run_turn"] = turn

    return {
        "history_path": str(p),
        "cycles_counted": cycles_counted,
        "interrupt_cycles": interrupt_cycles,
        "skipped_malformed": skipped_malformed,
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
        "categories": [per_category[str(n)] for n in sorted(CATEGORIES)],
    }


def cmd_history(args: argparse.Namespace) -> int:
    history_path = args.history or history_path_for_state(args.state)
    result = tally_history(history_path, limit=args.limit)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_draw = sub.add_parser("draw", help="Run tonight's weight-aging lottery draw")
    p_draw.add_argument("--state", default=DEFAULT_STATE_PATH, help="Path to dream-lottery.json")
    p_draw.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to knowledge.db")
    p_draw.add_argument("--turn", type=int, required=True, help="Current turn number (pre-advance)")
    p_draw.add_argument("--count", type=int, default=1, help="Types to draw (default 1; design allows raising to 2 later; must be >= 1)")
    p_draw.add_argument("--interrupt-types", default=None, help="Comma-separated category numbers force-included on cascade interrupt (default: 2,5)")
    p_draw.add_argument("--interrupt-threshold", type=int, default=DEFAULT_INTERRUPT_THRESHOLD, help="Cascade-marker count that triggers the interrupt (must be >= 1)")
    p_draw.add_argument("--seed", type=int, default=None, help="Seed the RNG (testing/reproducibility only)")
    p_draw.add_argument("--dry-run", action="store_true", help="Compute and print the draw without persisting state")
    p_draw.add_argument(
        "--force",
        action="store_true",
        help="Override the same-turn re-entrancy guard (corrupts the aging sequence if misused)",
    )
    p_draw.add_argument(
        "--history",
        default=None,
        help="Path to the append-only draw-history JSONL (default: derived from --state)",
    )
    p_draw.set_defaults(func=cmd_draw)

    p_status = sub.add_parser("status", help="Print current lottery state (human/CI readable)")
    p_status.add_argument("--state", default=DEFAULT_STATE_PATH, help="Path to dream-lottery.json")
    p_status.set_defaults(func=cmd_status)

    p_history = sub.add_parser("history", help="Tally the append-only draw-history file")
    p_history.add_argument(
        "--state",
        default=DEFAULT_STATE_PATH,
        help="Path to dream-lottery.json (used only to derive the default --history path)",
    )
    p_history.add_argument(
        "--history",
        default=None,
        help="Path to dream-lottery-history.jsonl (default: derived from --state)",
    )
    p_history.add_argument("--limit", type=int, default=None, help="Restrict tally to the most recent N cycles (0 or negative = no limit)")
    p_history.set_defaults(func=cmd_history)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
