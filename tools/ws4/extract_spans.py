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

If the transcript can't be read or the call can't be located, the row is
recorded in unlocatable.jsonl with a reason instead of being silently
dropped. Piece 1's invariant: rows_in == spans_out + unlocatable, printed at
the end of every run.

Read-only: never writes to the ledger or to knowledge.db. Stdlib only.

Usage:
    python -m tools.ws4.extract_spans --date 2026-09-28 --out /tmp/ws4-out
    python -m tools.ws4.extract_spans --date 2026-09-28 --k 6 --out /tmp/ws4-out \\
        --ledger /path/to/tripwire-fire-ledger.jsonl   # override for testing
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .constants import MAX_K_SWEEP, REDACT_CHAIN
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


def run(date_str: str, k: int, out_dir: str, ledger_path: str, redact_chain: bool = REDACT_CHAIN) -> dict:
    rows = load_jsonl(ledger_path)
    selected = rows_for_date_and_arms(rows, date_str, ADJUDICATION_ARMS)

    out = Path(out_dir)
    spans_dir = out / "spans"
    spans_dir.mkdir(parents=True, exist_ok=True)

    key_rows = []
    unlocatable_rows = []
    flags_rows = []
    spans_out = 0

    for row in selected:
        result = extract_one(row, k, redact_chain=redact_chain)
        fire_id = result["fire_id"]
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

    rows_in = len(selected)
    unlocatable_n = len(unlocatable_rows)
    assert rows_in == spans_out + unlocatable_n, (
        f"WS4 extract_spans invariant violated: rows_in={rows_in} spans_out={spans_out} "
        f"unlocatable={unlocatable_n}"
    )

    return {
        "rows_in": rows_in,
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
    parser.add_argument("--out", required=True, help="output directory (spans/, key.jsonl, unlocatable.jsonl, flags.jsonl)")
    parser.add_argument(
        "--ledger",
        default=None,
        help="override the fire-ledger path (default: $ENGRAM_HOME/tripwire-fire-ledger.jsonl)",
    )
    args = parser.parse_args(argv)

    ledger_path = args.ledger or default_ledger_path()
    report = run(args.date, args.k, args.out, ledger_path)

    print(f"rows_in={report['rows_in']} spans_out={report['spans_out']} unlocatable={report['unlocatable']}")
    print(f"invariant rows_in == spans_out + unlocatable: {report['rows_in']} == {report['spans_out']} + {report['unlocatable']}")
    print(f"flagged (head_named_in_span): {report['flagged']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
