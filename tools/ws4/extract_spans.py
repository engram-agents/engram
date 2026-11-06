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

Opt-in exploratory sub-agent stratum (engram-alpha#2753, the design owner's
D11 ruling of 2026-11-06): with --subagent-stratum, each SAMPLED row that
ends as call_not_located above is looked up in the sub-agent transcripts of
its OWN session -- <dir of transcript_path>/<session-id>/subagents/
agent-<id>.jsonl -- by tool_use_id only (no timestamp fallback). Exactly one
file matches: the span is cut from it (that file's own agentId records are
the acting agent; see transcript.is_excluded). None: reason call_not_located.
More than one: reason ambiguous_subagent. Everything lands ONLY under
<out>/subagent/ (spans/, key.jsonl, flags.jsonl, unlocatable.jsonl, and the
analyst-only stratum.jsonl with agent type / model / file name); every
top-level output stays byte-identical to a run without the flag, and the
rows stay unlocatable in the primary denominators. The span header carries
the fire id only. Without the flag nothing changes.

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

from .. import _owner_read as owner_read  # the ONE ENGRAM_HOME definition (#3863); a package-relative import, like .constants
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
    get_message_content,
    build_span,
    compute_window,
    find_fired_tool_use,
    find_tripwire_attachment,
    head_named_in_span,
    load_transcript,
)

ADJUDICATION_ARMS = {"chain", "control"}


def default_ledger_path() -> str:
    """$ENGRAM_HOME/tripwire-fire-ledger.jsonl through the one resolver (#3863): a RELATIVE ENGRAM_HOME raises
    ValueError naming it (main() turns that into a one-line refusal, exit 2)."""
    return str(owner_read.engram_home() / "tripwire-fire-ledger.jsonl")


def _extract_from_records(row: dict, fire_id: str, records: list, k: int, redact_chain: bool, own_agent_id=None):
    """Locate-and-cut over already-loaded records (shared by the main path
    and the sub-agent stratum; own_agent_id None = main-transcript mode)."""
    fired = find_fired_tool_use(
        records, row.get("tool_use_id"), row.get("tool_name"), row.get("ts"), own_agent_id=own_agent_id
    )
    if fired is None:
        return {"fire_id": fire_id, "ok": False, "reason": "call_not_located"}

    attachment = find_tripwire_attachment(records, fired, own_agent_id=own_agent_id)
    if attachment is None:
        return {
            "fire_id": fire_id,
            "ok": False,
            "reason": "tripwire_attachment_not_found",
            "locate_basis": fired.locate_basis,
        }

    window = compute_window(records, fired, k, own_agent_id=own_agent_id)
    span_text = build_span(records, fired, window, fire_id, k, redact_chain=redact_chain, own_agent_id=own_agent_id)
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

    return _extract_from_records(row, fire_id, records, k, redact_chain)


# --- opt-in sub-agent stratum (D11 ruling, contract items a to g) -----------


def subagents_dir_for(transcript_path):
    """<dir>/<session-id>/subagents for a main transcript <dir>/<session-id>.jsonl
    -- derived from the row's OWN transcript_path, never another session's.
    None when the path is not a usable string."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    p = Path(transcript_path)
    if ".." in p.parts:
        # A path that navigates is not provably this row's own session:
        # the resolve()-containment check below is self-consistent for it
        # (the directory it names resolves to itself), so refuse it here.
        return None
    return p.parent / p.stem / "subagents"


def _subagent_id_from_name(path: Path) -> str:
    """agent-<id>.jsonl -> <id> (the file's own agentId)."""
    return path.stem[len("agent-"):]


def _file_has_tool_use(records: list, agent_id: str, tool_use_id: str) -> bool:
    """True if tool_use_id is the id of a tool_use block in an assistant
    record of this file's own agent (match by id only; no timestamp
    fallback)."""
    for rec in records:
        if rec.get("agentId") != agent_id or rec.get("type") != "assistant":
            continue
        for item in get_message_content(rec):
            if isinstance(item, dict) and item.get("type") == "tool_use" and item.get("id") == tool_use_id:
                return True
    return False


def locate_subagent_files(row: dict) -> list:
    """All (path, agent_id, records) in the row's own session's subagents/
    directory whose own-agent assistant records carry the row's
    tool_use_id. Empty when the row has no tool_use_id, the directory does
    not exist, or nothing matches."""
    tool_use_id = row.get("tool_use_id")
    sub_dir = subagents_dir_for(row.get("transcript_path"))
    if not tool_use_id or sub_dir is None or not sub_dir.is_dir():
        return []
    root = sub_dir.resolve()
    matches = []
    for path in sorted(sub_dir.glob("agent-*.jsonl")):
        # Own session only, enforced: a match whose resolved path lies
        # outside this session's resolved subagents/ (a symlink to another
        # session's file) is skipped, not followed.
        try:
            path.resolve().relative_to(root)
        except ValueError:
            continue
        agent_id = _subagent_id_from_name(path)
        try:
            records = load_transcript(path)
        except TranscriptReadError:
            continue
        if _file_has_tool_use(records, agent_id, tool_use_id):
            matches.append((path, agent_id, records))
    return matches


def _stratum_facts(path, agent_id, records):
    """Analyst-only: agent type (meta.json beside the file) and model (first
    assistant record that names one)."""
    agent_type = None
    meta_path = Path(path).with_name(f"agent-{agent_id}.meta.json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if isinstance(meta, dict) and isinstance(meta.get("agentType"), str):
            agent_type = meta["agentType"]
    except (OSError, ValueError):
        pass
    model = None
    for rec in records:
        if rec.get("type") == "assistant":
            m = (rec.get("message") or {}).get("model")
            if isinstance(m, str):
                model = m
                break
    return agent_type, model


def extract_subagent_one(row: dict, k: int, redact_chain: bool = REDACT_CHAIN):
    """The stratum lookup for one row that ended call_not_located in its main
    transcript. Returns the extract_one dict plus agent_type / model /
    subagent_file (None where unknown). Never raises."""
    fire_id = compute_fire_id(row)
    base = {"agent_type": None, "model": None, "subagent_file": None}
    if not row.get("tool_use_id"):
        return {"fire_id": fire_id, "ok": False, "reason": "call_not_located", **base}
    matches = locate_subagent_files(row)
    if not matches:
        return {"fire_id": fire_id, "ok": False, "reason": "call_not_located", **base}
    if len(matches) > 1:
        return {"fire_id": fire_id, "ok": False, "reason": "ambiguous_subagent", **base}
    path, agent_id, records = matches[0]
    agent_type, model = _stratum_facts(path, agent_id, records)
    result = _extract_from_records(row, fire_id, records, k, redact_chain, own_agent_id=agent_id)
    result.update({"agent_type": agent_type, "model": model, "subagent_file": path.name})
    return result


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
    subagent_stratum: bool = False,
) -> dict:
    rows = load_jsonl(ledger_path)
    selected = rows_for_date_and_arms(rows, date_str, ADJUDICATION_ARMS)

    out = Path(out_dir)
    if subagent_stratum:
        # Checked BEFORE the first write of any kind, so a refused run
        # leaves no half-written stratum and touches no top-level output.
        _require_fresh_stratum_dir(out)
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
    # Rows that ended call_not_located in their own main transcript -- the
    # only rows the opt-in sub-agent stratum ever looks at.
    stratum_candidates = []

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
            if result["reason"] == "call_not_located":
                stratum_candidates.append(row)
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

    subagent_report = None
    if subagent_stratum:
        subagent_report = _run_subagent_stratum(stratum_candidates, k, out, redact_chain)

    report = {
        "rows_in": eligible,
        "eligible": eligible,
        "sampled": sampled_n,
        "unsampled": unsampled_n,
        "spans_out": spans_out,
        "unlocatable": unlocatable_n,
        "flagged": sum(1 for r in flags_rows if r["head_named_in_span"]),
    }
    if subagent_report is not None:
        report["subagent"] = subagent_report
    return report


class StratumDirNotFresh(Exception):
    """<out>/subagent/ already holds files; a re-run would leave stale spans
    beside a fresh key.jsonl, and screeners read subagent/spans/."""


def _require_fresh_stratum_dir(out: Path) -> None:
    sub_dir = out / "subagent"
    if sub_dir.exists() and (not sub_dir.is_dir() or any(sub_dir.iterdir())):
        raise StratumDirNotFresh(
            f"{sub_dir} already exists and is not empty; "
            "re-run with --subagent-stratum into a fresh --out directory"
        )


def _run_subagent_stratum(candidates: list, k: int, out: Path, redact_chain: bool) -> dict:
    """Write the exploratory sub-agent stratum under <out>/subagent/ only.
    Nothing here touches a top-level output (D11 ruling item 1: the rows
    stay in the primary denominators as unlocatable)."""
    sub_dir = out / "subagent"
    spans_dir = sub_dir / "spans"
    spans_dir.mkdir(parents=True, exist_ok=True)
    key_rows, flags_rows, unlocatable_rows, stratum_rows = [], [], [], []
    spans_out = 0
    for row in candidates:
        fire_id = compute_fire_id(row)
        result = extract_subagent_one(row, k, redact_chain=redact_chain)
        stratum_rows.append(
            {
                "fire_id": fire_id,
                "agent_type": result["agent_type"],
                "model": result["model"],
                "subagent_file": result["subagent_file"],
            }
        )
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
        key_rows.append({"fire_id": fire_id, "arm": result["arm"]})
        flags_rows.append(
            {
                "fire_id": fire_id,
                "head_named_in_span": result["head_named_in_span"],
                "locate_basis": result["locate_basis"],
                "close_reason": result["close_reason"],
            }
        )
    write_jsonl(sub_dir / "key.jsonl", key_rows)
    write_jsonl(sub_dir / "flags.jsonl", flags_rows)
    write_jsonl(sub_dir / "unlocatable.jsonl", unlocatable_rows)
    write_jsonl(sub_dir / "stratum.jsonl", stratum_rows)
    assert len(stratum_rows) == spans_out + len(unlocatable_rows), (
        f"WS4 extract_spans sub-agent invariant violated: stratum_rows={len(stratum_rows)} "
        f"spans_out={spans_out} unlocatable={len(unlocatable_rows)}"
    )
    return {
        "stratum_rows": len(stratum_rows),
        "spans_out": spans_out,
        "unlocatable": len(unlocatable_rows),
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
    parser.add_argument(
        "--subagent-stratum",
        action="store_true",
        help=(
            "opt-in EXPLORATORY sub-agent stratum (D11 ruling): look up rows that end call_not_located "
            "in their own session's subagents/ directory and write them ONLY under <out>/subagent/. "
            "Every top-level output is byte-identical to a run without this flag."
        ),
    )
    args = parser.parse_args(argv)

    try:
        ledger_path = args.ledger or default_ledger_path()
    except ValueError as exc:  # a relative ENGRAM_HOME: refused before the output directory or any file is touched
        print(f"extract_spans: {exc}", file=sys.stderr)
        return 2
    try:
        report = run(
            args.date, args.k, args.out, ledger_path,
            sample_n=args.sample, sample_seed=args.sample_seed,
            subagent_stratum=args.subagent_stratum,
        )
    except StratumDirNotFresh as exc:
        print(f"extract_spans: {exc}", file=sys.stderr)
        return 2

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
    sub = report.get("subagent")
    if sub is not None:
        print("\nsubagent stratum (exploratory):")
        print(f"  spans_out={sub['spans_out']} unlocatable={sub['unlocatable']}")
        print(
            "  invariant stratum_rows == spans_out + unlocatable: "
            f"{sub['stratum_rows']} == {sub['spans_out']} + {sub['unlocatable']}"
        )
        print(f"  flagged (head_named_in_span): {sub['flagged']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
