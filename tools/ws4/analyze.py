#!/usr/bin/env python3
"""WS4 adjudication kit, Piece 3 (engram-alpha#2753) -- the pre-registered
analysis: O1 (walked-past share, arm difference only), a length control, and
O2 (post-fire ENGRAM-citation counts, raw vs transfer).

verdicts.jsonl format (Piece 2 -- written by the human adjudicator, one line
per fire, keyed by the fire_id from extract_spans.py's spans/*.md headers
(spans are numbered "[call 1]", "[call 2]", ... so the adjudicator can name
a specific call). NEVER contains arm information -- the adjudicator only
ever sees fire_id):

    {"fire_id": "fire-...", "adjudicator": "<name>",
     "verdict": "heeded" | "walked_past" | "unclear",
     "corrective_at": <int >= 1> | null, "note": "<free text>"}

corrective_at (round 2, item 1) is the 1-based "[call N]" index of the call
where the corrective act happens. Required and >= 1 when verdict=="heeded";
null for every other verdict. load_verdicts() validates this on every row
and fails loud (raises ValueError naming the fire_id) on a violation.

drops.jsonl format (Amendment 2 / round 2, item 2 -- written by a seat's own
privacy screener from the span files and their fire ids; NEVER sees or
carries the arm):

    {"fire_id": "fire-...", "reason": "dropped_privacy"}

Pre-registration rules enforced here:
  - O1 never prints an absolute walked-past rate as a headline -- only the
    arm DIFFERENCE (chain_share - control_share).
  - Per-lesson counting on multi-lesson fires: a fire with N shown lessons
    contributes N counting units (see tools/ws4/ledger.py's
    expand_per_lesson), each carrying just that lesson's own heads.
  - Denominators (fires by arm, unlocatable, unclear, dropped_privacy,
    ledger_rows_by_arm, error_rows) are reported alongside every table.
    ledger_rows_by_arm counts every RAW ledger row (chain, control, na,
    error, and any missing/non-string arm as "missing") -- including the
    na/error rows that never get a key.jsonl entry and so never appear in
    any chain/control table. error_rows separately surfaces the count and
    per-error-string breakdown of PR #3173's `error` rows, so an arm-draw
    or row-construction failure stays VISIBLE in the denominator instead
    of silently vanishing.
  - Fires flagged by the leak audit (head_named_in_span) are excluded from
    the main tally and reported as a SEPARATE row, never silently mixed in.
    Same treatment for fires dropped by a seat's privacy screener
    (drops.jsonl) -- excluded from O1/O2, counted per arm (Amendment 2).

Amendment 3 (#2753, Kepler's ruling 2026-09-27 18:47Z): the report states the
sampled fraction per seat, and the per-lesson composition of each seat's
sample (so a two-lesson seat is not read as a fleet result). This reads
extract_spans.py's own sample_summary.json (date, sample_n, sample_seed,
eligible, sampled, per_lesson), one file per UTC day, via --sample-summary
(a glob per seat, repeatable, paired with --seat like --ledger/--verdicts/
--key) or its explicit opt-out --no-sample-summary. Never a walked-past
rate -- counts and fractions of the SAMPLE only.

O1's "per K in K_SWEEP ...; report sign stability across K" (round 2, item 1)
measures the SAME outcome (heeded vs walked past) at every K, from the
verdicts.jsonl's own corrective_at field -- never round 1's proxy
(compute_o1_k_sweep re-cutting the window to see whether it closes on an
outbound act before K calls), which measured a different quantity and is
deleted here, not kept under another name. A fire counts as heeded AT K iff
verdict=="heeded" and corrective_at<=K; otherwise it is walked_past at K.
The headline reported by main() is K_PRIMARY; the full sweep + whether the
arm-difference sign holds across K is reported alongside it.

Read-only: never writes to the ledger, to knowledge.db, or to any
adjudicator-facing file. Stdlib only.

Usage:
    python -m tools.ws4.analyze --ledger LEDGER --verdicts VERDICTS --key KEY \\
        --flags FLAGS --sample-summary "out/seatA/*/sample_summary.json" \\
        --db knowledge.db --seat SEAT-A \\
        [--drops DROPS] [--unlocatable OUT/unlocatable.jsonl]
    # or, explicitly opting out of the leak audit (round 2, item 3):
    python -m tools.ws4.analyze --no-flags --ledger LEDGER --verdicts VERDICTS \\
        --key KEY --sample-summary "out/seatA/*/sample_summary.json" --seat SEAT-A
    # or, explicitly opting out of the sample report (amendment 3):
    python -m tools.ws4.analyze --no-sample-summary --ledger LEDGER --verdicts VERDICTS \\
        --key KEY --flags FLAGS --seat SEAT-A

    # multiple seats, pooled + per-seat:
    python -m tools.ws4.analyze --seat SEAT-A --ledger L1 --verdicts V1 --key K1 --flags F1 \\
                                 --sample-summary "out/seatA/*/sample_summary.json" \\
                                 --seat SEAT-B --ledger L2 --verdicts V2 --key K2 --flags F2 \\
                                 --sample-summary "out/seatB/*/sample_summary.json" \\
                                 --db knowledge.db
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

from .constants import K_PRIMARY, K_SWEEP, is_node_creating_engram_call
from .hook_bridge import build_match_target, load_situation_patterns, situation_pattern_matches
from .ledger import compute_fire_id, expand_per_lesson, load_jsonl
from .transcript import (
    TranscriptReadError,
    find_fired_tool_use,
    get_message_content,
    is_sidechain,
    load_transcript,
    o2_scan_end,
)

VERDICT_VALUES = ("heeded", "walked_past", "unclear")
ARMS = ("chain", "control")


def load_key(path) -> dict:
    return {row["fire_id"]: row for row in load_jsonl(path)}


def _validate_corrective_at(row: dict) -> None:
    """Round 2, item 1's verdicts.jsonl contract: corrective_at is required
    and >= 1 when verdict=="heeded", and must be null otherwise. Fails
    loud (raises ValueError naming the fire_id) on a violation -- this is
    a one-time schema guard on the raw file contents, independent of which
    K a caller later evaluates heeded-at-K against."""
    fire_id = row.get("fire_id")
    verdict = row.get("verdict")
    corrective_at = row.get("corrective_at")
    is_valid_int = isinstance(corrective_at, int) and not isinstance(corrective_at, bool)
    if verdict == "heeded":
        if not (is_valid_int and corrective_at >= 1):
            raise ValueError(
                f"verdicts.jsonl violation for fire_id={fire_id!r}: verdict=='heeded' requires "
                f"corrective_at to be an int >= 1, got {corrective_at!r}"
            )
    else:
        if corrective_at is not None:
            raise ValueError(
                f"verdicts.jsonl violation for fire_id={fire_id!r}: verdict={verdict!r} requires "
                f"corrective_at to be null, got {corrective_at!r}"
            )


def load_verdicts(path) -> dict:
    """fire_id -> verdict row. If a fire_id appears more than once (e.g. a
    re-adjudication pass), the LAST entry wins -- see handoff's
    ambiguous-decisions log; the spec doesn't describe multi-adjudicator
    aggregation. Every row is validated via _validate_corrective_at as it
    is read (fails loud, naming the fire_id, on a violation)."""
    verdicts = {}
    for row in load_jsonl(path):
        _validate_corrective_at(row)
        verdicts[row["fire_id"]] = row
    return verdicts


def load_flags(path) -> dict:
    """Optional analyst-only sidecar written by extract_spans.py
    (flags.jsonl) -- fire_id -> {"head_named_in_span": bool, ...}. Missing
    file -> empty dict (leak-audit reporting degrades to "not available",
    never silently assumes unflagged)."""
    if path is None or not Path(path).exists():
        return {}
    return {row["fire_id"]: row for row in load_jsonl(path)}


def load_drops(path) -> dict:
    """Amendment 2 / round 2 item 2: a seat's own privacy-screener sidecar
    -- fire_id -> {"fire_id": ..., "reason": "dropped_privacy"}. Never
    carries the arm; analyze.py does the arm join via the seat's own
    key.jsonl. Missing file -> empty dict (no screening for this seat)."""
    if path is None or not Path(path).exists():
        return {}
    return {row["fire_id"]: row for row in load_jsonl(path)}


def load_sample_summaries(pattern: str, seat: str) -> list:
    """Amendment 3 (#2753): expands one --sample-summary glob (one pattern
    per --seat, e.g. "out/seatA/*/sample_summary.json" -- one file per UTC
    day) and parses each matched file as extract_spans.py's
    sample_summary.json (date, sample_n, sample_seed, eligible, sampled,
    per_lesson). A pattern matching zero files is a fail-loud error naming
    the seat -- an empty match is far more likely a mistyped path or date
    range than "no data for this seat" and must never be silently treated
    as zero days."""
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise ValueError(f"--sample-summary glob matched no files for seat {seat!r}: {pattern!r}")
    summaries = []
    for match in matches:
        with open(match, "r", encoding="utf-8") as f:
            summaries.append(json.load(f))
    return summaries


class SeatInputs:
    def __init__(self, seat, ledger_rows, key, verdicts, flags, drops=None, sample_summaries=None):
        self.seat = seat
        self.ledger_rows = ledger_rows
        self.key = key
        self.verdicts = verdicts
        self.flags = flags
        self.drops = drops if drops is not None else {}
        self.sample_summaries = sample_summaries if sample_summaries is not None else []


def _transcript_cache_get(cache: dict, path: str):
    if path in cache:
        return cache[path]
    try:
        records = load_transcript(path)
    except TranscriptReadError:
        records = None
    cache[path] = records
    return records


def _is_flagged(flags: dict, fire_id: str) -> bool:
    entry = flags.get(fire_id)
    return bool(entry and entry.get("head_named_in_span"))


def _is_dropped(drops: dict, fire_id: str) -> bool:
    """Amendment 2 / round 2 item 2: True if a seat's own privacy screener
    dropped this fire (drops.jsonl). Checked BEFORE the leak-audit flag in
    every table below, so a dropped fire is counted once, as
    dropped_privacy, never double-counted as flagged too."""
    return fire_id in drops


def _o1_outcome_bucket(verdict_row, k: int) -> str:
    """Shared O1 outcome logic (round 3, item 3): a fire counts as heeded
    AT k iff verdict=="heeded" and corrective_at<=k; walked_past if
    verdict=="walked_past" outright, or a "heeded" verdict misses k;
    "unclear" and "no_verdict" (no verdict row at all) are their own
    buckets, excluded from the heeded/walked_past share.

    Factored out of compute_o1 so compute_length_control can share the
    EXACT same outcome instead of the raw verdict -- round 3 item 3's bug
    was compute_length_control binning a fire "heeded" (at K_PRIMARY=3)
    when its actual verdict was heeded at, say, corrective_at=9, which
    compute_o1(seats, K_PRIMARY) would call walked_past. Both callers must
    agree on what "heeded" means for the same headline K, or the length
    control is conditioned on a different outcome than the O1 headline it
    is meant to pair with."""
    verdict = verdict_row.get("verdict") if verdict_row else None
    if verdict == "unclear":
        return "unclear"
    if verdict == "heeded":
        corrective_at = verdict_row.get("corrective_at")
        return "heeded" if (corrective_at is not None and corrective_at <= k) else "walked_past"
    if verdict == "walked_past":
        return "walked_past"
    return "no_verdict"


def compute_o1(seats: "list[SeatInputs]", k: int = K_PRIMARY):
    """O1 at a single K (round 2, item 1): walked-past share per arm and
    the arm difference ONLY (pre-registration: never print an absolute
    rate as a headline -- the per-arm shares below are intermediate values
    used to form the difference, not the number a caller should surface
    alone).

    Measures the SAME outcome (heeded vs walked past) at every K: a fire
    counts as heeded AT this K iff verdict=="heeded" and corrective_at<=k;
    otherwise it is walked_past at K. "unclear" is excluded from both and
    counted in its own bucket. Fires screened out for privacy
    (drops.jsonl, Amendment 2) or flagged by the leak audit are excluded
    entirely and reported as their own separate counts, never silently
    mixed into either bucket."""

    def _empty_tally():
        return {arm: {"heeded": 0, "walked_past": 0, "unclear": 0, "no_verdict": 0} for arm in ARMS}

    per_seat = {}
    pooled = _empty_tally()
    flagged_counts = {arm: 0 for arm in ARMS}
    dropped_counts = {arm: 0 for arm in ARMS}

    for seat_inputs in seats:
        tally = _empty_tally()
        for row in seat_inputs.ledger_rows:
            fire_id = compute_fire_id(row)
            key_entry = seat_inputs.key.get(fire_id)
            if key_entry is None:
                continue
            arm = key_entry.get("arm")
            if arm not in ARMS:
                continue
            if _is_dropped(seat_inputs.drops, fire_id):
                dropped_counts[arm] += 1
                continue
            if _is_flagged(seat_inputs.flags, fire_id):
                flagged_counts[arm] += 1
                continue
            verdict_row = seat_inputs.verdicts.get(fire_id)
            bucket = _o1_outcome_bucket(verdict_row, k)
            for _unit in expand_per_lesson(row):
                tally[arm][bucket] += 1
                pooled[arm][bucket] += 1
        per_seat[seat_inputs.seat] = tally

    def _share(tally, arm):
        d = tally[arm]
        denom = d["heeded"] + d["walked_past"]
        return (d["walked_past"] / denom) if denom else None

    def _summarize(tally):
        chain_share = _share(tally, "chain")
        control_share = _share(tally, "control")
        diff = (chain_share - control_share) if (chain_share is not None and control_share is not None) else None
        return {"tally": tally, "chain_share": chain_share, "control_share": control_share, "arm_difference": diff}

    return {
        "k": k,
        "per_seat": {seat: _summarize(t) for seat, t in per_seat.items()},
        "pooled": _summarize(pooled),
        "flagged_excluded": flagged_counts,
        "dropped_excluded": dropped_counts,
    }


def compute_o1_k_sweep(seats: "list[SeatInputs]", k_values=K_SWEEP):
    """Round 2, item 1: O1 (the SAME heeded-vs-walked-past outcome, via
    corrective_at) recomputed at every K in k_values, plus whether the
    arm-difference sign holds across the sweep.

    This REPLACES round 1's compute_o1_k_sweep, which measured a different
    quantity entirely (whether the heeding window closes via an outbound
    act before K calls elapse -- a proxy, not O1). That proxy table is
    deleted here, not kept under another name."""
    by_k = {k: compute_o1(seats, k) for k in k_values}
    diffs = [by_k[k]["pooled"]["arm_difference"] for k in k_values]
    signs = {(0 if d == 0 else (1 if d > 0 else -1)) for d in diffs if d is not None}
    sign_stable = len(signs) <= 1
    return {"by_k": by_k, "sign_stable": sign_stable}


def _quartile_edges(values: "list[float]"):
    """Local choice (spec doesn't fix a binning scheme): quartiles of the
    pooled rendered_length distribution -- see handoff's ambiguous-
    decisions log."""
    if not values:
        return []
    s = sorted(values)
    n = len(s)

    def _q(p):
        idx = min(n - 1, max(0, int(round(p * (n - 1)))))
        return s[idx]

    cuts = [s[0], _q(0.25), _q(0.5), _q(0.75), s[-1] + 1]
    cuts = sorted(set(cuts))
    edges = []
    for i in range(len(cuts) - 1):
        edges.append((cuts[i], cuts[i + 1]))
    if not edges:
        edges = [(s[0], s[-1] + 1)]
    return edges


def compute_length_control(seats: "list[SeatInputs]"):
    """rendered_length = nudge_chars + (chain_chars if chain_rendered else
    0) (spec, verbatim). Reports the arm difference in walked-past share
    within bins of rendered_length.

    Round 3, item 3: the outcome binned here is heeded-AT-K_PRIMARY --
    verdict=="heeded" and corrective_at<=K_PRIMARY -- via the same
    _o1_outcome_bucket() helper compute_o1's headline uses, not the raw
    verdict. A fire verdicted "heeded" with corrective_at=9 is
    walked_past at K_PRIMARY=3 and must be binned as such here too, or
    this table is conditioned on a different outcome than the O1 headline
    it pairs with. Only fires with a heeded/walked_past outcome AT
    K_PRIMARY participate (unclear and no_verdict excluded from the
    share, consistent with O1's own denominator rule)."""
    entries = []  # (rendered_length, arm, outcome) -- outcome is heeded-at-K_PRIMARY, not the raw verdict
    flagged_counts = {arm: 0 for arm in ARMS}
    dropped_counts = {arm: 0 for arm in ARMS}
    for seat_inputs in seats:
        for row in seat_inputs.ledger_rows:
            fire_id = compute_fire_id(row)
            key_entry = seat_inputs.key.get(fire_id)
            if key_entry is None:
                continue
            arm = key_entry.get("arm")
            if arm not in ARMS:
                continue
            if _is_dropped(seat_inputs.drops, fire_id):
                dropped_counts[arm] += 1
                continue
            if _is_flagged(seat_inputs.flags, fire_id):
                flagged_counts[arm] += 1
                continue
            verdict_row = seat_inputs.verdicts.get(fire_id)
            outcome = _o1_outcome_bucket(verdict_row, K_PRIMARY)
            if outcome not in ("heeded", "walked_past"):
                continue
            rendered_length = (row.get("nudge_chars") or 0) + (
                (row.get("chain_chars") or 0) if row.get("chain_rendered") else 0
            )
            for _unit in expand_per_lesson(row):
                entries.append((rendered_length, arm, outcome))

    if not entries:
        return {"bins": [], "flagged_excluded": flagged_counts, "dropped_excluded": dropped_counts}

    lengths = [e[0] for e in entries]
    edges = _quartile_edges(lengths)
    bins = []
    for lo, hi in edges:
        per_arm = {arm: [0, 0] for arm in ARMS}  # [walked_past, total]
        for length, arm, outcome in entries:
            # Half-open [lo, hi) throughout -- the top edge is built as
            # max(lengths) + 1 (see _quartile_edges), so this uniformly
            # includes the largest observed length in the final bin
            # without a separate closed-interval special case.
            if not (lo <= length < hi):
                continue
            if arm not in per_arm:
                continue
            per_arm[arm][1] += 1
            if outcome == "walked_past":
                per_arm[arm][0] += 1

        def _share(arm):
            n, d = per_arm[arm]
            return (n / d) if d else None

        chain_share = _share("chain")
        control_share = _share("control")
        diff = (chain_share - control_share) if (chain_share is not None and control_share is not None) else None
        bins.append({"range": [lo, hi], "n": sum(v[1] for v in per_arm.values()), "arm_difference": diff})
    return {"bins": bins, "flagged_excluded": flagged_counts, "dropped_excluded": dropped_counts}


def compute_o2(seats: "list[SeatInputs]", db_path):
    """For each participating fire, from the fire to the end of that
    session or the next compaction boundary, count node-creating ENGRAM
    calls whose payload cites any of the fired lesson's heads: o2_raw and
    o2_transfer (transfer = the fired lesson's situation_pattern does NOT
    match the citing call's own build_match_target)."""
    situation_patterns = load_situation_patterns(db_path) if db_path else {}
    transcript_cache: dict = {}
    per_arm = {arm: {"o2_raw": 0, "o2_transfer": 0, "participating": 0} for arm in ARMS}
    flagged_counts = {arm: 0 for arm in ARMS}
    dropped_counts = {arm: 0 for arm in ARMS}

    for seat_inputs in seats:
        for row in seat_inputs.ledger_rows:
            fire_id = compute_fire_id(row)
            key_entry = seat_inputs.key.get(fire_id)
            if key_entry is None:
                continue
            arm = key_entry.get("arm")
            if arm not in ARMS:
                continue
            if _is_dropped(seat_inputs.drops, fire_id):
                dropped_counts[arm] += 1
                continue
            if _is_flagged(seat_inputs.flags, fire_id):
                flagged_counts[arm] += 1
                continue
            records = _transcript_cache_get(transcript_cache, row.get("transcript_path"))
            if records is None:
                continue
            fired = find_fired_tool_use(records, row.get("tool_use_id"), row.get("tool_name"), row.get("ts"))
            if fired is None:
                continue
            per_arm[arm]["participating"] += 1
            end_idx = o2_scan_end(records, fired.record_idx)

            for unit in expand_per_lesson(row):
                heads = unit["heads"]
                if not heads:
                    continue
                pattern = situation_patterns.get(unit["lesson_id"])
                for ridx in range(fired.record_idx + 1, end_idx + 1):
                    rec = records[ridx]
                    if is_sidechain(rec) or rec.get("type") != "assistant":
                        continue
                    for item in get_message_content(rec):
                        if not isinstance(item, dict) or item.get("type") != "tool_use":
                            continue
                        name = item.get("name", "")
                        if not is_node_creating_engram_call(name):
                            continue
                        tool_input = item.get("input") or {}
                        try:
                            serialized = json.dumps(tool_input, ensure_ascii=False)
                        except (TypeError, ValueError):
                            serialized = str(tool_input)
                        if not any(h in serialized for h in heads):
                            continue
                        per_arm[arm]["o2_raw"] += 1
                        call_target = build_match_target({"tool_name": name, "tool_input": tool_input})
                        if not situation_pattern_matches(pattern, call_target):
                            per_arm[arm]["o2_transfer"] += 1

    return {"per_arm": per_arm, "flagged_excluded": flagged_counts, "dropped_excluded": dropped_counts}


def compute_sample_report(seats: "list[SeatInputs]") -> dict:
    """Amendment 3 (#2753), Kepler's ruling 2026-09-27 18:47Z: reports the
    sampled fraction per seat, and the per-lesson composition of each
    seat's sample -- so a two-lesson seat is never read as a fleet result.
    Aggregates each seat's sample_summary.json files (one per UTC day, via
    SeatInputs.sample_summaries) into per-seat totals, per-lesson
    composition (summed over days), a per-day breakdown (sorted by date),
    and a pooled total across seats. sampled_fraction is None when
    eligible==0 (never a bogus 0/0), and these are always counts/fractions
    of the SAMPLE only, never a walked-past rate.

    A seat whose summaries carry more than one distinct sample_n or
    sample_seed is reported loudly via config_changed=True plus the
    distinct values seen -- the ruling forbids changing N mid-window, and
    this is never silently averaged over or ignored."""
    per_seat = {}
    pooled_eligible = 0
    pooled_sampled = 0

    for seat_inputs in seats:
        summaries = seat_inputs.sample_summaries
        eligible = 0
        sampled = 0
        per_lesson: dict = {}
        per_day = []
        sample_ns = set()
        sample_seeds = set()
        for summary in summaries:
            summary_eligible = summary.get("eligible", 0)
            summary_sampled = summary.get("sampled", 0)
            eligible += summary_eligible
            sampled += summary_sampled
            sample_ns.add(summary.get("sample_n"))
            sample_seeds.add(summary.get("sample_seed"))
            for lesson_id, counts in (summary.get("per_lesson") or {}).items():
                bucket = per_lesson.setdefault(lesson_id, {"eligible": 0, "sampled": 0})
                bucket["eligible"] += counts.get("eligible", 0)
                bucket["sampled"] += counts.get("sampled", 0)
            per_day.append({"date": summary.get("date"), "eligible": summary_eligible, "sampled": summary_sampled})
        per_day.sort(key=lambda d: d["date"])

        seat_report = {
            "days": len(summaries),
            "eligible": eligible,
            "sampled": sampled,
            "sampled_fraction": (sampled / eligible) if eligible else None,
            "per_lesson": per_lesson,
            "per_day": per_day,
            "config_changed": len(sample_ns) > 1 or len(sample_seeds) > 1,
        }
        if seat_report["config_changed"]:
            seat_report["sample_n_values"] = sorted(sample_ns, key=lambda v: (v is None, v))
            seat_report["sample_seed_values"] = sorted(sample_seeds, key=lambda v: (v is None, v))
        per_seat[seat_inputs.seat] = seat_report

        pooled_eligible += eligible
        pooled_sampled += sampled

    pooled = {
        "eligible": pooled_eligible,
        "sampled": pooled_sampled,
        "sampled_fraction": (pooled_sampled / pooled_eligible) if pooled_eligible else None,
    }
    return {"per_seat": per_seat, "pooled": pooled}


_KNOWN_LEDGER_ARMS = ("chain", "control", "na", "error")
_ERROR_STRING_MAX_LEN = 120


def compute_denominators(seats: "list[SeatInputs]", unlocatable_paths=None):
    """fires_by_arm / unclear / dropped_privacy / unlocatable, as before,
    joined through each seat's key.jsonl -- chain/control fires only.

    Also computed here, from the RAW ledger rows of each seat (never
    joined through the key -- na/error rows have no key.jsonl entry at
    all, by design: extract_spans.py's ADJUDICATION_ARMS = {"chain",
    "control"} is correct and untouched):

    ledger_rows_by_arm: {"pooled": {arm_value: count}, "per_seat":
    {seat: {arm_value: count}}}, over EVERY ledger row of every seat. The
    row's own "arm" field is used as-is; a missing or non-string "arm"
    counts under the key "missing". chain/control/na/error are always
    present (0 when absent) so a reader sees a zero rather than a missing
    key. Contract: every ledger row is counted exactly once -- per seat,
    the values sum to len(ledger_rows) for that seat, and pooled sums to
    the grand total. An "error" row is never counted in any chain/control
    tally (O1, O2, length control) -- those all join through the key,
    which na/error rows never have.

    error_rows: {"count": N, "by_error": {error_string_truncated_to_120:
    count}}, pooled across all seats -- PR #3173's `error` rows (arm draw
    or row construction failed) surfaced by error string, so a failure
    stays visible in the denominator instead of disappearing. An `error`
    row with no `error` key (or an explicit null) is bucketed under the
    literal string "(no error string)", never the misleading "None".

    unlocatable_by_arm: {arm_value: count, "unmatched": count} -- each
    unlocatable row (from unlocatable_paths) joined to an arm by
    recomputing compute_fire_id() over the RAW ledger rows of ALL seats
    (never through key.jsonl -- an unlocatable fire never gets a
    key.jsonl row, so this join is the only way to see whether location
    failures cluster in one arm). A fire_id with no matching ledger row
    counts under "unmatched". None (not computed) when unlocatable_paths
    is not given, mirroring "unlocatable" itself."""
    fires_by_arm = {arm: 0 for arm in ARMS}
    unclear = 0
    dropped_privacy = {arm: 0 for arm in ARMS}
    ledger_rows_by_arm_pooled = {arm: 0 for arm in _KNOWN_LEDGER_ARMS}
    ledger_rows_by_arm_per_seat = {}
    error_row_count = 0
    error_by_string: dict = {}

    for seat_inputs in seats:
        seat_arm_counts = {arm: 0 for arm in _KNOWN_LEDGER_ARMS}
        for row in seat_inputs.ledger_rows:
            raw_arm = row.get("arm")
            arm_key = raw_arm if isinstance(raw_arm, str) else "missing"
            seat_arm_counts[arm_key] = seat_arm_counts.get(arm_key, 0) + 1
            ledger_rows_by_arm_pooled[arm_key] = ledger_rows_by_arm_pooled.get(arm_key, 0) + 1
            if arm_key == "error":
                error_row_count += 1
                error_string = row.get("error")
                if error_string is None:
                    error_string = "(no error string)"
                elif not isinstance(error_string, str):
                    error_string = str(error_string)
                error_string = error_string[:_ERROR_STRING_MAX_LEN]
                error_by_string[error_string] = error_by_string.get(error_string, 0) + 1

            fire_id = compute_fire_id(row)
            key_entry = seat_inputs.key.get(fire_id)
            if key_entry is None:
                continue
            arm = key_entry.get("arm")
            if arm not in ARMS:
                continue
            fires_by_arm[arm] += 1
            # Amendment 2 / round 2 item 2: drops.jsonl carries no arm --
            # the join to the arm happens here, via the seat's own key.
            if _is_dropped(seat_inputs.drops, fire_id):
                dropped_privacy[arm] += 1
            verdict_row = seat_inputs.verdicts.get(fire_id)
            if verdict_row and verdict_row.get("verdict") == "unclear":
                unclear += 1
        ledger_rows_by_arm_per_seat[seat_inputs.seat] = seat_arm_counts

    unlocatable = None
    unlocatable_by_arm = None
    if unlocatable_paths:
        unlocatable = 0
        unlocatable_by_arm = {arm: 0 for arm in ARMS}
        unlocatable_by_arm["unmatched"] = 0
        # Join basis for unlocatable_by_arm: fire_id -> arm, recomputed from
        # the RAW ledger rows of every seat (never through key.jsonl -- an
        # unlocatable fire never gets a key.jsonl row).
        fire_id_to_arm = {}
        for seat_inputs in seats:
            for row in seat_inputs.ledger_rows:
                fire_id_to_arm[compute_fire_id(row)] = row.get("arm")
        for p in unlocatable_paths:
            if p and Path(p).exists():
                unlocatable_rows = load_jsonl(p)
                unlocatable += len(unlocatable_rows)
                for row in unlocatable_rows:
                    arm = fire_id_to_arm.get(row.get("fire_id"))
                    if arm in ARMS:
                        unlocatable_by_arm[arm] += 1
                    else:
                        unlocatable_by_arm["unmatched"] += 1
    return {
        "fires_by_arm": fires_by_arm,
        "unclear": unclear,
        "dropped_privacy": dropped_privacy,
        "unlocatable": unlocatable,
        "unlocatable_by_arm": unlocatable_by_arm,
        "ledger_rows_by_arm": {"pooled": ledger_rows_by_arm_pooled, "per_seat": ledger_rows_by_arm_per_seat},
        "error_rows": {"count": error_row_count, "by_error": error_by_string},
    }


def _print_sample_report(sample_report: dict) -> None:
    """Amendment 3's printed section (#2753): one line per seat with
    eligible / sampled / fraction / days, then that seat's per-lesson
    lines, then the pooled line. Counts and fractions of the SAMPLE only
    -- never a walked-past rate."""
    print("\n== Sample (amendment 3) ==")
    for seat, info in sample_report["per_seat"].items():
        print(
            f"  seat={seat} eligible={info['eligible']} sampled={info['sampled']} "
            f"fraction={info['sampled_fraction']} days={info['days']}"
        )
        for lesson_id, counts in info["per_lesson"].items():
            print(f"    lesson={lesson_id} eligible={counts['eligible']} sampled={counts['sampled']}")
        if info["config_changed"]:
            print(
                f"  WARNING: seat={seat} sample_n/sample_seed changed mid-window -- "
                f"sample_n values={info['sample_n_values']} sample_seed values={info['sample_seed_values']}"
            )
    pooled = sample_report["pooled"]
    print(
        f"  pooled eligible={pooled['eligible']} sampled={pooled['sampled']} "
        f"fraction={pooled['sampled_fraction']}"
    )


def _print_report(
    o1_headline,
    o1_k_sweep,
    length_control,
    o2,
    denominators,
    no_flags_warning=False,
    sample_report=None,
    no_sample_summary_warning=False,
) -> None:
    if no_flags_warning:
        print(
            "WARNING: --no-flags was passed -- the leak audit is disabled; head-named "
            "spans (where a fired lesson's head id still appears in the span) will be "
            "silently mixed into every table below.\n"
        )
    if no_sample_summary_warning:
        print(
            "WARNING: --no-sample-summary was passed -- the sample report (amendment 3) is "
            "disabled; this run's report will not show the sampled fraction per seat.\n"
        )

    print("== Denominators ==")
    print(json.dumps(denominators, indent=2))
    error_rows = denominators["error_rows"]
    print(f"error rows (arm unknown; in no O1/O2/length table): {error_rows['count']}")
    if error_rows["count"] > 0:
        for error_string, count in error_rows["by_error"].items():
            print(f"  {count}x {error_string}")
    unlocatable_by_arm = denominators.get("unlocatable_by_arm")
    if unlocatable_by_arm is not None:
        print(f"unlocatable by arm: {unlocatable_by_arm}")
    # Reviewer note (PR #3175 round-3 comment): flagged/dropped_privacy
    # counts below are per-fire, but every table they guard tallies per
    # shown-lesson-unit (expand_per_lesson) -- a multi-lesson fire is one
    # flagged/dropped count against N lesson-units of included tally. Not
    # a correctness defect (per-fire and per-lesson-unit coincide whenever
    # shown_lesson_ids is empty, today's fallback), but the two counts are
    # in different units and must not be summed against each other.
    print("(note: flagged/dropped_privacy counts are per-fire; all other counts below are per-lesson-unit)")

    if sample_report is not None:
        _print_sample_report(sample_report)

    print(
        f"\n== O1: walked-past share, arm difference at K_PRIMARY={K_PRIMARY} "
        "(pre-registration: never an absolute rate as headline) =="
    )
    print(f"pooled arm_difference={o1_headline['pooled']['arm_difference']}")
    for seat, summary in o1_headline["per_seat"].items():
        print(f"  seat={seat} arm_difference={summary['arm_difference']}")
    print(f"flagged (excluded from tally above): {o1_headline['flagged_excluded']}")
    print(f"dropped_privacy (excluded from tally above): {o1_headline['dropped_excluded']}")

    print("\n== O1 K-sweep (same outcome, every K; sign stability across K) ==")
    for k, summary in o1_k_sweep["by_k"].items():
        print(f"  K={k} arm_difference={summary['pooled']['arm_difference']}")
    print(f"sign_stable={o1_k_sweep['sign_stable']}")

    print("\n== Length control (rendered_length bins) ==")
    for b in length_control["bins"]:
        print(f"  range={b['range']} n={b['n']} arm_difference={b['arm_difference']}")
    print(f"flagged (excluded): {length_control['flagged_excluded']}")
    print(f"dropped_privacy (excluded): {length_control['dropped_excluded']}")

    print("\n== O2: node-creating ENGRAM citations of fired heads (raw vs transfer) ==")
    for arm, counts in o2["per_arm"].items():
        print(f"  arm={arm} {counts}")
    print(f"flagged (excluded): {o2['flagged_excluded']}")
    print(f"dropped_privacy (excluded): {o2['dropped_excluded']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seat", action="append", required=True, help="seat label for this input set (repeatable)")
    parser.add_argument("--ledger", action="append", required=True, help="fire-ledger JSONL path (repeatable, paired with --seat)")
    parser.add_argument("--verdicts", action="append", required=True, help="verdicts.jsonl path (repeatable, paired with --seat)")
    parser.add_argument("--key", action="append", required=True, help="key.jsonl path from extract_spans.py (repeatable, paired with --seat)")
    parser.add_argument(
        "--flags",
        action="append",
        default=None,
        help=(
            "flags.jsonl path from extract_spans.py (repeatable, paired with --seat). "
            "Required unless --no-flags is explicitly passed (round 2, item 3) -- "
            "without it, head-named spans are silently mixed into every table."
        ),
    )
    parser.add_argument(
        "--no-flags",
        action="store_true",
        help=(
            "Explicitly run WITHOUT the leak-audit flags.jsonl sidecar. Prints a "
            "warning naming the consequence. Mutually exclusive with --flags."
        ),
    )
    parser.add_argument(
        "--drops",
        action="append",
        default=None,
        help="drops.jsonl path from a seat's privacy screener (Amendment 2; repeatable, paired with --seat; optional)",
    )
    parser.add_argument("--unlocatable", action="append", default=None, help="unlocatable.jsonl path(s), for the denominators table (optional)")
    parser.add_argument(
        "--sample-summary",
        action="append",
        default=None,
        help=(
            "sample_summary.json GLOB from extract_spans.py (repeatable, paired with --seat; "
            "one glob per --seat, e.g. out/seatA/*/sample_summary.json -- one file per UTC "
            "day). Required unless --no-sample-summary is explicitly passed (amendment 3). "
            "A glob matching zero files is a fail-loud error naming the seat."
        ),
    )
    parser.add_argument(
        "--no-sample-summary",
        action="store_true",
        help=(
            "Explicitly run WITHOUT the amendment-3 sample report. Prints a warning naming "
            "the consequence. Mutually exclusive with --sample-summary."
        ),
    )
    parser.add_argument("--db", default=None, help="knowledge.db path, read-only (for O2 situation_pattern lookups)")
    args = parser.parse_args(argv)

    n = len(args.seat)
    for name, values in (("--ledger", args.ledger), ("--verdicts", args.verdicts), ("--key", args.key)):
        if len(values) != n:
            parser.error(f"{name} must be given once per --seat (got {len(values)}, expected {n})")

    # Round 2, item 3: --flags is required unless --no-flags is explicitly
    # passed. Omitting --flags silently (round 1's behavior) is no longer
    # allowed -- a missing flags file must never quietly disable the leak
    # audit.
    if args.no_flags:
        if args.flags:
            parser.error("--no-flags and --flags are mutually exclusive")
        flags_list = [None] * n
    else:
        if not args.flags:
            parser.error(
                "--flags is required (once per --seat) unless --no-flags is explicitly "
                "passed -- omitting it silently would disable the leak audit"
            )
        if len(args.flags) != n:
            parser.error(f"--flags must be given once per --seat (got {len(args.flags)}, expected {n})")
        flags_list = args.flags

    drops_list = args.drops if args.drops is not None else [None] * n
    if len(drops_list) != n:
        parser.error(f"--drops must be given once per --seat (got {len(drops_list)}, expected {n})")

    # Amendment 3: --sample-summary is required unless --no-sample-summary is
    # explicitly passed. Omitting it silently would leave the sample report
    # missing with no indication why.
    if args.no_sample_summary:
        if args.sample_summary:
            parser.error("--sample-summary and --no-sample-summary are mutually exclusive")
        sample_summary_list = [None] * n
    else:
        if not args.sample_summary:
            parser.error(
                "--sample-summary is required (once per --seat) unless --no-sample-summary "
                "is explicitly passed -- omitting it silently would disable the sample report"
            )
        if len(args.sample_summary) != n:
            parser.error(
                f"--sample-summary must be given once per --seat (got {len(args.sample_summary)}, expected {n})"
            )
        sample_summary_list = args.sample_summary

    seats = []
    for seat, ledger_path, verdicts_path, key_path, flags_path, drops_path, sample_summary_pattern in zip(
        args.seat, args.ledger, args.verdicts, args.key, flags_list, drops_list, sample_summary_list
    ):
        seats.append(
            SeatInputs(
                seat=seat,
                ledger_rows=load_jsonl(ledger_path),
                key=load_key(key_path),
                verdicts=load_verdicts(verdicts_path),
                flags=load_flags(flags_path),
                drops=load_drops(drops_path),
                sample_summaries=(
                    load_sample_summaries(sample_summary_pattern, seat)
                    if sample_summary_pattern is not None
                    else []
                ),
            )
        )

    o1_headline = compute_o1(seats, K_PRIMARY)
    o1_k_sweep = compute_o1_k_sweep(seats)
    length_control = compute_length_control(seats)
    o2 = compute_o2(seats, args.db)
    denominators = compute_denominators(seats, args.unlocatable)
    sample_report = None if args.no_sample_summary else compute_sample_report(seats)

    _print_report(
        o1_headline,
        o1_k_sweep,
        length_control,
        o2,
        denominators,
        no_flags_warning=args.no_flags,
        sample_report=sample_report,
        no_sample_summary_warning=args.no_sample_summary,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
