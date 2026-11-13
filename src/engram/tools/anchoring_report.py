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
                       ~/.engram)/knowledge.db, via engram_core.DATA_DIR ($ENGRAM_HOME).
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

Reading the population (#3881, U18 of #2652): the whole current-observation population is read through the
shared owner-read helper (tools/_owner_read.py), in keyset pages by id (PAGE_ROWS rows a page, a literal
LIMIT in the registered statement), so no single owner reply carries the whole store. Behind
engramd.anchoring_report_reads_via_owner (default OFF) the pages come from the engramd owner; with the flag
OFF the same paging runs on a `mode=ro` connection, so the two differ in the connection only.
tools/quote_quality_survey.py reads the same population through the same function and rides the same flag.
With the flag ON and the owner down, refusing or answering malformed the tool prints NOTHING on stdout, one
stderr line, and exits 3: never an empty or partial report (that would read as "no observations"), never a
fallback to a direct open. Exit codes: 0 ok, 1 the store was not found, 2 a RELATIVE ENGRAM_HOME (refused,
alpha #3863), 3 the population could not be read honestly.

Order of the worst-N list: unanchored count desc, ratio asc, confidence desc, then node id asc. The id tiebreak
(#3881) makes rows that tie on the first three parts print by id, not in whatever order the store returned
them; flag OFF output therefore differs from the pre-#3881 output only where rows tie on the first three parts:
in their order, and, when `--top N` cuts through a group of tied rows, in which of them fill the last slots (now
the lowest ids).
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

from tools import _owner_read as owner_read  # noqa: E402

OWNER_FLAG_KEY = "anchoring_report_reads_via_owner"
OWNER_READ_FAILED_EXIT = 3  # 1 means "store not found"; argparse and a relative ENGRAM_HOME use 2
BAD_HOME_EXIT = 2

if __name__ == "__main__":
    # engram_core resolves ENGRAM_HOME when it is imported and raises on a RELATIVE value (alpha #3863), which
    # would end this run in a traceback; refuse it here, before that import, with one stderr line.
    try:
        owner_read.engram_home()
    except ValueError as _exc:
        print(f"ERROR: {' '.join(str(_exc).split())}", file=sys.stderr)
        sys.exit(BAD_HOME_EXIT)

import engram_core as core  # noqa: E402
from engram_anchoring import anchor, quote_claim_similarity  # noqa: E402

# The population is read PAGE_ROWS rows at a time, by id. The number is a judgement from one measured store
# (2,434 current observations): JSON-encoded rows average 1,590 bytes and the largest seen is 5,154, so a page
# is about 318 KB at the mean and at most about 1.03 MB if every row were the largest seen, about 10% of the
# owner client's 10 MiB reply cap.
PAGE_ROWS = 200

# The two statements the tool sends, registered byte for byte in src/engram/engramd_read_templates.json under
# the site tools/anchoring_report.py:_fetch_population. The production owner admits a `?` only after a
# comparison operator and has no path for a bound LIMIT, so the two observation types and the page size are
# LITERALS in the SQL bytes; the only placeholders are `quote_type = ?` and the cursor `id > ?`. Changing
# PAGE_ROWS therefore re-keys both templates, by design (a registered template's bytes fix its meaning).
# The read-template scanner reads a plain string literal, not one built by an f-string, so the number is
# written out here and pinned to PAGE_ROWS by the guard below (and by tests/test_anchoring_report_owner_3881.py
# cells T3 and T4, which also classify both statements with the production classifier).
# Eight columns: this tool reads six; metadata and created_at are read only by tools/quote_quality_survey.py
# (#4061), which shares this one function. The owner's read connection has no row_factory, so rows are turned
# into dicts BY POSITION from _COLUMNS on both paths.
# nodes.id is `TEXT PRIMARY KEY` with no COLLATE clause (BINARY), so `id > ?` and `ORDER BY id` agree.
_COLUMNS = ("id", "quote_type", "confidence", "claim", "quoted_text", "interpretation", "metadata", "created_at")
_POPULATION_SQL_ALL = (
    "SELECT id, quote_type, confidence, claim, quoted_text, interpretation, metadata, created_at "
    "FROM nodes WHERE type IN ('observation_factual', 'observation_predictive') AND is_current = 1 "
    "AND id > ? ORDER BY id LIMIT 200"
)
_POPULATION_SQL_BY_TYPE = (
    "SELECT id, quote_type, confidence, claim, quoted_text, interpretation, metadata, created_at "
    "FROM nodes WHERE type IN ('observation_factual', 'observation_predictive') AND is_current = 1 "
    "AND quote_type = ? AND id > ? ORDER BY id LIMIT 200"
)
if not (_POPULATION_SQL_ALL.endswith(f" LIMIT {PAGE_ROWS}") and _POPULATION_SQL_BY_TYPE.endswith(f" LIMIT {PAGE_ROWS}")):
    raise RuntimeError("PAGE_ROWS and the LIMIT literal in the population templates disagree: change both, "
                       "and re-register the templates (tools/engramd_read_scan.py)")


class AnchoringReportOwnerReadError(RuntimeError):
    """The population could not be read honestly (the owner is down, refused or answered malformed with the flag
    ON; a store that cannot be read as a store with it OFF). Raised instead of returning a short list: an empty
    or partial population would read downstream as "no observations", an answer the store never gave."""


def _owner_spec() -> "owner_read.OwnerReadSpec":
    return owner_read.OwnerReadSpec(
        flag_key=OWNER_FLAG_KEY,
        error_cls=AnchoringReportOwnerReadError,
        home=owner_read.engram_home(),
        never_empty="Not reporting an empty or partial population: that would read as 'no observations'.",
    )


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """A direct `mode=ro` connection with sqlite3.Row rows. NOT the population reader (that is
    `_open_population_reader`); kept direct-only for tools/s2_clause_sheet.py, which borrows it through the
    survey and makes two reads of its own on it (alpha #4286 tracks moving those)."""
    conn = sqlite3.connect(Path(db_path).absolute().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _open_population_reader(db_path: Path):
    """The connection the population is read on: the owner shim when engramd.anchoring_report_reads_via_owner
    is ON and `db_path` is this home's store (the owner must answer a ping first), else the helper's `mode=ro`
    direct open. Holds no connect call of its own: the one decision between the two lives in the helper."""
    db_path = Path(db_path)
    spec = _owner_spec()
    use_owner = owner_read.flag_applies(spec, db_path)
    if use_owner:
        owner_read.ping(spec)
    return owner_read.open_reader(spec, db_path, use_owner)


def _is_str_or_none(cell) -> bool:
    return cell is None or isinstance(cell, str)


def _checked_page(rows, cursor: str) -> list[dict]:
    """One page as dicts keyed by _COLUMNS. Anything that does not fit raises ValueError (the caller turns it
    into the typed error): not a list, more than PAGE_ROWS rows, a row that is not len(_COLUMNS) cells, an id
    that is not a str, ids that do not strictly increase from above the cursor (this also ends a loop against
    an owner that repeats a page), a cell of the wrong type (a BLOB included)."""
    if not isinstance(rows, (list, tuple)):
        raise ValueError("the answer is not a list of rows")
    if len(rows) > PAGE_ROWS:
        raise ValueError(f"{len(rows)} rows for a page of at most {PAGE_ROWS}")
    page, previous = [], cursor
    for row in rows:
        if isinstance(row, (str, bytes, dict)) or not isinstance(row, (list, tuple)) or len(row) != len(_COLUMNS):
            raise ValueError(f"a row is not {len(_COLUMNS)} cells")
        cells = dict(zip(_COLUMNS, row))
        if not isinstance(cells["id"], str):
            raise ValueError("a row's id is not a string")
        if cells["id"] <= previous:
            raise ValueError("the ids do not strictly increase from above the cursor")
        previous = cells["id"]
        conf = cells["confidence"]
        if conf is not None and (isinstance(conf, bool) or not isinstance(conf, (int, float))):
            raise ValueError("a row's confidence is neither a number nor NULL")
        for name in ("quote_type", "claim", "quoted_text", "interpretation", "metadata", "created_at"):
            if not _is_str_or_none(cells[name]):
                raise ValueError(f"a row's {name} is neither text nor NULL")
        page.append(cells)
    return page


def _fetch_population(conn, quote_type: str | None) -> list[dict]:
    """Every current observation (optionally of one quote_type), read PAGE_ROWS rows at a time by id on `conn`
    (whatever `_open_population_reader` returned: the paging is the same for both flag states). Raises
    AnchoringReportOwnerReadError for any page that cannot be read or does not fit, never returning the pages
    read so far."""
    spec = _owner_spec()
    base = [quote_type] if quote_type else []
    population: list[dict] = []
    cursor = ""
    while True:
        try:
            # Two call sites, each naming its module-level statement: the read-template scanner resolves a
            # call-site literal or one module constant, not a variable chosen by a branch.
            if quote_type:
                rows = conn.execute(_POPULATION_SQL_BY_TYPE, base + [cursor]).fetchall()
            else:
                rows = conn.execute(_POPULATION_SQL_ALL, base + [cursor]).fetchall()
        except sqlite3.Error as exc:
            raise AnchoringReportOwnerReadError(
                f"the store could not be read as a store ({exc}); check the store, or retry if another process "
                f"holds a lock on it. {spec.never_empty}"
            ) from exc
        try:
            # The lower bound is the last id RECEIVED, not the cursor just sent: a page that does not start above
            # what was already received (a repeated page, a cursor that did not advance) is malformed, so the loop
            # always ends in a page shorter than PAGE_ROWS or in the typed error.
            page = _checked_page(rows, population[-1]["id"] if population else "")
        except ValueError as exc:
            raise AnchoringReportOwnerReadError(
                f"a page of the population does not match the node schema ({exc}); nothing was reported. If the "
                f"read goes through the engramd owner, retry once it is healthy or set engramd.{spec.flag_key} "
                f"to false to read the store directly; otherwise check the store. {spec.never_empty}"
            ) from exc
        population.extend(page)
        if len(page) < PAGE_ROWS:
            return population
        cursor = page[-1]["id"]


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _row_result(row, compute_similarity: bool) -> dict:
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
        # The node id is the LAST part (#3881): rows tied on the first three print by ascending id on both paths.
        return (-len(r["unanchored"]), ratio_for_sort, -confidence_for_sort, r["id"])

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

    # NOTHING is printed until every page is in: a failure on any page ends the run with no report.
    try:
        conn = _open_population_reader(db_path)
        try:
            rows = _fetch_population(conn, args.quote_type)
        finally:
            conn.close()
    except FileNotFoundError:
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 1
    except (owner_read.StoreUnusable, sqlite3.Error) as exc:
        print(f"ERROR: AnchoringReportOwnerReadError: the store at {db_path} could not be read ("
              f"{' '.join(str(exc).split())}); fix the path or the store. {_owner_spec().never_empty}",
              file=sys.stderr)
        return OWNER_READ_FAILED_EXIT
    except AnchoringReportOwnerReadError as exc:
        print(f"ERROR: {type(exc).__name__}: {' '.join(str(exc).split())}", file=sys.stderr)
        return OWNER_READ_FAILED_EXIT
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
