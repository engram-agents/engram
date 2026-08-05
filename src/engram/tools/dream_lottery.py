#!/usr/bin/env python3
"""dream_lottery.py — Weight-aging lottery draw for the sleep-cycle dream fairies.

Design of record: Lei, converged with Kepler 2026-08-03 (engram-alpha#1901),
refined same night to a three-class split (comment on #1901):

  1. **Always-on** — the batch-summary (recall_summary) cohort step. Its work
     scales with the day's new-node cohort and is essential whenever nodes
     were created. NOT touched by this module — cohort_dispatch.py's
     ``prev-sleep`` cadence is already correct for an every-night step.
  2. **Lottery-rotated** — the 9 dream-fairy scan categories (open-questions,
     contradictions, stale-load-bearing, cornerstone-candidates,
     tainted-valid, resolution-echoes, missing-edges, stale-task-refs,
     coverage-gaps). Exactly ONE type is drawn per sleep (``--count`` can
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

The `INTERRUPT_TYPES` default ({2, 5} -- contradictions + tainted-valid) is
this module's own reading of the issue's "cascade/contradiction fairy"
phrase, not literally specified there (Kepler's formal spec had not landed
as of #1901's filing). Flagged for Kepler/Lei's read; override via
--interrupt-types if the call should differ.

Usage:
    python -m tools.dream_lottery draw --db ~/.engram/knowledge.db --turn 39
    python -m tools.dream_lottery status
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
}

# Categories whose scan is genuinely window-scoped (per the #1901 audit --
# see docs/ audit note / PR description). Everything else runs a full-graph
# scan every time it's drawn, so a per-type watermark is informational only
# for those, not load-bearing.
WINDOW_SCOPED_CATEGORIES: frozenset[int] = frozenset({6, 7})

# See module docstring -- this module's own reading of "cascade/contradiction
# fairy", not a literal quote from #1901. Override with --interrupt-types.
DEFAULT_INTERRUPT_TYPES: frozenset[int] = frozenset({2, 5})

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


def count_cascade_markers(db_path: str, since_iso: str | None) -> int:
    """Count edit_history rows for cascade-producing actions since `since_iso`.

    `since_iso=None` counts across all of history (used only when the state
    file has never recorded a check -- first-ever draw on a fresh install).
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
    interrupt_fired = cascade_count >= interrupt_threshold
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


def cmd_draw(args: argparse.Namespace) -> int:
    state = load_state(args.state)
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
    if not args.dry_run:
        save_state(outcome["state"], args.state)
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
    p_draw.add_argument("--count", type=int, default=1, help="Types to draw (default 1; design allows raising to 2 later)")
    p_draw.add_argument("--interrupt-types", default=None, help="Comma-separated category numbers force-included on cascade interrupt (default: 2,5)")
    p_draw.add_argument("--interrupt-threshold", type=int, default=DEFAULT_INTERRUPT_THRESHOLD, help="Cascade-marker count that triggers the interrupt")
    p_draw.add_argument("--seed", type=int, default=None, help="Seed the RNG (testing/reproducibility only)")
    p_draw.add_argument("--dry-run", action="store_true", help="Compute and print the draw without persisting state")
    p_draw.set_defaults(func=cmd_draw)

    p_status = sub.add_parser("status", help="Print current lottery state (human/CI readable)")
    p_status.add_argument("--state", default=DEFAULT_STATE_PATH, help="Path to dream-lottery.json")
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
