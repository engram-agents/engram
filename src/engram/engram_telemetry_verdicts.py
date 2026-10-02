"""engram_telemetry_verdicts.py — §1 VERDICTS block, the READ side (issue
#3136, epic #2574 WS7).

Design doc: docs/2574-telemetry-design.md §1 (the "THE PARTICIPATION
DEFINITION" block). Per issue #3136: "The spec is §1 itself. Implement the
VERDICTS block as written and do not paraphrase it here or in code comments.
Quote its conditions or cite the section." `_VERDICT_CONDITIONS` below quotes
§1's own predicate block condition-for-condition (see that constant's own
comment); nothing in this module restates the design in prose.

This module builds the READ side only: `compute_verdicts()` reads the raw
`l0_records` rows a `records.db` connection already holds (WS1's schema —
`engram_telemetry_store.open_store()`/`ingest()` populate it; WS6, #3135,
schedules that ingest — this module reads whatever is already there,
regardless of whether a live daemon has run), joins each declared site's own
`reached` count via the site's DERIVED dispatch-target set, classifies each
site's window into one of §1's eight VERDICTS lines, and writes one row per
(window, site) to a `verdicts` table — healthy included, same `derive_checks`
append-only-report-not-gate shape `health/series.py` already uses (§2:
"adopted from health-v2 without redesign"). An empty `verdicts` table must
mean "nothing ran", never "nothing was wrong".

REVISION 2026-09-27 (issue #3136 D1 fix — see the issue's own "REVISION
2026-09-27" block, and `tools/telemetry_manifest_gate.py`'s module docstring
"DISPATCH MAP" section): this module ORIGINALLY imported
`tools.telemetry_manifest_gate.build_call_graph` / `derive_dispatch_targets`
/ `enclosing_key_for_site` to walk the backward call-graph AT RUNTIME. That
cannot work on an installed plugin — no `tiers.json` ships, and that module's
own path resolution overshoots the plugin root once the build flattens
`hooks/claude/*` to `hooks/*`. The fix: the walk happens ONCE, at
map-generation time (`tools/telemetry_manifest_gate.py --emit-map`), and
ships as the committed, convenience-tier data file
`telemetry_targets.json` (a sibling of this module, read via
`Path(__file__).with_name(...)`). This module now imports NOTHING from
`tools.telemetry_manifest_gate` — no gate import, no manifest read, no AST
walk, no path resolution outside its own directory, at runtime — which is
what lets it ship at convenience tier instead of dev (see
`src/build/packaging/tiers.json`'s own entry/note for this module).

Nothing in this module writes any `l0_records` row, ingests any JSONL file,
or schedules any daemon — WS6 (#3135) owns those. This module's own `main()`
CLI is a raw per-site table; `summary_line()` (WS8, issue #3137) is the
separate one-line rendering that engram_diagnose (engram_stats.py) and the
nightly dream record (agents/claude/engram-dream-master.md) put on their own
surfaces — see that function's own docstring/section comment, further down
in this file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from engram_telemetry_store import open_store

__all__ = ["compute_verdicts", "brute_force_verdicts", "summary_line"]

REACHED_EVENT_TYPE = "engram.telemetry.reached"

_WINDOW_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# §1 VERDICTS -- quoted condition-for-condition from
# docs/2574-telemetry-design.md §1's own predicate block (the "after round
# 10" version, brute-force-verified there: unmatched=0, multi-match=0 over
# {0,1,2}^5). Do not paraphrase; if that block ever changes, update the
# conditions below to match it line-for-line and re-run
# `brute_force_verdicts` (see this module's own contract tests).
#
#   let R = reached, D = delivered, X = dropped, N = no_emitter, I = init_failed
#   let thr = DEGRADED_THRESHOLD
#
#   R=0 and D=X=N=I=0                        -> NOT REACHED     <- NOT a fault
#   R=0 and (D>0 or X>0 or N>0 or I>0)       -> INCONSISTENT    <- store integrity
#   R>0 and I>0 and D=0                      -> INIT-FAILED     <- a real failure
#   R>0 and I=0 and D=0 and X=0 and N=0      -> GUARDED OUT     <- NOT a fault
#   R>0 and I=0 and D=0 and X=0 and N>0      -> UNWIRED-SILENT  <- not broken; Phase-4
#   R>0 and I=0 and D=0 and X>0              -> DARK            <- every attempt failed
#   R>0 and D>0 and X/(D+X) >  thr          -> DEGRADED
#   R>0 and D>0 and X/(D+X) <= thr          -> DELIVERING
#
#   FLAG (not a line): N>0 and D>0           -> PARTIAL
#   FLAG (not a line): I>0 and D>0           -> INIT-FLAPPING
#
# Each condition below is evaluated INDEPENDENTLY (never first-match/elif),
# so `_classify` and `brute_force_verdicts` share the exact same exhaustive/
# mutually-exclusive check -- the two cannot drift apart, and a runtime call
# that somehow matches zero or more-than-one line fails LOUD (ValueError)
# rather than silently picking a plausible-looking line.
# ---------------------------------------------------------------------------

_VERDICT_CONDITIONS: list[tuple[str, Any]] = [
    ("NOT REACHED", lambda R, D, X, N, I, thr: R == 0 and D == 0 and X == 0 and N == 0 and I == 0),
    ("INCONSISTENT", lambda R, D, X, N, I, thr: R == 0 and (D > 0 or X > 0 or N > 0 or I > 0)),
    ("INIT-FAILED", lambda R, D, X, N, I, thr: R > 0 and I > 0 and D == 0),
    ("GUARDED OUT", lambda R, D, X, N, I, thr: R > 0 and I == 0 and D == 0 and X == 0 and N == 0),
    ("UNWIRED-SILENT", lambda R, D, X, N, I, thr: R > 0 and I == 0 and D == 0 and X == 0 and N > 0),
    ("DARK", lambda R, D, X, N, I, thr: R > 0 and I == 0 and D == 0 and X > 0),
    ("DEGRADED", lambda R, D, X, N, I, thr: R > 0 and D > 0 and (X / (D + X)) > thr),
    ("DELIVERING", lambda R, D, X, N, I, thr: R > 0 and D > 0 and (X / (D + X)) <= thr),
]


def _verdict_matches(R: int, D: int, X: int, N: int, I: int, thr: float, conditions=None) -> list[str]:
    """Return every §1 VERDICTS line-name whose condition holds for this
    (R,D,X,N,I,thr) tuple -- evaluating every line independently, never
    first-match. `conditions` defaults to `_VERDICT_CONDITIONS`; a caller may
    pass a deliberately-mutated list (see `brute_force_verdicts`'s own
    docstring for why that matters for the can-it-fail control)."""
    conds = _VERDICT_CONDITIONS if conditions is None else conditions
    return [name for name, cond in conds if cond(R, D, X, N, I, thr)]


def _classify(R: int, D: int, X: int, N: int, I: int, thr: float) -> str:
    """§1's VERDICTS table, exhaustive and mutually exclusive by
    construction (see `_VERDICT_CONDITIONS`'s own docstring). Raises
    ValueError if the tuple matches zero or more than one line -- this
    should be unreachable given §1's brute-force verification
    (`brute_force_verdicts(thr)` returns unmatched=0, multi_match=0 over
    {0,1,2}^5), but a real (R,D,X,N,I) is not bounded to {0,1,2}; failing
    loud here rather than silently picking one match is the same fail-loud
    discipline this whole design is about, one level down."""
    matches = _verdict_matches(R, D, X, N, I, thr)
    if len(matches) != 1:
        shape = "unmatched" if not matches else f"multi-matched {matches}"
        raise ValueError(
            f"§1 VERDICTS table {shape} for (R={R}, D={D}, X={X}, N={N}, I={I}, thr={thr}) "
            f"-- see docs/2574-telemetry-design.md §1's own brute-force block and "
            f"this module's `brute_force_verdicts`"
        )
    return matches[0]


def brute_force_verdicts(thr: float, domain=(0, 1, 2), conditions=None) -> dict:
    """§12's brute-force control (docs/2574-telemetry-design.md §1's own
    "THIS LIST IS NOT REVIEWED AS PROSE ANY MORE" block), reusable rather
    than a one-off script: range every (R,D,X,N,I) over `domain`^5 (243
    states for the doc's own {0,1,2} domain) and report `unmatched` (zero
    lines matched) and `multi_match` (more than one line matched) counts,
    plus the first offending tuple of each kind for a human to look at. Both
    counts MUST be 0 for the VERDICTS block, as written, to be exhaustive and
    mutually exclusive -- report BOTH numbers always (§1: "`unmatched` alone
    cannot test the mutual exclusivity VERDICTS asserts").

    `conditions`, if given, overrides `_VERDICT_CONDITIONS` -- this is what
    lets a test re-run this SAME enumeration against a deliberately-mutated
    condition list (e.g. restoring `and I=0` to the DEGRADED line, per §1's
    own can-it-fail control, which the design doc states reproduces
    unmatched=12) without touching the real module state.
    """
    unmatched = 0
    multi_match = 0
    first_unmatched = None
    first_multi_match = None
    total = 0
    for R in domain:
        for D in domain:
            for X in domain:
                for N in domain:
                    for I in domain:
                        total += 1
                        matches = _verdict_matches(R, D, X, N, I, thr, conditions=conditions)
                        if len(matches) == 0:
                            unmatched += 1
                            if first_unmatched is None:
                                first_unmatched = (R, D, X, N, I)
                        elif len(matches) > 1:
                            multi_match += 1
                            if first_multi_match is None:
                                first_multi_match = ((R, D, X, N, I), tuple(matches))
    return {
        "total": total,
        "unmatched": unmatched,
        "multi_match": multi_match,
        "first_unmatched": first_unmatched,
        "first_multi_match": first_multi_match,
    }


# ---------------------------------------------------------------------------
# verdicts table -- the derive_checks shape (health/series.py's own §2
# precedent): append-only, a row written on EVERY compute_verdicts() call
# for every declared site, healthy included. No UNIQUE constraint on
# (window, site) -- re-running for the same window is legitimate (a later
# run after more ingest, or a deliberate re-check) and simply appends a
# fresh observation, exactly like derive_checks's own plain INSERT.
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    window        TEXT NOT NULL,
    site          TEXT NOT NULL,
    verdict       TEXT NOT NULL,
    r             INTEGER NOT NULL,
    d             INTEGER NOT NULL,
    x             INTEGER NOT NULL,
    n             INTEGER NOT NULL,
    i             INTEGER NOT NULL,
    unparseable   INTEGER NOT NULL DEFAULT 0,
    flags_json    TEXT NOT NULL,
    computed_at   TEXT NOT NULL
);
"""


def _ensure_verdicts_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _now_utc_iso(now: Optional[datetime] = None) -> str:
    """UTC, ISO-8601, explicit 'Z' -- same rendering shape as
    engram_telemetry_store._now_utc_iso / engram_log_emitter._emit_inner
    (design doc §7: local-time timestamps are the originating incident this
    whole codebase now guards against at every write site). Reimplemented
    locally (not imported) so this module stays as self-contained as
    engram_telemetry_store.py itself is about its own UTC formatting."""
    dt = now if now is not None else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _validate_window(window: Any) -> None:
    """§1's WINDOW definition: "a window is a UTC calendar day". Reject
    anything that is not literally a 'YYYY-MM-DD' string -- a malformed
    window silently matching zero rows (rather than raising) would read as
    a false NOT REACHED/INCONSISTENT for every declared site, which is
    exactly the silent-corruption failure mode this whole design exists to
    prevent, one level up in the caller's own input.

    Uses `fullmatch`, not `match` (issue #3136 D4): `_WINDOW_RE.match` with
    its own `^...$` anchors still accepts a trailing newline (e.g.
    "2026-09-25\\n") because `$` matches just before a trailing '\\n', not
    only at the true end of string -- `re.compile(r'^\\d{4}-\\d{2}-\\d{2}$').match('2026-09-25\\n')`
    is True, `.fullmatch(...)` is False. `fullmatch` has no such gap."""
    if not isinstance(window, str) or not _WINDOW_RE.fullmatch(window):
        raise ValueError(f"window must be a UTC calendar day 'YYYY-MM-DD' string, got {window!r}")


# ---------------------------------------------------------------------------
# R -- reached, joined via the site's own DERIVED dispatch-target set (§1:
# "R := max, over the site's derived dispatch-target set, of that target's
# reached count in the window"). Issue #3136 D1 fix: the derived-target set
# is a plain LOOKUP into the pre-built `targets_map["sites"]` dict (built
# once, at map-generation time, by tools/telemetry_manifest_gate.py's
# `build_dispatch_map`/`--emit-map` and committed as `telemetry_targets.json`
# -- see this module's own docstring) -- never a live call-graph walk.
# ---------------------------------------------------------------------------


def _reached_count(conn: sqlite3.Connection, target_id: str, window: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM l0_records WHERE manifest_id = ? AND event_type = ? AND substr(ts_utc, 1, 10) = ?",
        (target_id, REACHED_EVENT_TYPE, window),
    ).fetchone()
    return row[0] if row else 0


def _compute_R(targets_map: dict, conn: sqlite3.Connection, site_literal: str, window: str) -> int:
    """R := max over the site's derived dispatch-target set of that target's
    reached count in the window (§1). An EMPTY target list (no dispatch
    target derivable for this site -- e.g. a declared site whose static
    join-key check, tools.telemetry_manifest_gate.reached_literal_mismatches,
    is failing) yields R=0, matching §1's own stated fallback: no evidence of
    reach exists, so this window cannot claim more than
    NOT REACHED/INCONSISTENT for this site.

    Named/kept as its own top-level function (rather than inlined into
    `compute_verdicts`'s loop) specifically so a test can monkeypatch it to
    run §1's own "mutation control" -- replacing the map-lookup join with a
    naive 'any record for this site literal exists' query -- against the
    SAME `compute_verdicts` call, per issue #3136's contract tests."""
    target_ids = targets_map["sites"].get(site_literal, [])
    if not target_ids:
        return 0
    return max(_reached_count(conn, tid, window) for tid in target_ids)


# ---------------------------------------------------------------------------
# D/X/N/I -- outcome counts, read from the site's OWN outcome rows
# (manifest_id = the site literal itself, per the real call-site shape:
# write_telemetry_record(manifest_id=<site>, event_type=<the site's own
# domain event, e.g. "engram.surface.fire">, fields={"outcome": ...})).
# The manifest does not declare each site's own domain event_type (only
# {site, trigger}), so outcome rows are filtered by manifest_id alone and
# bucketed by their own fields_json["outcome"] value -- aggregated in
# Python, not via SQL json_extract, matching engram_stats.py's own stated
# precedent ("aggregate in Python to avoid fragile json_extract across
# SQLite versions").
# ---------------------------------------------------------------------------

_OUTCOME_KEYS = ("delivered", "dropped", "no_emitter", "init_failed")


def _outcome_counts(conn: sqlite3.Connection, site_literal: str, window: str) -> tuple:
    """Return ``(counts, unparseable)`` -- `counts` is ``{outcome_key: N}``
    over the 4 known `_OUTCOME_KEYS`; `unparseable` is the count of rows that
    either fail `json.loads`, parse to something other than a dict, or carry
    an `outcome` value outside the 4 known keys (issue #3136 D3: these rows
    were previously silently dropped via a bare `continue`/no-match, which
    could hide a DARK or DELIVERING site as a false GUARDED OUT -- see
    `compute_verdicts`'s own UNPARSEABLE flag, which surfaces this count
    without touching `_VERDICT_CONDITIONS`/the brute-forced predicate table
    at all)."""
    counts = {k: 0 for k in _OUTCOME_KEYS}
    unparseable = 0
    cur = conn.execute(
        "SELECT fields_json FROM l0_records WHERE manifest_id = ? AND substr(ts_utc, 1, 10) = ?",
        (site_literal, window),
    )
    for (fields_json,) in cur.fetchall():
        try:
            fields = json.loads(fields_json) if fields_json else {}
        except (TypeError, ValueError):
            unparseable += 1
            continue
        if not isinstance(fields, dict):
            unparseable += 1
            continue
        outcome = fields.get("outcome")
        if outcome in counts:
            counts[outcome] += 1
        else:
            unparseable += 1
    return counts, unparseable


# ---------------------------------------------------------------------------
# §1 RUNTIME clause -- issue #3136 D2 (revised shape, issue's own "REVISION
# 2026-09-27" item 5): "an increment naming a target outside the walk's
# derived set is an INCONSISTENT row rather than a silent adjustment." A
# `reached` row whose `manifest_id` is not a target anywhere in the map, or
# an outcome row whose `manifest_id` is not a declared site, gets its OWN
# row in the same returned list / same `verdicts` table: `site` = that id,
# `verdict` = "INCONSISTENT", `flags` = ["UNKNOWN-ID"]. Deployed code older
# or newer than the committed map produces exactly these rows -- a version
# skew between what ships and what is declared, made loud instead of
# silently dropped.
# ---------------------------------------------------------------------------

_UNKNOWN_ID_VERDICT = "INCONSISTENT"
_UNKNOWN_ID_FLAG = "UNKNOWN-ID"


def _reached_ids_seen(conn: sqlite3.Connection, window: str) -> dict:
    """{manifest_id: count} for every DISTINCT manifest_id among
    REACHED_EVENT_TYPE rows in `window`."""
    cur = conn.execute(
        "SELECT manifest_id, COUNT(*) FROM l0_records "
        "WHERE event_type = ? AND substr(ts_utc, 1, 10) = ? GROUP BY manifest_id",
        (REACHED_EVENT_TYPE, window),
    )
    return {manifest_id: count for manifest_id, count in cur.fetchall()}


def _outcome_writer_ids_seen(conn: sqlite3.Connection, window: str) -> dict:
    """{manifest_id: count} for every DISTINCT manifest_id among rows in
    `window` whose `fields_json` carries an "outcome" key at all (any of the
    4 known values, or an unparseable one -- the writer's OWN identity is
    what §1's RUNTIME clause cares about here, not whether its outcome value
    happens to be valid; a row failing json.loads entirely cannot name its
    own writer and is excluded, same as `_outcome_counts`'s own handling)."""
    cur = conn.execute(
        "SELECT manifest_id, fields_json FROM l0_records WHERE substr(ts_utc, 1, 10) = ?",
        (window,),
    )
    counts: dict = {}
    for manifest_id, fields_json in cur.fetchall():
        try:
            fields = json.loads(fields_json) if fields_json else {}
        except (TypeError, ValueError):
            continue
        if isinstance(fields, dict) and "outcome" in fields:
            counts[manifest_id] = counts.get(manifest_id, 0) + 1
    return counts


def _known_target_ids(targets_map: dict) -> set:
    """Union, over every declared site's own target-id list, of all target
    ids the map names -- the "known-good reached-target universe" §1's
    RUNTIME clause checks a `reached` row's manifest_id against."""
    out: set = set()
    for ids in targets_map["sites"].values():
        out.update(ids)
    return out


def _unknown_id_row(window: str, manifest_id: str, count: int) -> dict:
    return {
        "window": window,
        "site": manifest_id,
        "verdict": _UNKNOWN_ID_VERDICT,
        "R": count,
        "D": 0,
        "X": 0,
        "N": 0,
        "I": 0,
        "unparseable": 0,
        "flags": [_UNKNOWN_ID_FLAG],
    }


# ---------------------------------------------------------------------------
# compute_verdicts -- the public entry point (issue #3136's own signature,
# REVISED 2026-09-27: `compute_verdicts(conn, targets_map, window)` -- no
# `manifest`, no `src_root`; see module docstring)
# ---------------------------------------------------------------------------


def compute_verdicts(conn: sqlite3.Connection, targets_map: dict, window: str) -> list:
    """Compute and persist §1's VERDICTS for every declared site, for one
    UTC calendar-day `window`. Returns the list of computed rows (also
    written to `records.db`'s `verdicts` table -- see module docstring for
    the append-only, healthy-included persistence contract).

    Each returned row: ``{"window": ..., "site": ..., "verdict": ...,
    "R": ..., "D": ..., "X": ..., "N": ..., "I": ..., "unparseable": ...,
    "flags": [...]}``. In addition to one row per declared site, §1's
    RUNTIME clause (issue #3136 D2) adds one further row per orphaned id
    encountered in the window -- see `_unknown_id_row`.

    Args:
        conn: an open sqlite3 connection to `records.db` (e.g.
            ``engram_telemetry_store.open_store(db_path)``) -- must already
            hold WS1's `l0_records` schema; this function creates its own
            `verdicts` table idempotently if absent.
        targets_map: the loaded `telemetry_targets.json` dict (or an
            equivalent test fixture carrying the same ``{"degraded_threshold":
            ..., "sites": {site: [target_id, ...]}}`` shape) -- built once by
            `tools.telemetry_manifest_gate.build_dispatch_map`/`--emit-map`.
            This function never re-derives it and never imports the gate
            module that builds it.
        window: a UTC calendar day, ``"YYYY-MM-DD"``.
    """
    _validate_window(window)
    _ensure_verdicts_table(conn)
    thr = targets_map["degraded_threshold"]  # KeyError if missing -- fail loud (issue #3136 D5)
    sites_map = targets_map["sites"]
    computed_at = _now_utc_iso()

    rows: list = []
    for site in sorted(sites_map.keys()):
        R = _compute_R(targets_map, conn, site, window)
        outcomes, unparseable = _outcome_counts(conn, site, window)
        D, X, N, I = outcomes["delivered"], outcomes["dropped"], outcomes["no_emitter"], outcomes["init_failed"]
        verdict = _classify(R, D, X, N, I, thr)

        flags: list = []
        if N > 0 and D > 0:
            flags.append("PARTIAL")
        if I > 0 and D > 0:
            flags.append("INIT-FLAPPING")
        if unparseable > 0:
            flags.append("UNPARSEABLE")

        row = {
            "window": window,
            "site": site,
            "verdict": verdict,
            "R": R,
            "D": D,
            "X": X,
            "N": N,
            "I": I,
            "unparseable": unparseable,
            "flags": flags,
        }
        rows.append(row)

        conn.execute(
            "INSERT INTO verdicts (window, site, verdict, r, d, x, n, i, unparseable, flags_json, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (window, site, verdict, R, D, X, N, I, unparseable, json.dumps(flags), computed_at),
        )

    # §1 RUNTIME clause (issue #3136 D2) -- orphaned ids, in both directions.
    known_target_ids = _known_target_ids(targets_map)
    declared_sites = set(sites_map.keys())

    for manifest_id, count in sorted(_reached_ids_seen(conn, window).items()):
        if manifest_id not in known_target_ids:
            row = _unknown_id_row(window, manifest_id, count)
            rows.append(row)
            conn.execute(
                "INSERT INTO verdicts (window, site, verdict, r, d, x, n, i, unparseable, flags_json, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (window, manifest_id, row["verdict"], row["R"], row["D"], row["X"], row["N"], row["I"],
                 row["unparseable"], json.dumps(row["flags"]), computed_at),
            )

    for manifest_id, count in sorted(_outcome_writer_ids_seen(conn, window).items()):
        if manifest_id not in declared_sites:
            row = _unknown_id_row(window, manifest_id, count)
            rows.append(row)
            conn.execute(
                "INSERT INTO verdicts (window, site, verdict, r, d, x, n, i, unparseable, flags_json, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (window, manifest_id, row["verdict"], row["R"], row["D"], row["X"], row["N"], row["I"],
                 row["unparseable"], json.dumps(row["flags"]), computed_at),
            )

    conn.commit()
    return rows


# ---------------------------------------------------------------------------
# summary_line -- WS8 (issue #3137, epic #2574 phase 2): the ONE summary line
# rendered onto engram_diagnose's output (engram_stats.py) and the nightly
# dream record. compute_verdicts()/the CLI above are a raw per-site table;
# nobody reading engram_diagnose or the dream record opens records.db
# directly, so this is the one line that surfaces telemetry health on the
# two surfaces people already read.
#
# THE ONE INVARIANT THIS UNIT EXISTS FOR: "we could not check" must NEVER
# render as "everything is healthy". A missing records.db, a missing
# verdicts table, a stale ingest, or zero rows for the requested window must
# ALL render as a "not aggregated" line -- never a line of zero counts,
# which would be indistinguishable from a genuinely healthy, fully-checked
# window. See this module's own contract tests
# (tests/test_telemetry_ws8_summary_line.py) for the C2 class that pins this
# down directly.
#
# The 7 named counts in the rendered line use their STORED verdict-column
# values character-for-character (issue #3137's own amendment, mid-review:
# an earlier draft of the issue showed a display-only rename of
# UNWIRED-SILENT to "NOT WIRED" in its example line -- that was the issue
# author's own typo/slip, not a design decision; no renaming happens here).
# `DELIVERING` (the fully-healthy verdict) is not one of the 7 named counts
# but still contributes to the `<N> sites` total.
# ---------------------------------------------------------------------------

_SUMMARY_NAMED_VERDICTS: list[str] = [
    "DARK",
    "DEGRADED",
    "INIT-FAILED",
    "INCONSISTENT",
    "NOT REACHED",
    "GUARDED OUT",
    "UNWIRED-SILENT",
]

_SUMMARY_NOT_AGGREGATED_MISSING = "telemetry: not aggregated (records.db missing)"
_SUMMARY_NOT_AGGREGATED_NO_INGEST_TIMESTAMP = (
    "telemetry: not aggregated (ingest timestamp unavailable)"
)

_SUMMARY_STALE_INGEST_DAYS = 2


def _summary_table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _summary_parse_iso_utc(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp in the shape `_now_utc_iso` writes
    (e.g. "2026-09-25T10:00:00.000Z"). Returns None (never raises) on
    anything unparseable -- an unparseable stored timestamp is treated the
    same as "no timestamp available" by the caller (`summary_line`), per
    this function's own docstring contract: a corrupt/foreign timestamp
    string in `ingest_offsets` must not crash the summary line, and must
    not be silently treated as "fresh" either."""
    if not isinstance(value, str) or not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def summary_line(db_path, window: Optional[str] = None) -> str:
    """Render the ONE telemetry summary line for `db_path`'s `verdicts`
    table -- issue #3137 (WS8, epic #2574 phase 2).

    Opens `db_path` read-only (never writes, never creates). Resolves
    `window` to the most recent window present when not given explicitly
    (a caller-supplied `window` that fails `_validate_window` raises
    ValueError -- a caller bug, not a data-quality question this function
    answers). Cross-checks staleness against `ingest_offsets` (WS6's own
    ingest-timestamp table, populated independently of `compute_verdicts`):
    a store whose most recent ingest is more than
    `_SUMMARY_STALE_INGEST_DAYS` days old, or that carries zero `verdicts`
    rows for the resolved window specifically, is reported as
    "not aggregated" rather than rendered as a (misleadingly healthy) line
    of zero counts.

    Never writes anything; always closes the connection it opens
    (try/finally). May raise (a malformed `window`, or an unexpected sqlite
    error) -- callers that need a never-raises guarantee wrap the call
    themselves (see engram_stats.py's `_diagnose_impl`, which converts any
    exception here into a `"telemetry: unavailable (...)"` string).
    """
    conn: Optional[sqlite3.Connection] = None
    try:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            return _SUMMARY_NOT_AGGREGATED_MISSING

        if not _summary_table_exists(conn, "verdicts"):
            return _SUMMARY_NOT_AGGREGATED_MISSING

        if window is not None:
            _validate_window(window)  # ValueError propagates -- caller bug
            resolved_window = window
        else:
            row = conn.execute("SELECT MAX(window) FROM verdicts").fetchone()
            resolved_window = row[0] if row else None
            if resolved_window is None:
                # Table exists but carries zero rows at all -- fresh/empty
                # store, same as "nothing has aggregated yet".
                return _SUMMARY_NOT_AGGREGATED_MISSING

        last_ingest_raw: Optional[str] = None
        if _summary_table_exists(conn, "ingest_offsets"):
            row = conn.execute("SELECT MAX(last_ingested_at) FROM ingest_offsets").fetchone()
            last_ingest_raw = row[0] if row else None

        if not last_ingest_raw:
            # No ingest_offsets table, or an empty one -- "no timestamp
            # available". Reviewer-fairy round 1 (PR-3170) caught that this
            # used to reuse _SUMMARY_NOT_AGGREGATED_MISSING, which reads
            # "records.db missing" even when records.db and verdicts both
            # genuinely exist with healthy data -- misleading about which
            # table is actually the problem, even though it never produces a
            # fake-healthy line either way. A distinct message names the
            # real cause.
            return _SUMMARY_NOT_AGGREGATED_NO_INGEST_TIMESTAMP

        last_ingest_dt = _summary_parse_iso_utc(last_ingest_raw)
        if last_ingest_dt is None:
            # Stored timestamp exists but is not parseable -- same "ingest
            # timestamp unavailable" case as a missing one, not a missing
            # records.db.
            return _SUMMARY_NOT_AGGREGATED_NO_INGEST_TIMESTAMP

        window_count_row = conn.execute(
            "SELECT COUNT(*) FROM verdicts WHERE window = ?", (resolved_window,)
        ).fetchone()
        window_count = window_count_row[0] if window_count_row else 0

        is_stale = (datetime.now(timezone.utc) - last_ingest_dt) > timedelta(days=_SUMMARY_STALE_INGEST_DAYS)
        if is_stale or window_count == 0:
            return f"telemetry: not aggregated (last ingest {last_ingest_raw})"

        counts = {name: 0 for name in _SUMMARY_NAMED_VERDICTS}
        unrecognized: list[tuple[str, int]] = []
        cur = conn.execute(
            "SELECT verdict, COUNT(*) as c FROM verdicts WHERE window = ? GROUP BY verdict",
            (resolved_window,),
        )
        for verdict, c in cur.fetchall():
            if verdict in counts:
                counts[verdict] = c
            elif verdict != "DELIVERING":
                # Design-conformance read (PR-3170, Borges): a verdict that
                # is neither one of the named 7 nor the fully-healthy
                # DELIVERING is exactly the case _SUMMARY_NAMED_VERDICTS
                # drifting out of sync with WS7's _VERDICT_CONDITIONS would
                # produce -- a NEW fault verdict silently counted into
                # `window_count` but rendered nowhere. Render it too, so an
                # unpinned drift shows up on the line instead of
                # disappearing into the total.
                unrecognized.append((verdict, c))
            # DELIVERING itself (fully healthy) still counted toward
            # window_count above via the separate COUNT(*) query, by design.

        breakdown = " · ".join(f"{name} {counts[name]}" for name in _SUMMARY_NAMED_VERDICTS)
        for verdict, c in sorted(unrecognized):
            breakdown += f" · {verdict} {c}"
        return f"telemetry {resolved_window}: {window_count} sites · {breakdown}"
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# CLI -- python -m engram_telemetry_verdicts [--window YYYY-MM-DD] [--db PATH]
#        [--targets-map PATH]
# Exits 0 always: it's a report, not a gate (§8: telemetry gates nothing T1).
# The CLI above is a raw per-site table for a human/operator to read
# directly; `summary_line` (just above) is the one-line rendering WS8 puts
# onto engram_diagnose's output and the nightly dream record.
# ---------------------------------------------------------------------------


def _default_data_dir() -> Path:
    """Resolve DATA_DIR the same way engram_telemetry_store.py does -- read
    ENGRAM_HOME directly, no engram_core import (this module stays outside
    the #2652 one-owner boundary telemetry is a stated exception to, same as
    its sibling store module)."""
    return Path(os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram"))


def _default_db_path() -> Path:
    return _default_data_dir() / "telemetry" / "records.db"


def _default_targets_map_path() -> Path:
    """The committed map ships as a sibling of this module -- on a real
    plugin install that means alongside `server.py`/`platform.json` at the
    bundle root; in source, alongside this file in `src/engram/`. Resolved
    via `Path(__file__).with_name(...)` (issue #3136 D1: never `parents[N]`,
    never anything under `src/` -- see module docstring)."""
    return Path(__file__).with_name("telemetry_targets.json")


def _load_targets_map(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(
            f"telemetry targets map not found at {path} -- this is a packaging "
            f"invariant, not a user-facing 'not aggregated' case (that's a "
            f"different failure -- see WS8/#3137's own reporting of missing/stale "
            f"records.db data). telemetry_targets.json must ship alongside "
            f"engram_telemetry_verdicts.py; see tools/telemetry_manifest_gate.py's "
            f"--emit-map flag to (re)generate it."
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _print_report(window: str, rows: list) -> None:
    """Print the table, non-healthy rows first (issue #3136's own CLI
    spec). "Healthy" here is read narrowly as `DELIVERING` -- the one
    verdict §1 marks with no "NOT a fault"/"real failure"/"Phase-4" caveat
    at all -- so every OTHER verdict (including the explicitly-"NOT a
    fault" NOT REACHED/GUARDED OUT lines, which are still worth a human's
    attention before the unremarkable DELIVERING rows) sorts first, in
    manifest-declaration order; DELIVERING rows sort after, in the same
    relative order. This ordering is a CLI-formatting judgment call the
    issue left to the implementer (§1/the issue do not define "healthy");
    flagged as such in this PR's own handoff."""
    if not rows:
        print(f"engram-telemetry-verdicts: window {window} -- no declared sites.")
        return
    non_healthy = [r for r in rows if r["verdict"] != "DELIVERING"]
    healthy = [r for r in rows if r["verdict"] == "DELIVERING"]
    ordered = non_healthy + healthy

    print(f"engram-telemetry-verdicts: window {window} ({len(rows)} declared site(s))")
    header = f"{'SITE':<42} {'VERDICT':<16} {'R':>5} {'D':>5} {'X':>5} {'N':>5} {'I':>5}  FLAGS"
    print(header)
    for r in ordered:
        flags_str = ",".join(r["flags"]) if r["flags"] else "-"
        print(
            f"{r['site']:<42} {r['verdict']:<16} {r['R']:>5} {r['D']:>5} {r['X']:>5} {r['N']:>5} {r['I']:>5}  {flags_str}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="§1 VERDICTS runtime report (issue #3136, epic #2574 WS7)")
    ap.add_argument("--window", default=None, help="UTC calendar day YYYY-MM-DD (default: today, UTC)")
    ap.add_argument("--db", default=None, help="path to records.db (default: $ENGRAM_HOME/telemetry/records.db)")
    ap.add_argument("--targets-map", default=None,
                     help="path to telemetry_targets.json (default: alongside this file, as shipped)")
    args = ap.parse_args()

    window = args.window or _today_utc()
    db_path = Path(args.db) if args.db else _default_db_path()
    targets_map_path = Path(args.targets_map) if args.targets_map else _default_targets_map_path()

    targets_map = _load_targets_map(targets_map_path)

    conn = open_store(db_path)
    try:
        rows = compute_verdicts(conn, targets_map, window)
    finally:
        conn.close()

    _print_report(window, rows)
    return 0  # a report, never a gate (§8) -- exits 0 regardless of verdict content


if __name__ == "__main__":
    sys.exit(main())
