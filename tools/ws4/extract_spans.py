#!/usr/bin/env python3
"""WS4 adjudication kit, Piece 1 (engram-alpha#2753) -- cut heeding-window
spans out of Claude Code transcripts for blinded human adjudication.

For each ledger row (in $ENGRAM_HOME/tripwire-fire-ledger.jsonl, schema_version
1 -- see ws4-fire-ledger.md section 2 for the full field list) with arm in
{chain, control} on the given UTC date:

  1. Open transcript_path; locate the fired tool call (by tool_use_id when
     present, otherwise by the nearest assistant tool_use to ts with
     matching tool_name -- record locate_basis).
  2. Cut the heeding window ONCE, at k=max(K_SWEEP) (round 2, item 1):
     starts after the fired call; closes at the first of k further tool
     calls or the first OUTBOUND ACT -- see tools/ws4/constants.py. --k
     stays as an override for tests only; production runs always extract
     at the largest K so one adjudication pass supports O1 at every K in
     the sweep.
  3. Write one markdown span per fire: the fired call, the nudge text
     (verbatim, chain-block redacted per REDACT_CHAIN), then each
     subsequent call/result/text up to the window close, with every tool
     call after the fired call numbered "[call 1]", "[call 2]", ... in
     transcript order (round 2, item 1) -- so the adjudicator can name the
     call where a corrective act happens (verdicts.jsonl's corrective_at).
     The header carries the fire id, never the arm.
  4. Write a separate key.jsonl (fire_id -> arm) for the ANALYST, never
     given to the adjudicator.

Amendment 3 (#2753, adopted by Kepler 2026-09-27 18:47Z): per seat per UTC
day, at most --sample fires (default tools/ws4/constants.py's SAMPLE_N) are
adjudicated -- the sample is the N eligible (chain+control) fires with the
LOWEST sha256(f"{sample_seed}:{fire_id}") hex digest, --sample-seed
defaulting to constants.SAMPLE_SEED. fire_id never hashes arm (see
ledger.compute_fire_id) and the arm draw uses its own separate seed, so the
sample is arm-blind by construction -- the same fire_ids with the arms
swapped give the identical sample. --sample 0 disables sampling entirely
(every eligible fire is extracted, the pre-amendment-3 behaviour; for tests
and audits). Only SAMPLED fires are extracted (spans/, key.jsonl,
flags.jsonl, unlocatable.jsonl rows); unsampled fires get no span and no key
row, but are never dropped from accounting. Two new analyst-only files
record every eligible fire regardless of sampling outcome -- sample.jsonl
(fire_id, sampled, rank, shown_lesson_ids; NO arm field) and
sample_summary.json (date, sample_n, sample_seed, eligible, sampled,
per-lesson eligible/sampled composition). Like key.jsonl, NEITHER file is
ever given to an adjudicator or a screener.

If the transcript can't be read or the call can't be located, the row is
recorded in unlocatable.jsonl with a reason instead of being silently
dropped. Piece 1's invariant (amendment 3: over SAMPLED fires, not every
eligible fire): sampled == spans_out + unlocatable, and eligible ==
sampled + unsampled, both printed at the end of every run.

Read-only: never writes to the ledger or to knowledge.db. Stdlib only.

Usage:
    python -m tools.ws4.extract_spans --date 2026-09-28 --out /tmp/ws4-out
    python -m tools.ws4.extract_spans --date 2026-09-28 --k 6 --out /tmp/ws4-out \\
        --ledger /path/to/tripwire-fire-ledger.jsonl   # override for testing
    python -m tools.ws4.extract_spans --date 2026-09-28 --out /tmp/ws4-out \\
        --sample 0   # no sampling -- every eligible fire (tests/audits)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from .constants import MAX_K_SWEEP, REDACT_CHAIN, SAMPLE_N, SAMPLE_SEED
from .ledger import (
    append_jsonl,
    compute_fire_id,
    flatten_heads,
    load_jsonl,
    rows_for_date_and_arms,
    write_jsonl,
)
from .transcript import (
    TranscriptReadError,
    build_span,
    compute_window,
    find_fired_tool_use,
    find_tripwire_attachment,
    head_named_in_span,
    load_transcript,
)

ADJUDICATION_ARMS = {"chain", "control"}


def default_ledger_path() -> str:
    engram_home = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
    return str(Path(engram_home) / "tripwire-fire-ledger.jsonl")


def extract_one(row: dict, k: int, redact_chain: bool = REDACT_CHAIN):
    """Process a single ledger row.

    Returns a dict with keys: fire_id, ok (bool), span_text (or None),
    reason (unlocatable reason, or None), locate_basis, close_reason,
    head_named_in_span (bool), heads (list). Never raises -- transcript
    read/locate failures are reported via ok=False + reason, never an
    exception the caller has to catch per row.
    """
    fire_id = compute_fire_id(row)
    transcript_path = row.get("transcript_path")

    try:
        records = load_transcript(transcript_path)
    except TranscriptReadError as exc:
        return {"fire_id": fire_id, "ok": False, "reason": f"transcript_unreadable: {exc}"}

    fired = find_fired_tool_use(records, row.get("tool_use_id"), row.get("tool_name"), row.get("ts"))
    if fired is None:
        return {"fire_id": fire_id, "ok": False, "reason": "call_not_located"}

    attachment = find_tripwire_attachment(records, fired)
    if attachment is None:
        return {
            "fire_id": fire_id,
            "ok": False,
            "reason": "tripwire_attachment_not_found",
            "locate_basis": fired.locate_basis,
        }

    window = compute_window(records, fired, k)
    span_text = build_span(records, fired, window, fire_id, k, redact_chain=redact_chain)
    heads = flatten_heads(row.get("heads_by_lesson"))
    flagged = head_named_in_span(span_text, heads)

    return {
        "fire_id": fire_id,
        "ok": True,
        "span_text": span_text,
        "locate_basis": fired.locate_basis,
        "close_reason": window.close_reason,
        "head_named_in_span": flagged,
        "heads": heads,
        "arm": row.get("arm"),
    }


def _sample_sort_key(fire_id: str, sample_seed: str):
    """Amendment 3's ranking key: sha256(f"{sample_seed}:{fire_id}") hex
    digest, with fire_id itself as the tiebreaker so the order is total
    (ties are impossible in practice). Never hashes arm -- fire_id itself
    never carries arm (ledger.compute_fire_id) -- so this ranking is
    arm-blind by construction."""
    digest = hashlib.sha256(f"{sample_seed}:{fire_id}".encode("utf-8")).hexdigest()
    return (digest, fire_id)


def run(
    date_str: str,
    k: int,
    out_dir: str,
    ledger_path: str,
    redact_chain: bool = REDACT_CHAIN,
    sample_n: int = SAMPLE_N,
    sample_seed: str = SAMPLE_SEED,
) -> dict:
    rows = load_jsonl(ledger_path)
    selected = rows_for_date_and_arms(rows, date_str, ADJUDICATION_ARMS)

    out = Path(out_dir)
    spans_dir = out / "spans"
    spans_dir.mkdir(parents=True, exist_ok=True)

    eligible = len(selected)

    # Amendment 3: rank every eligible fire by the hash above and keep the
    # lowest sample_n. sample_n <= 0 ("--sample 0") means no sampling --
    # every eligible fire is kept (the pre-amendment-3 behaviour).
    fire_ids = [compute_fire_id(row) for row in selected]
    ranked_idx = sorted(range(eligible), key=lambda i: _sample_sort_key(fire_ids[i], sample_seed))
    effective_n = eligible if sample_n <= 0 else min(sample_n, eligible)

    sampled_fire_ids = set()
    sample_rows = []
    per_lesson = {}
    for rank, idx in enumerate(ranked_idx):
        row = selected[idx]
        fire_id = fire_ids[idx]
        is_sampled = rank < effective_n
        if is_sampled:
            sampled_fire_ids.add(fire_id)
        shown = list(row.get("shown_lesson_ids") or [])
        sample_rows.append(
            {
                "fire_id": fire_id,
                "sampled": is_sampled,
                "rank": rank,
                "shown_lesson_ids": shown,
            }
        )
        lesson_keys = shown if shown else ["(none)"]
        for lesson_id in lesson_keys:
            bucket = per_lesson.setdefault(lesson_id, {"eligible": 0, "sampled": 0})
            bucket["eligible"] += 1
            if is_sampled:
                bucket["sampled"] += 1

    key_rows = []
    unlocatable_rows = []
    flags_rows = []
    spans_out = 0

    for idx, row in enumerate(selected):
        fire_id = fire_ids[idx]
        if fire_id not in sampled_fire_ids:
            # Unsampled fires get no span and no key row (amendment 3) --
            # they are still accounted for in sample.jsonl / summary above,
            # just never extracted.
            continue

        result = extract_one(row, k, redact_chain=redact_chain)
        if not result["ok"]:
            unlocatable_rows.append(
                {
                    "fire_id": fire_id,
                    "transcript_path": row.get("transcript_path"),
                    "tool_use_id": row.get("tool_use_id"),
                    "ts": row.get("ts"),
                    "reason": result["reason"],
                }
            )
            continue

        (spans_dir / f"{fire_id}.md").write_text(result["span_text"], encoding="utf-8")
        spans_out += 1
        # key.jsonl is STRICTLY fire_id -> arm (spec, Piece 1) -- never
        # adjudicator-facing, and never enriched with anything else here.
        key_rows.append({"fire_id": fire_id, "arm": result["arm"]})
        # flags.jsonl is this kit's own analyst-only sidecar (not named by
        # the spec) carrying the leak-audit + diagnostic fields analyze.py
        # needs for its "report flagged fires as a separate row" duty --
        # see handoff's ambiguous-decisions log.
        flags_rows.append(
            {
                "fire_id": fire_id,
                "head_named_in_span": result["head_named_in_span"],
                "locate_basis": result["locate_basis"],
                "close_reason": result["close_reason"],
            }
        )

    write_jsonl(out / "key.jsonl", key_rows)
    write_jsonl(out / "unlocatable.jsonl", unlocatable_rows)
    write_jsonl(out / "flags.jsonl", flags_rows)
    write_jsonl(out / "sample.jsonl", sample_rows)

    sampled_n = len(sampled_fire_ids)
    # Counted from the rows actually written to sample.jsonl, not derived as
    # eligible - sampled_n, so the second invariant below can fail: a dropped
    # or duplicated eligible fire, or a fire_id collision, breaks it.
    unsampled_n = sum(1 for r in sample_rows if not r["sampled"])
    unlocatable_n = len(unlocatable_rows)

    # Amendment 3's invariant supersedes Piece 1's: it now holds over the
    # SAMPLED set, not every eligible fire (unsampled fires are never
    # extracted, so they can never appear as spans_out or unlocatable).
    assert sampled_n == spans_out + unlocatable_n, (
        f"WS4 extract_spans invariant violated: sampled={sampled_n} spans_out={spans_out} "
        f"unlocatable={unlocatable_n}"
    )
    assert eligible == len(sample_rows) == sampled_n + unsampled_n, (
        f"WS4 extract_spans invariant violated: eligible={eligible} sampled={sampled_n} "
        f"unsampled={unsampled_n}"
    )

    summary = {
        "date": date_str,
        "sample_n": sample_n,
        "sample_seed": sample_seed,
        "eligible": eligible,
        "sampled": sampled_n,
        "per_lesson": per_lesson,
    }
    (out / "sample_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "rows_in": eligible,
        "eligible": eligible,
        "sampled": sampled_n,
        "unsampled": unsampled_n,
        "spans_out": spans_out,
        "unlocatable": unlocatable_n,
        "flagged": sum(1 for r in flags_rows if r["head_named_in_span"]),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", required=True, help="UTC date, YYYY-MM-DD (matched against ledger ts_iso)")
    parser.add_argument(
        "--k",
        type=int,
        default=MAX_K_SWEEP,
        help=(
            f"heeding-window K (default: max(K_SWEEP)={MAX_K_SWEEP} -- round 2 item 1: "
            "extract once, at the largest K, so the numbered span supports every K in "
            "the sweep from one adjudication pass; override for tests only)"
        ),
    )
    parser.add_argument(
        "--out",
        required=True,
        help="output directory (spans/, key.jsonl, unlocatable.jsonl, flags.jsonl, sample.jsonl, sample_summary.json)",
    )
    parser.add_argument(
        "--ledger",
        default=None,
        help="override the fire-ledger path (default: $ENGRAM_HOME/tripwire-fire-ledger.jsonl)",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=SAMPLE_N,
        help=(
            f"amendment 3 (#2753): sample at most N chain+control fires per run, "
            f"ranked by sha256(sample_seed:fire_id) (default: SAMPLE_N={SAMPLE_N}). "
            "0 means no sampling -- every eligible fire is extracted "
            "(pre-amendment-3 behaviour; for tests and audits)."
        ),
    )
    parser.add_argument(
        "--sample-seed",
        default=SAMPLE_SEED,
        help=f"amendment 3 (#2753): published sample seed (default: SAMPLE_SEED={SAMPLE_SEED!r}).",
    )
    args = parser.parse_args(argv)

    ledger_path = args.ledger or default_ledger_path()
    report = run(
        args.date, args.k, args.out, ledger_path,
        sample_n=args.sample, sample_seed=args.sample_seed,
    )

    print(f"eligible={report['eligible']} sampled={report['sampled']} unsampled={report['unsampled']}")
    print(f"spans_out={report['spans_out']} unlocatable={report['unlocatable']}")
    print(
        "invariant sampled == spans_out + unlocatable: "
        f"{report['sampled']} == {report['spans_out']} + {report['unlocatable']}"
    )
    print(
        "invariant eligible == sampled + unsampled: "
        f"{report['eligible']} == {report['sampled']} + {report['unsampled']}"
    )
    print(f"flagged (head_named_in_span): {report['flagged']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
