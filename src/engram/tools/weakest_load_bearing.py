#!/usr/bin/env python3
"""weakest_load_bearing -- read-only ranking of the weakest load-bearing beliefs
(engram-alpha#3921, Calibration step 4; feeds the study queue).

A *load-bearing belief* is a CURRENT, non-retracted, claim-bearing node with at
least one dependent.  The instrument ranks them by

    score = load x weakness

load(X)     number of DISTINCT, TRANSITIVE, CURRENT dependents of X.  D depends on
            X when an edge ``D -> X`` (source_id = D, target_id = X) carries a
            relation in ``engram_core.CITATION_SUPPORT_RELATIONS`` (the one-hop
            premise relations ``_direct_premise_ids`` walks).  A diamond counts
            each dependent once; cycles are guarded; a retracted or non-current
            node neither counts nor conducts.
weakness(X) 1 - confidence, EXCEPT 1.0 when X is
              contested  -- target of a ``contradicts`` edge from a current
                            contradiction node whose status is still unsettled
                            (``engram_core.UNSETTLED_CONTRADICTION_STATUSES``:
                            'active' or 'partially_resolved'; 'resolved' is
                            settled), or
              stale      -- has a ``cascade_markers`` row with status 'pending', or
              null_confidence -- a claim-bearing node whose confidence is NULL
                            (treated as weakness 1.0 and labelled, never skipped).
            Reported reason precedence: contested, stale, null_confidence; every
            applicable reason is listed in ``weakness_reasons``.

Nothing else enters the score -- not recency, not importance.

Claim-bearing = ``engram_core.CLAIM_BEARING_TYPES`` minus ``axiom``.  The set is
derived, never re-listed, so it tracks the codebase classification.

Two layers
==========
CORE.  ``rank(conn, n=5, current_turn=None)`` is a PURE READ over an already-open
connection: it opens none, issues SELECTs only, and calls no function that bumps
recall or utility counters.  Every statement is a module-level literal (the
read-template scanner resolves literals, not f-strings), with the premise
relations and the unsettled statuses travelling as ONE json parameter each (the
registry's admitted ``IN (SELECT value FROM json_each(?))`` form), so the
constants above stay the only list.

ENTRY.  ``read_ranking(db_path, n=5)`` and the CLI
(``weakest_load_bearing.py [--n N] [--json] [--db PATH]``) read ONLY through the
shared owner-read helper (tools/_owner_read.py): behind
``engramd.weakest_load_bearing_reads_via_owner`` (default OFF) the reads go to the
engramd owner; flag OFF opens the store ``mode=ro``.  With the flag ON and the
owner down, refusing or answering malformed, the entry RAISES
``WeakestLoadBearingOwnerReadError``: never an empty ranking, never a direct
fallback.  The module has no ``connect`` call of its own.  The CLI prints the
ranking and exits 0, or prints NOTHING on stdout, one ``<ClassName>: <message>``
line on stderr, and exits 1.

The store keeps no per-node recall timestamp, only ``recall_turn``; recency is
therefore reported in TURNS (``current_turn - recall_turn``) and never in days.
The core takes ``current_turn`` as a parameter (None -> turns unknown); the entry
layer supplies it from ``engram_core._get_current_turn()``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate SSoT modules -- support both "run as module" and direct invocation
# (same pattern as tools/anchoring_report.py and tools/cohort_dispatch.py).
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import engram_core as core  # noqa: E402
from tools import _owner_read as owner_read  # noqa: E402

_PREMISE_RELATIONS = tuple(sorted(core.CITATION_SUPPORT_RELATIONS))
_RELATIONS_JSON = json.dumps(list(_PREMISE_RELATIONS))

# Claim-bearing minus axiom (axioms are terminal: nothing to study or weaken).
_RANKABLE_TYPES = frozenset(core.CLAIM_BEARING_TYPES) - {"axiom"}

_UNSETTLED_JSON = json.dumps(list(core.UNSETTLED_CONTRADICTION_STATUSES))

_CLAIM_CHARS = 120
_SCORE_KEY_DIGITS = 9  # float noise must not decide a tie; id does

_HARD_DATA_FALSIFIER = "re-read the recorded measurement; re-run it if it is repeatable"

# ---------------------------------------------------------------------------
# The statements.  Each is registered byte for byte in
# src/engram/engramd_read_templates.json under the site of the ONE function that
# sends it (tools/weakest_load_bearing.py:<function>); the drift gate is
# tests/test_engramd_read_scan.py (r12).  Change a byte here and regenerate the
# list with tools/engramd_read_scan.py.  They are plain literals on purpose: the
# scanner reads literals and module constants, not f-strings or helper arguments.
# ---------------------------------------------------------------------------

_LIVE_NODES_SQL = (
    "SELECT id, type, confidence, recall_turn FROM nodes "
    "WHERE is_current = 1 AND COALESCE(status, '') != 'retracted'"
)
_PREMISE_EDGES_SQL = (
    "SELECT source_id, target_id FROM edges WHERE relation IN (SELECT value FROM json_each(?))"
)
_CONTESTED_SQL = (
    "SELECT DISTINCT e.target_id FROM edges e JOIN nodes ct ON ct.id = e.source_id "
    "WHERE e.relation = 'contradicts' AND ct.type = 'contradiction' AND ct.is_current = 1 "
    "AND ct.status IN (SELECT value FROM json_each(?))"
)
_PENDING_MARKERS_SQL = (
    "SELECT node_id, cascade_event, root_node FROM cascade_markers WHERE status = 'pending' ORDER BY id"
)
_DIRECT_PREMISES_SQL = (
    "SELECT target_id FROM edges WHERE source_id = ? "
    "AND relation IN (SELECT value FROM json_each(?)) ORDER BY target_id"
)
_NODE_DETAIL_SQL = "SELECT claim, quote_type, evidence_id FROM nodes WHERE id = ?"
_EVIDENCE_SQL = "SELECT source_url, source_title FROM nodes WHERE id = ?"


# ---------------------------------------------------------------------------
# Row checks.  The owner returns rows as positional lists whose cells the client
# has checked only as scalars, and an empty or short answer reads as "nothing is
# weak": every statement's row shape is checked HERE, on both paths, and a row
# that does not fit is an error, never a skipped row.
# ---------------------------------------------------------------------------

class MalformedAnswer(ValueError):
    """A statement's answer does not have the shape that statement returns."""


def _is_str(c) -> bool:
    return isinstance(c, str)


def _is_str_or_null(c) -> bool:
    return c is None or isinstance(c, str)


def _is_number_or_null(c) -> bool:
    return c is None or (isinstance(c, (int, float)) and not isinstance(c, bool))


def _is_int_or_null(c) -> bool:
    return c is None or (isinstance(c, int) and not isinstance(c, bool))


def _checked(rows, shape: tuple, what: str) -> list:
    """The rows as tuples, each exactly len(shape) cells, each cell passing its predicate."""
    if not isinstance(rows, (list, tuple)):
        raise MalformedAnswer(f"{what}: the answer is not a list of rows")
    out = []
    for row in rows:
        if isinstance(row, (str, bytes)) or not hasattr(row, "__len__") or len(row) != len(shape):
            raise MalformedAnswer(f"{what}: a row is not {len(shape)} cells")
        cells = tuple(row)
        for i, (cell, ok) in enumerate(zip(cells, shape)):
            if not ok(cell):
                raise MalformedAnswer(f"{what}: cell {i} has an unexpected type")
        out.append(cells)
    return out


def _one_liner(text) -> str:
    return " ".join((text or "").split())[:_CLAIM_CHARS]


def _live_nodes(conn) -> dict:
    """id -> (id, type, confidence, recall_turn) for every CURRENT, non-retracted node."""
    rows = _checked(
        conn.execute(_LIVE_NODES_SQL).fetchall(),
        (_is_str, _is_str, _is_number_or_null, _is_int_or_null),
        "live nodes",
    )
    return {r[0]: r for r in rows}


def _dependents_map(conn, live: dict) -> dict:
    """target id -> set of DIRECT live dependents (premise-relation edges only)."""
    deps: dict = {}
    rows = _checked(
        conn.execute(_PREMISE_EDGES_SQL, (_RELATIONS_JSON,)).fetchall(),
        (_is_str, _is_str),
        "premise edges",
    )
    for src, tgt in rows:
        if src in live and tgt in live:
            deps.setdefault(tgt, set()).add(src)
    return deps


def _transitive_load(node_id: str, deps: dict) -> int:
    seen = set()
    stack = list(deps.get(node_id, ()))
    while stack:
        d = stack.pop()
        if d in seen or d == node_id:
            continue
        seen.add(d)
        stack.extend(deps.get(d, ()))
    return len(seen)


def _contested_ids(conn) -> set:
    rows = _checked(
        conn.execute(_CONTESTED_SQL, (_UNSETTLED_JSON,)).fetchall(),
        (_is_str,),
        "contested nodes",
    )
    return {r[0] for r in rows}


def _pending_markers(conn) -> dict:
    """node id -> list of (cascade_event, root_node) for PENDING markers."""
    out: dict = {}
    rows = _checked(
        conn.execute(_PENDING_MARKERS_SQL).fetchall(),
        (_is_str, _is_str_or_null, _is_str_or_null),
        "pending markers",
    )
    for nid, ev, root in rows:
        out.setdefault(nid, []).append((ev, root))
    return out


def _direct_premises(conn, node_id: str) -> list:
    rows = _checked(
        conn.execute(_DIRECT_PREMISES_SQL, (node_id, _RELATIONS_JSON)).fetchall(),
        (_is_str,),
        "direct premises",
    )
    return [r[0] for r in rows]


def _node_detail(conn, node_id: str) -> tuple:
    """(claim, quote_type, evidence_id) of a node the live read just returned."""
    rows = _checked(
        conn.execute(_NODE_DETAIL_SQL, (node_id,)).fetchall(),
        (_is_str_or_null, _is_str_or_null, _is_str_or_null),
        "node detail",
    )
    if len(rows) != 1:
        raise MalformedAnswer(f"node detail: expected exactly one row for a live node, got {len(rows)}")
    return rows[0]


def _evidence_label(conn, evidence_id: str):
    """(source_url, source_title) of an evidence node, or None when there is no such node."""
    rows = _checked(
        conn.execute(_EVIDENCE_SQL, (evidence_id,)).fetchall(),
        (_is_str_or_null, _is_str_or_null),
        "evidence",
    )
    return rows[0] if rows else None


def _falsifier(conn, nid: str, ntype: str, quote_type, evidence_id, reasons: list,
               markers: dict, live: dict) -> str:
    if ntype in ("observation_factual", "observation_predictive"):
        text = _HARD_DATA_FALSIFIER if quote_type == "hard_data" else "re-read the source"
        if evidence_id:
            ev = _evidence_label(conn, evidence_id)
            if ev and (ev[0] or ev[1]):
                bits = [b for b in (ev[1], ev[0]) if b]
                text += " (" + " -- ".join(bits) + ")"
        return text

    premises = _direct_premises(conn, nid)
    if "stale" in reasons:
        events = {ev for ev, _root in markers.get(nid, [])}
        roots = {root for _ev, root in markers.get(nid, [])}
        marked = [
            p for p in premises
            if p in roots or any(ev in events for ev, _r in markers.get(p, []))
        ]
        if not marked:
            marked = sorted(r for r in roots if r)
        if marked:
            return f"check the premise's successor ({marked[0]})"
        return "check the premise's successor"

    scored = []
    for p in premises:
        prow = live.get(p)
        if prow is not None and prow[2] is not None:
            scored.append((prow[2], p))
    if scored:
        return f"re-check the weakest premise ({min(scored)[1]})"
    return "re-check the weakest premise (no scored direct premise recorded)"


def rank(conn, n: int = 5, current_turn=None) -> list:
    """Top-``n`` weakest load-bearing beliefs, as a list of dicts.

    ``conn`` is an already-open connection (a sqlite3 connection of any row_factory,
    or the owner's read shim); only SELECTs are issued.  Each dict: id, type, claim,
    confidence, load, weakness, weakness_reason (None when weakness is 1 - confidence),
    weakness_reasons, score, turns_since_recall (None when ``current_turn`` is None),
    falsifier.  Order: score descending, ties by id ascending.  A statement whose
    answer does not fit its row shape raises ``MalformedAnswer``.  ``n`` below 1
    raises ``ValueError``: an empty list would read as a statement about the graph.
    A NULL ``recall_turn`` is treated as 0 when ``turns_since_recall`` is computed.
    """
    if n < 1:
        raise ValueError(f"n must be at least 1, got {n}")
    live = _live_nodes(conn)
    deps = _dependents_map(conn, live)
    contested = _contested_ids(conn)
    markers = _pending_markers(conn)

    ranked = []
    for nid, row in live.items():
        ntype, conf = row[1], row[2]
        if ntype not in _RANKABLE_TYPES:
            continue
        load = _transitive_load(nid, deps)
        if load < 1:
            continue
        reasons = []
        if nid in contested:
            reasons.append("contested")
        if nid in markers:
            reasons.append("stale")
        if conf is None:
            reasons.append("null_confidence")
        weakness = 1.0 if reasons else 1.0 - conf
        ranked.append((nid, row, load, weakness, reasons))

    ranked.sort(key=lambda t: (-round(t[2] * t[3], _SCORE_KEY_DIGITS), t[0]))

    out = []
    for nid, row, load, weakness, reasons in ranked[:n]:
        recall_turn = row[3]
        claim, quote_type, evidence_id = _node_detail(conn, nid)
        out.append({
            "id": nid,
            "type": row[1],
            "claim": _one_liner(claim),
            "confidence": row[2],
            "load": load,
            "weakness": weakness,
            "weakness_reason": reasons[0] if reasons else None,
            "weakness_reasons": reasons,
            "score": load * weakness,
            "turns_since_recall": (
                None if current_turn is None else current_turn - (recall_turn or 0)
            ),
            "falsifier": _falsifier(conn, nid, row[1], quote_type, evidence_id, reasons, markers, live),
        })
    return out


def to_json(rows: list) -> str:
    """Machine-readable form (for the study queue)."""
    return json.dumps(rows, indent=2, sort_keys=True)


def format_text(rows: list) -> str:
    """Human-readable form.  Recency is always labelled in turns, never days."""
    if not rows:
        return "No load-bearing beliefs found."
    lines = []
    for i, r in enumerate(rows, 1):
        conf = "none" if r["confidence"] is None else f"{r['confidence']:.2f}"
        why = f" [{r['weakness_reason']}]" if r["weakness_reason"] else ""
        turns = r["turns_since_recall"]
        recall = "turns since last recall: unknown" if turns is None else f"{turns} turns since last recall"
        lines.append(
            f"{i}. {r['id']}  score {r['score']:.2f} = load {r['load']} x weakness "
            f"{r['weakness']:.2f}{why}  (confidence {conf})"
        )
        lines.append(f"   {r['claim']}")
        lines.append(f"   {recall}")
        lines.append(f"   cheapest falsifier: {r['falsifier']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry layer: the owner-read flag, the typed error, and the one read path.
# ---------------------------------------------------------------------------

OWNER_FLAG_KEY = "weakest_load_bearing_reads_via_owner"


class WeakestLoadBearingOwnerReadError(RuntimeError):
    """The owner could not give the ranking's rows (flag ON), or gave rows that do not fit. Raised instead
    of returning an empty or shorter ranking: an empty answer would read downstream as "no belief is both
    weak and load-bearing", a clean bill of health the store never gave."""


def _owner_spec() -> "owner_read.OwnerReadSpec":
    return owner_read.OwnerReadSpec(
        flag_key=OWNER_FLAG_KEY,
        error_cls=WeakestLoadBearingOwnerReadError,
        home=owner_read.engram_home(),
        never_empty="Not answering from an empty list: that would read as 'no belief is both weak and "
                    "load-bearing'.",
    )


def read_ranking(db_path, n: int = 5, use_owner: "bool | None" = None) -> list:
    """The ranking of the store at `db_path`, read through the shared owner-read helper only.

    Behind engramd.weakest_load_bearing_reads_via_owner (default OFF) the reads go to the engramd owner
    when `db_path` is this home's store; the owner's answer replaces the db_path.exists() precondition.
    Flag ON and the owner down, refusing or answering malformed RAISES WeakestLoadBearingOwnerReadError:
    never `[]`, never a shorter list, never a direct fallback. Flag OFF opens the store `mode=ro`.

    `use_owner`: None (default) reads the flag here, once; a caller that has already decided passes
    that decision (computed by `owner_read.flag_applies`, never any other way).

    `current_turn` comes from engram_core._get_current_turn(), so every row carries an integer
    turns_since_recall."""
    db_path = Path(db_path)
    spec = _owner_spec()
    if use_owner is None:
        use_owner = owner_read.flag_applies(spec, db_path)
    if use_owner:
        owner_read.ping(spec)  # the owner's answer replaces the db_path.exists() precondition
    else:
        # Kept before `_get_current_turn()` below: that reads config.json and raises on a torn one, which would
        # hide a store problem behind a config error (the open_reader check comes only after it).
        owner_read.check_store_path(db_path)
    current_turn = core._get_current_turn()
    conn = owner_read.open_reader(spec, db_path, use_owner)
    try:
        return rank(conn, n=n, current_turn=current_turn)
    except MalformedAnswer as exc:
        via = "the engramd owner answered" if use_owner else "the store answered"
        remedy = (f"retry once the owner is healthy, or set engramd.{spec.flag_key} to false to read the "
                  "store directly" if use_owner else "check the store")
        raise WeakestLoadBearingOwnerReadError(
            f"{via} a row that does not match the node schema ({exc}); {remedy}. {spec.never_empty}"
        ) from exc
    finally:
        conn.close()


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--n must be an integer, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"--n must be at least 1, got {value}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="weakest_load_bearing.py",
        description="The weakest load-bearing beliefs, ranked by load x weakness. Read-only. The ranking on "
                    "stdout and exit 0, or NOTHING on stdout and a non-zero exit with the error class on stderr.",
    )
    ap.add_argument("--n", type=_positive_int, default=5, help="how many rows, at least 1 (default 5)")
    ap.add_argument("--json", action="store_true", help="machine-readable output (for the study queue)")
    ap.add_argument("--db", default=str(owner_read.default_db_path()),
                    help="the store to read (default: this ENGRAM_HOME's knowledge.db)")
    return ap


def main(argv: "list[str] | None" = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        rows = read_ranking(Path(args.db), n=args.n)
        text = to_json(rows) if args.json else format_text(rows)
    except Exception as exc:  # every failure is loud and empty on stdout; KeyboardInterrupt propagates
        sys.stderr.write(f"{type(exc).__name__}: {' '.join(str(exc).split())}\n")
        return 1
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
