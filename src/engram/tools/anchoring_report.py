#!/usr/bin/env python3
"""anchoring_report -- read-only hard-token anchoring measurement (engram-alpha#2602 facet 1).

Reports, per `quote_type`, how well observation/prediction claims are
anchored to their own `quoted_text` -- see engram_anchoring.py's module
docstring for what "anchored" means and its accepted bound. Read-only:
this tool measures and reports; it never writes to the graph and never
refuses anything (that's facet 3, deliberately out of scope here).

Usage:
    python anchoring_report.py [--db PATH] [--quote-type T] [--top N]
                                [--similarity] [--format human|json]

    --db PATH         Path to knowledge.db. Default: ENGRAM_HOME (or
                       ~/.engram)/knowledge.db, via engram_core.DATA_DIR.
    --quote-type T    Restrict to one quote_type (e.g. hard_data).
    --top N           Worst-N list size. Default 20.
    --similarity      Also compute cosine(embed(quoted_text), embed(claim))
                       per node (one embed call per node -- opt-in because
                       it's not free). Falls back to null columns + one
                       stderr line when embeddings are disabled or the
                       embedder errors; never fabricates a 0.
    --format human|json   Default human.

Population: `type IN ('observation_factual', 'observation_predictive')
AND is_current = 1`. The population count is always printed / returned so
every report's bound is visible.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate SSoT modules -- support both "run as module" and direct invocation
# (same pattern as tools/cohort_dispatch.py).
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import engram_core as core  # noqa: E402
from engram_anchoring import anchor, quote_claim_similarity  # noqa: E402

_POPULATION_TYPES = ("observation_factual", "observation_predictive")


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_population(conn: sqlite3.Connection, quote_type: str | None) -> list[sqlite3.Row]:
    query = (
        "SELECT id, quote_type, confidence, claim, quoted_text, interpretation "
        "FROM nodes WHERE type IN (?, ?) AND is_current = 1"
    )
    params: list = list(_POPULATION_TYPES)
    if quote_type:
        query += " AND quote_type = ?"
        params.append(quote_type)
    return conn.execute(query, params).fetchall()


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _row_result(row: sqlite3.Row, compute_similarity: bool) -> dict:
    claim = row["claim"] or ""
    quoted_text = row["quoted_text"] or ""
    interpretation = row["interpretation"] or ""
    a = anchor(claim, quoted_text)
    body_to_quote_ratio = (len(claim) + len(interpretation)) / max(len(quoted_text), 1)

    similarity = None
    similarity_reason = None
    if compute_similarity:
        similarity, similarity_reason = quote_claim_similarity(quoted_text, claim)

    return {
        "id": row["id"],
        "quote_type": row["quote_type"],
        "confidence": row["confidence"],
        "hard_tokens": a.hard_tokens,
        "anchored": a.anchored,
        "ratio": a.ratio,
        "unanchored": a.unanchored,
        "quote_len": len(quoted_text),
        "claim_len": len(claim),
        "claim_plus_interp_len": len(claim) + len(interpretation),
        "body_to_quote_ratio": body_to_quote_ratio,
        "similarity": similarity,
        "similarity_reason": similarity_reason,
    }


def _build_by_quote_type_table(results: list[dict]) -> dict:
    by_type: dict[str, list[dict]] = {}
    for r in results:
        by_type.setdefault(r["quote_type"] or "", []).append(r)

    table = {}
    for qt, rows in sorted(by_type.items()):
        with_tokens = [r for r in rows if r["hard_tokens"] > 0]
        all_anchored = [r for r in with_tokens if r["ratio"] == 1.0]
        none_anchored = [r for r in with_tokens if r["ratio"] == 0.0]
        similarities = [r["similarity"] for r in rows if r["similarity"] is not None]
        table[qt] = {
            "n": len(rows),
            "with_hard_tokens": len(with_tokens),
            "all_anchored": len(all_anchored),
            "none_anchored": len(none_anchored),
            "median_ratio": _median([r["ratio"] for r in with_tokens]),
            "median_quote_len": _median([r["quote_len"] for r in rows]),
            "median_claim_plus_interp_len": _median([r["claim_plus_interp_len"] for r in rows]),
            "median_body_to_quote_ratio": _median([r["body_to_quote_ratio"] for r in rows]),
        }
        if similarities:
            table[qt]["median_similarity"] = _median(similarities)
    return table


def _worst_n(results: list[dict], top: int) -> list[dict]:
    def sort_key(r: dict):
        ratio_for_sort = r["ratio"] if r["ratio"] is not None else 1.0
        confidence_for_sort = r["confidence"] if r["confidence"] is not None else 0.0
        return (-len(r["unanchored"]), ratio_for_sort, -confidence_for_sort)

    ordered = sorted(results, key=sort_key)
    worst = []
    for r in ordered[:top]:
        worst.append(
            {
                "id": r["id"],
                "quote_type": r["quote_type"],
                "confidence": r["confidence"],
                "hard_tokens": r["hard_tokens"],
                "anchored": r["anchored"],
                "ratio": r["ratio"],
                "quote_len": r["quote_len"],
                "body_to_quote_ratio": r["body_to_quote_ratio"],
                "unanchored": r["unanchored"][:8],
            }
        )
    return worst


def _print_human(population: int, table: dict, worst: list[dict]) -> None:
    print(f"Population (observation_factual + observation_predictive, is_current=1): {population}")
    print()
    header = (
        f"{'quote_type':<24}{'n':>6}{'w/tok':>7}{'all_anch':>9}{'none_anch':>10}"
        f"{'med_ratio':>10}{'med_qlen':>9}{'med_clen':>9}{'med_b2q':>9}{'med_sim':>9}"
    )
    print(header)
    for qt, stats in table.items():
        med_ratio = f"{stats['median_ratio']:.2f}" if stats["median_ratio"] is not None else "-"
        med_sim = (
            f"{stats['median_similarity']:.2f}" if stats.get("median_similarity") is not None else "-"
        )
        print(
            f"{qt:<24}{stats['n']:>6}{stats['with_hard_tokens']:>7}{stats['all_anchored']:>9}"
            f"{stats['none_anchored']:>10}{med_ratio:>10}{stats['median_quote_len']:>9.0f}"
            f"{stats['median_claim_plus_interp_len']:>9.0f}{stats['median_body_to_quote_ratio']:>9.2f}"
            f"{med_sim:>9}"
        )
    print()
    print(f"Worst {len(worst)} (sorted: unanchored desc, ratio asc, confidence desc):")
    for r in worst:
        ratio_str = f"{r['ratio']:.2f}" if r["ratio"] is not None else "-"
        print(
            f"  {r['id']}  {r['quote_type']}  conf={r['confidence']}  "
            f"tokens={r['hard_tokens']} anchored={r['anchored']} ratio={ratio_str}  "
            f"qlen={r['quote_len']} b2q={r['body_to_quote_ratio']:.1f}  "
            f"unanchored={r['unanchored']}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=None, help="Path to knowledge.db (default: ENGRAM_HOME/knowledge.db)")
    parser.add_argument("--quote-type", default=None, help="Restrict to one quote_type")
    parser.add_argument("--top", type=int, default=20, help="Worst-N list size (default 20)")
    parser.add_argument("--similarity", action="store_true", help="Also compute quote/claim cosine similarity")
    parser.add_argument("--format", choices=("human", "json"), default="human")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else (core.DATA_DIR / "knowledge.db")

    conn = _open_readonly(db_path)
    rows = _fetch_population(conn, args.quote_type)
    results = [_row_result(row, compute_similarity=args.similarity) for row in rows]

    similarity_computed = args.similarity
    if args.similarity:
        # If every attempted computation failed/unavailable, surface it once.
        reasons = {r["similarity_reason"] for r in results if r["similarity_reason"]}
        if reasons and all(r["similarity"] is None for r in results):
            similarity_computed = False
            print(f"anchoring_report: similarity unavailable -- {sorted(reasons)[0]}", file=sys.stderr)

    table = _build_by_quote_type_table(results)
    worst = _worst_n(results, args.top)

    conn.close()

    if args.format == "json":
        payload = {
            "population": len(results),
            "by_quote_type": table,
            "worst": worst,
            "similarity_computed": similarity_computed,
            "db": str(db_path),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        print(json.dumps(payload, indent=2))
    else:
        _print_human(len(results), table, worst)

    return 0


if __name__ == "__main__":
    sys.exit(main())
