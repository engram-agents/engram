#!/usr/bin/env python3
"""Propose `generalizes` edges from today's principle-to-principle edges.

WS3 Deliverable A of epic #2753 (D5 — the one-shot, adjudicated migration).

THE SCRIPT PROPOSES; THE AGENT DISPOSES.
=======================================
This script is READ-ONLY and cannot be made otherwise from the command
line. It opens the graph with ``file:...?mode=ro`` and emits a markdown
report. There is deliberately NO ``--apply`` flag, no bulk-acceptance path,
and no write path of any kind (spec R2, Lei's ruling 5): a flag is one typo
away from auto-wiring a whole graph's abstraction structure, and the epic's
own line is *"wrong abstraction is worse than none."*

Acceptance is an agent hand-running one ``engram_add_edge(source,
'generalizes', target)`` call per accepted row — which routes through WS1's
endpoint whitelist AND its write-time cycle guard. Every proposed row in
the report carries that call verbatim as a runnable ``accept:`` line, so the
adjudicator never hand-assembles the direction (see R1 below — the direction
is exactly what a human would get wrong).

R1 — DIRECTION COMES FROM THE TYPE PAIR, NEVER FROM THE SOURCE RELATION
=======================================================================
`generalizes` runs general -> specific. The obvious implementation reads the
source relation and emits a fixed direction per relation. Measured on the
author's graph, against the candidate population this script itself emits
(34 candidates, of which 19 are TYPE-ORDER): **12 of those 19 point the
opposite way from every edge that nominated them.** A relation-keyed
implementation would write 12 backwards edges into a live graph::

    cornerstone supported_by lesson   ->  cornerstone generalizes lesson   (same)
    cornerstone supported_by axiom    ->  axiom generalizes cornerstone    (REVERSED)

Those two lines are illustrative, not exhaustive, and the reversals are NOT
confined to ``supported_by``: counted over raw current edges they are 9
``supported_by`` (both the axiom and the goal targets), 6 ``cites`` and 1
``serves``. Note the two denominators differ on purpose -- candidate PAIRS
are deduped, raw EDGES are not -- so quote whichever you mean by name.

Same relation, opposite directions, because the generality order is a
property of the TYPES, not of the edge. The source relation's only job is to
NOMINATE the pair.

The type-level generality order is DERIVED from the endpoint whitelist
rather than restated here (see ``derive_generality_rank``); on the shipped
whitelist it comes out as ``axiom, goal > cornerstone > lesson``.

Pairs whose two types sit at the SAME generality rank (``lesson<->lesson``,
``cornerstone<->cornerstone``) have no type-level answer. They are reported
as AMBIGUOUS for the adjudicating agent to decide, and the script NEVER
guesses. The source edge's own direction is shown for those, explicitly
labelled a hint: ``supported_by`` carries confidence and cascade,
``generalizes`` carries neither, and they are not the same claim.

R3 — ENDPOINT-REFUSED IS A REPORTED TIER, NEVER A SILENT FILTER
===============================================================
Some measured edges imply endpoints D1's whitelist does not permit (e.g.
``lesson cites axiom`` implies ``axiom generalizes lesson``). A migration
script that filters to the whitelist and says nothing produces a clean
report that hides the most interesting question. These are REPORTED under
ENDPOINT-REFUSED with counts.

Kepler ruled (2026-09-11, #2753) that the whitelist's omission is
DELIBERATE — abstraction proceeds one type-rung at a time, so an axiom
reaches a lesson only THROUGH a cornerstone. The refused rows are therefore
**the authoring worklist, not a loss**: the fix for a ``lesson cites axiom``
candidate is to mint the intermediate cornerstone, then write two edges.
Lei has not ruled on this and Kepler named it an overturnable point.

    *** THE SEAM ***
    If that ruling is overturned, the ONLY change needed is adding the
    pair(s) to ``GENERALIZES_ALLOWED_PAIRS`` in ``src/engram/engram_core.py``.
    Nothing in this script is restructured: rows move from ENDPOINT-REFUSED
    to TYPE-ORDER on their own, because the whitelist is read live from
    engram_core (never copied), the generality order is derived FROM that
    whitelist, and the tier boundary is the single ``in allowed`` test in
    ``classify_pair``.

D5 VOCABULARY NOTE (measured, not assumed)
==========================================
The epic's D5 names four source patterns. A full principle->principle edge
histogram — run instead of a per-pattern probe, which is what made the empty
ones legible as empty — shows that two of them match ZERO edges:
``lesson instantiates cornerstone`` and ``lesson instantiates axiom``.
``instantiates`` never appears with a principle node on either end, which is
exactly what D1 says it should do: it is a CONCRETE -> principle relation,
one of the example anchors a ``generalization_chain`` walk starts FROM. D5's
wording was written from the concrete-leg vocabulary.

The measured principle->principle patterns that actually nominate
candidates are ``supported_by`` and ``cites`` between principle nodes (plus
``serves``), which is why this script enumerates the FULL principle->principle
edge set by node type rather than looping over a hardcoded pattern list —
a hardcoded list is how D5 acquired two patterns that match nothing.

Usage::

    python3 tools/propose-generalizes-migration.py --out /tmp/proposal.md
    python3 tools/propose-generalizes-migration.py --db /path/to/knowledge.db \
        --out report.md --no-include-ambiguous
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# --- import bootstrap -------------------------------------------------------
# engram_core lives at src/engram/engram_core.py in the dev tree and beside
# tools/ in the deployed plugin bundle. Probe both; append (never insert(0))
# per the #2565 tools/-package collision discipline documented in
# tests/conftest.py and tools/surgical.py.
_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE.parent / "src" / "engram", _HERE.parent):
    if (_candidate / "engram_core.py").exists():
        if str(_candidate) not in sys.path:
            sys.path.append(str(_candidate))
        break

import engram_core as core  # noqa: E402


# --- tiers ------------------------------------------------------------------

TIER_TYPE_ORDER = "TYPE-ORDER"
TIER_AMBIGUOUS = "AMBIGUOUS"
TIER_REFUSED = "ENDPOINT-REFUSED"

# Report order. Not alphabetical — TYPE-ORDER first because it is the only
# tier carrying runnable accept: lines.
TIER_ORDER = (TIER_TYPE_ORDER, TIER_AMBIGUOUS, TIER_REFUSED)

TIER_BLURB = {
    TIER_TYPE_ORDER: (
        "Cross-type pair; direction determined by the type-level generality "
        "order (R1). Adjudicate for CORRECTNESS OF THE ABSTRACTION, not for "
        "direction — the direction is already settled and the `accept:` line "
        "carries it. The question is *is the general one genuinely the more "
        "general form of the specific one?*, not *are these related?* — "
        "`cites` already covers related."
    ),
    TIER_AMBIGUOUS: (
        "Same generality rank; the types give NO answer, so the script does "
        "not guess. Adjudicate DIRECTION **and** correctness, then write the "
        "edge by hand. The source edge's own direction is shown as a HINT "
        "only — `supported_by` carries confidence and cascade, `generalizes` "
        "carries neither, and they are not the same claim."
    ),
    TIER_REFUSED: (
        "The implied endpoint pair is not in `GENERALIZES_ALLOWED_PAIRS` "
        "(#2753 D1; Kepler's 2026-09-11 ruling: type-monotone rungs are "
        "deliberate). Reported with counts rather than silently filtered. "
        "**This is the authoring worklist, not a loss** — the fix is usually "
        "to mint the missing intermediate node and then write two edges. "
        "No `accept:` line is emitted; `engram_add_edge` would refuse these "
        "anyway."
    ),
}

# Relations excluded BY NAME from nomination.
#
#   supersedes / retracts — version and validity transitions, not abstraction.
#     a lesson that SUPERSEDES an earlier lesson REPLACES it; it emphatically
#     does not say the later one is the more general form of it. Migrating
#     these would write the graph's revision history into its taxonomy. (The
#     single largest wrong-answer class if they were let through — 6 such
#     edges on the graph this was measured against.)
#
# `generalizes` itself is excluded from nomination too, but separately: it is
# not a wrong nomination, it is the IDEMPOTENCY probe (step 6). Those edges
# are collected into the already-linked set instead of discarded.
EXCLUDED_NOMINATING_RELATIONS = frozenset({"supersedes", "retracts"})

MAX_SUMMARY_CHARS = 120


# --- whitelist-derived structure (the seam) ---------------------------------

def principle_types(allowed_pairs) -> frozenset:
    """The node types that can participate in `generalizes` at all.

    Derived from the live whitelist rather than hand-listed, so that a pair
    addition cannot silently desync this script from engram_core. (Mirrors
    core.GENERALIZES_PRINCIPLE_TYPES, but recomputed from the *live* value so
    a monkeypatched/overturned whitelist is honoured here too — core's own
    copy is frozen at its module import.)
    """
    return frozenset(t for pair in allowed_pairs for t in pair)


def derive_generality_rank(allowed_pairs) -> dict:
    """Derive the type-level generality order FROM the endpoint whitelist.

    R1 says the order comes from D1's allowed endpoint list. Deriving it,
    rather than restating ``axiom, goal > cornerstone > lesson`` as a
    literal, is what makes this script agree with engram_core BY
    CONSTRUCTION: the whitelist is the only place the order is declared.

    Each cross-type allowed pair ``(general, specific)`` is an edge in a
    small type DAG. A type's rank is the length of the longest path from it
    down to a sink. Same-type pairs (``lesson -> lesson``) are self-loops and
    carry no ordering information, so they are skipped — they are exactly the
    pairs R1 calls AMBIGUOUS.

    On the shipped whitelist this yields
    ``{lesson: 0, cornerstone: 1, axiom: 2, goal: 2}``.

    Raises ValueError if the cross-type pairs contain a cycle (which would
    mean the whitelist asserts two types are each more general than the
    other) — fail loud rather than emit an arbitrary order.
    """
    types = principle_types(allowed_pairs)
    successors: dict = {t: set() for t in types}
    for general, specific in allowed_pairs:
        if general == specific:
            continue  # self-loop: no ordering information
        successors[general].add(specific)

    rank: dict = {}
    visiting: set = set()

    def _rank(t: str) -> int:
        if t in rank:
            return rank[t]
        if t in visiting:
            raise ValueError(
                "GENERALIZES_ALLOWED_PAIRS contains a cycle among cross-type "
                f"pairs (at type {t!r}); no generality order exists."
            )
        visiting.add(t)
        best = 0
        for nxt in successors[t]:
            best = max(best, _rank(nxt) + 1)
        visiting.discard(t)
        rank[t] = best
        return best

    for t in sorted(types):
        _rank(t)
    return rank


def classify_pair(a_id, b_id, node_types, rank, allowed_pairs):
    """Assign a tier and (where determinable) a direction to one pair.

    Returns ``(tier, general_id, specific_id)``. ``general_id`` /
    ``specific_id`` are None when no direction is determined — AMBIGUOUS
    always, and ENDPOINT-REFUSED when the two types sit at the same rank.

    THE TIER BOUNDARY IS THE ``in allowed_pairs`` TEST BELOW. Overturning
    Kepler's ruling moves rows across it by editing the whitelist alone.
    """
    ta, tb = node_types[a_id], node_types[b_id]
    ra, rb = rank.get(ta), rank.get(tb)
    if ra is None or rb is None:
        # A type outside the principle family entirely. Unreachable via the
        # nominating query (which filters on principle types), but classified
        # rather than crashed if a caller reaches here another way.
        return TIER_REFUSED, None, None

    if ra != rb:
        # Different generality rank -> R1 settles the direction outright.
        general, specific = (a_id, b_id) if ra > rb else (b_id, a_id)
        if (node_types[general], node_types[specific]) in allowed_pairs:
            return TIER_TYPE_ORDER, general, specific
        return TIER_REFUSED, general, specific

    # Same generality rank -> the TYPES give no answer.
    ok = [
        (x, y) for (x, y) in ((a_id, b_id), (b_id, a_id))
        if (node_types[x], node_types[y]) in allowed_pairs
    ]
    if not ok:
        # e.g. axiom<->axiom, axiom<->goal on the shipped whitelist.
        return TIER_REFUSED, None, None
    # NOTE: an earlier revision branched here on "equal rank, different types,
    # whitelist permits exactly one ordering", justified as future-proofing for
    # a pair addition. That branch was UNREACHABLE and the justification was
    # wrong: the rank is DERIVED from this same whitelist by longest path, so
    # any permitted cross-type ordering necessarily creates a rank difference
    # and the equal-rank arm is never entered for it. Verified exhaustively --
    # 0 of the 543 acyclic whitelists over the four principle types reach it
    # (3,553 of 4,096 subsets are rejected by the cycle guard), against a
    # positive control that fires on a hand-built rank tie. If the rank ever
    # stops being derived, test_generality_order_follows_a_changed_whitelist
    # is what fails first.
    return TIER_AMBIGUOUS, None, None


# --- db access (read-only, structurally) ------------------------------------

def open_readonly(db_path) -> sqlite3.Connection:
    """Open the graph READ-ONLY. Fails loud on a missing file.

    Two separate guarantees, deliberately not collapsed into one:

      * ``?mode=ro`` makes writes IMPOSSIBLE at the connection level. This is
        the R2 enforcement — read-only by construction, not by discipline. No
        argument to this script can turn it off.
      * The explicit existence check fails loud on a bad path. Without it,
        ``mode=ro`` on a missing file raises a bare "unable to open database
        file", and — worse for a migration tool — any *softer* handling would
        make an empty report and an unreadable DB look alike. A migration
        that proposes nothing because it read nothing must never be
        mistakable for a migration that proposes nothing because there is
        nothing to propose.
    """
    db_path = Path(db_path).expanduser()
    if not db_path.exists():
        raise FileNotFoundError(
            f"ENGRAM database not found: {db_path}\n"
            "Refusing to emit a report — an empty report and an unreadable "
            "database must not look alike."
        )
    uri = "file:" + urllib.parse.quote(str(db_path.resolve())) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_column(conn, table, column) -> bool:
    return any(
        row["name"] == column
        for row in conn.execute(f"PRAGMA table_info({table})")
    )


def _short(text, limit=MAX_SUMMARY_CHARS) -> str:
    if not text:
        return ""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


# --- candidate collection ---------------------------------------------------

def collect_candidates(conn, include_ambiguous=True):
    """Run the 8-step algorithm. Returns ``(candidates, stats)``.

    Steps 2-7 of the spec, in order. Step 1 (open read-only, fail loud) is
    ``open_readonly``; step 8 (emit) is ``render_report``.
    """
    allowed_pairs = core.GENERALIZES_ALLOWED_PAIRS  # live read — see THE SEAM
    ptypes = principle_types(allowed_pairs)
    rank = derive_generality_rank(allowed_pairs)

    stats = {
        "principle_edges_scanned": 0,
        "excluded_by_relation": 0,
        "excluded_self_edge": 0,
        "dropped_superseded_endpoint": 0,
        "dropped_already_generalizes": 0,
        "nominating_edges_kept": 0,
        "pairs_before_idempotency": 0,
    }

    # Step 2 — the FULL principle->principle edge set, enumerated by node
    # TYPE, not by a per-pattern loop. A hardcoded pattern list is how D5
    # acquired two patterns that match zero edges; enumerating by type makes
    # an empty class visible as empty.
    placeholders = ",".join("?" for _ in ptypes)
    type_args = sorted(ptypes)
    rows = conn.execute(
        f"""
        SELECT e.source_id, e.target_id, e.relation,
               s.type AS s_type, t.type AS t_type,
               s.is_current AS s_current, t.is_current AS t_current
        FROM edges e
        JOIN nodes s ON s.id = e.source_id
        JOIN nodes t ON t.id = e.target_id
        WHERE s.type IN ({placeholders}) AND t.type IN ({placeholders})
        ORDER BY e.source_id, e.target_id, e.relation
        """,
        type_args + type_args,
    ).fetchall()

    node_types: dict = {}
    already_linked: set = set()   # frozenset({a, b}) for every generalizes pair
    nominations: dict = {}        # frozenset({a, b}) -> list of source patterns

    for row in rows:
        stats["principle_edges_scanned"] += 1
        src, tgt, rel = row["source_id"], row["target_id"], row["relation"]

        if rel == "generalizes":
            # Step 6 input, not a nomination: this pair is already linked.
            # Collected regardless of is_current — an existing edge means the
            # pair has been adjudicated, whatever state its endpoints are in.
            already_linked.add(frozenset((src, tgt)))
            continue
        if rel in EXCLUDED_NOMINATING_RELATIONS:
            stats["excluded_by_relation"] += 1
            continue
        if src == tgt:
            stats["excluded_self_edge"] += 1
            continue

        # Step 3 — is_current on BOTH endpoints. The #1195 hazard (508
        # `exemplifies` edges resolving to 162 targets against 143 active
        # lessons) is the same shape here: proposing an edge onto a
        # superseded node writes that hazard into the new relation, where
        # every future chain walk then has to resolve around it.
        if not (row["s_current"] == 1 and row["t_current"] == 1):
            stats["dropped_superseded_endpoint"] += 1
            continue

        stats["nominating_edges_kept"] += 1
        node_types[src] = row["s_type"]
        node_types[tgt] = row["t_type"]
        key = frozenset((src, tgt))
        nominations.setdefault(key, []).append((src, rel, tgt))

    stats["pairs_before_idempotency"] = len(nominations)

    # Steps 4/5/6/7, per pair.
    candidates = []
    for key, patterns in nominations.items():
        if key in already_linked:
            stats["dropped_already_generalizes"] += 1
            continue
        a_id, b_id = sorted(key)

        # Step 4 (direction / AMBIGUOUS) and step 5 (whitelist -> tier).
        tier, general, specific = classify_pair(
            a_id, b_id, node_types, rank, allowed_pairs
        )
        if tier == TIER_AMBIGUOUS and not include_ambiguous:
            continue

        # Step 7 — advisory cycle dry-run against the CURRENT graph.
        cycles = {}
        if tier == TIER_TYPE_ORDER:
            cycles[(general, specific)] = core._generalizes_cycle_path(
                conn, general, specific
            )
        elif tier == TIER_AMBIGUOUS:
            for x, y in ((a_id, b_id), (b_id, a_id)):
                cycles[(x, y)] = core._generalizes_cycle_path(conn, x, y)

        candidates.append({
            "pair": (a_id, b_id),
            "tier": tier,
            "general": general,
            "specific": specific,
            "types": {a_id: node_types[a_id], b_id: node_types[b_id]},
            "patterns": sorted(patterns),
            "cycles": cycles,
        })

    candidates.sort(key=lambda c: (TIER_ORDER.index(c["tier"]), c["pair"]))
    for n, cand in enumerate(candidates, start=1):
        cand["label"] = f"C-{n:03d}"
    return candidates, stats


def _hydrate_summaries(conn, candidates) -> dict:
    ids = sorted({nid for c in candidates for nid in c["pair"]})
    if not ids:
        return {}
    col = "recall_summary" if _has_column(conn, "nodes", "recall_summary") else "NULL"
    out = {}
    chunk = 400
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        ph = ",".join("?" for _ in part)
        for row in conn.execute(
            f"SELECT id, claim, {col} AS rs FROM nodes WHERE id IN ({ph})", part
        ):
            out[row["id"]] = (row["rs"], row["claim"])
    return out


# --- report -----------------------------------------------------------------

def _describe(nid, cand, summaries) -> str:
    rs, claim = summaries.get(nid, (None, None))
    text = _short(rs) or _short(claim) or "(no recall_summary, no claim)"
    marker = "<- recall_summary" if rs else "<- claim (no recall_summary)"
    return f'{nid} ({cand["types"][nid]}) "{text}"  {marker}'


def _cycle_line(path) -> str:
    if path is None:
        return "clean"
    return "WOULD CLOSE A CYCLE: " + " -> ".join(path)


def render_report(candidates, stats, db_path, include_ambiguous, summaries) -> str:
    by_tier = {t: [c for c in candidates if c["tier"] == t] for t in TIER_ORDER}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    out = []
    out.append("# `generalizes` migration proposal — #2753 WS3 (D5)")
    out.append("")
    out.append(f"- generated : {now}")
    out.append(f"- database  : `{db_path}` (opened **read-only**, `mode=ro`)")
    out.append(f"- ambiguous tier : {'included' if include_ambiguous else 'EXCLUDED (--no-include-ambiguous)'}")
    out.append("")
    out.append(
        "**This report proposes; it does not write.** The script has no "
        "`--apply` flag and no write path — acceptance is you running one "
        "`accept:` line below per edge you agree with, which routes through "
        "WS1's endpoint whitelist and write-time cycle guard. Rejecting is a "
        "normal outcome and needs no justification: wrong abstraction is "
        "worse than none."
    )
    out.append("")
    out.append(
        "**Do not hand-assemble the direction.** `generalizes` runs "
        "general -> specific, and its direction comes from the TYPE PAIR, "
        "never from the source relation — `cornerstone supported_by lesson` "
        "and `cornerstone supported_by axiom` migrate in OPPOSITE directions. "
        "Run the `accept:` line verbatim."
    )
    out.append("")

    out.append("## Summary")
    out.append("")
    out.append("| tier | candidates |")
    out.append("| --- | ---: |")
    for tier in TIER_ORDER:
        out.append(f"| {tier} | {len(by_tier[tier])} |")
    out.append(f"| **total** | **{len(candidates)}** |")
    out.append("")
    out.append("Denominators — a tier count means nothing without them:")
    out.append("")
    out.append(f"- principle->principle edges scanned : {stats['principle_edges_scanned']}")
    out.append(f"- excluded by relation (supersedes/retracts) : {stats['excluded_by_relation']}")
    out.append(f"- excluded self-edges : {stats['excluded_self_edge']}")
    out.append(f"- dropped, superseded endpoint : {stats['dropped_superseded_endpoint']}")
    out.append(f"- nominating edges kept : {stats['nominating_edges_kept']}")
    out.append(f"- distinct pairs nominated : {stats['pairs_before_idempotency']}")
    out.append(f"- dropped, pair already carries `generalizes` : {stats['dropped_already_generalizes']}")
    out.append("")
    out.append(
        "The cycle check below is **advisory**: it is dry-run against the "
        "graph as it stands now, and accepting several candidates changes "
        "it. WS1's write-time guard inside `engram_add_edge` is the real "
        "gate."
    )
    out.append("")

    for tier in TIER_ORDER:
        rows = by_tier[tier]
        out.append(f"## {tier} — {len(rows)} candidate(s)")
        out.append("")
        out.append(TIER_BLURB[tier])
        out.append("")
        if not rows:
            out.append("_(none)_")
            out.append("")
            continue
        for cand in rows:
            out.extend(_render_candidate(cand, summaries))
            out.append("")

    return "\n".join(out).rstrip() + "\n"


def _render_candidate(cand, summaries):
    a_id, b_id = cand["pair"]
    tier = cand["tier"]
    lines = []

    if tier == TIER_TYPE_ORDER:
        g, s = cand["general"], cand["specific"]
        lines.append(f"### {cand['label']}  {g} generalizes {s}      [direction: {tier}]")
    elif tier == TIER_AMBIGUOUS:
        lines.append(f"### {cand['label']}  {a_id} <-> {b_id}      [direction: {tier}]")
    else:
        if cand["general"]:
            lines.append(
                f"### {cand['label']}  {cand['general']} generalizes "
                f"{cand['specific']}      [direction: {tier}]"
            )
        else:
            lines.append(f"### {cand['label']}  {a_id} <-> {b_id}      [direction: {tier}]")

    lines.append("```")
    for src, rel, tgt in cand["patterns"]:
        lines.append(f"source pattern : {src} --{rel}--> {tgt}")

    if tier == TIER_TYPE_ORDER:
        g, s = cand["general"], cand["specific"]
        lines.append(f"general        : {_describe(g, cand, summaries)}")
        lines.append(f"specific       : {_describe(s, cand, summaries)}")
    else:
        lines.append(f"endpoint a     : {_describe(a_id, cand, summaries)}")
        lines.append(f"endpoint b     : {_describe(b_id, cand, summaries)}")

    lines.append("already linked : no")

    if tier == TIER_TYPE_ORDER:
        lines.append(f"cycle check    : {_cycle_line(cand['cycles'][(cand['general'], cand['specific'])])}")
    elif tier == TIER_AMBIGUOUS:
        for (x, y), path in sorted(cand["cycles"].items()):
            lines.append(f"cycle check    : {x} -> {y}: {_cycle_line(path)}")
    else:
        lines.append("cycle check    : n/a (endpoint-refused, no edge proposed)")

    if tier == TIER_AMBIGUOUS:
        lines.append(
            "direction      : AMBIGUOUS — both endpoints are the same "
            "generality rank; the types give no answer and the script does "
            "not guess."
        )
        for src, rel, tgt in cand["patterns"]:
            lines.append(
                f"HINT (not a rule): the source edge runs {src} --{rel}--> "
                f"{tgt}. `{rel}` is a different claim from `generalizes`; "
                "weigh it, do not apply it."
            )
    elif tier == TIER_REFUSED:
        # Tuples are rendered in their repr form so the pair reads exactly as
        # it would be written into GENERALIZES_ALLOWED_PAIRS — if the ruling
        # is overturned, this line is the edit.
        if cand["general"]:
            pair = (cand["types"][cand["general"]], cand["types"][cand["specific"]])
            lines.append(
                f"refused because: {pair!r} is not in "
                "GENERALIZES_ALLOWED_PAIRS (#2753 D1)."
            )
        else:
            fwd = (cand["types"][a_id], cand["types"][b_id])
            rev = (fwd[1], fwd[0])
            lines.append(
                f"refused because: neither {fwd!r} nor {rev!r} is in "
                "GENERALIZES_ALLOWED_PAIRS (#2753 D1), and the two types sit "
                "at the same generality rank, so no direction is implied."
            )
        lines.append(
            "next step      : this is authoring work, not migration — the "
            "usual fix is to mint the missing intermediate node, then write "
            "two whitelisted edges."
        )

    # The accept: line lives INSIDE the candidate block, last — it is the
    # payload the adjudicator copies. It exists only for TYPE-ORDER: AMBIGUOUS
    # rows have none because the direction is unsettled, and ENDPOINT-REFUSED
    # rows have none because the edge is not allowed.
    if tier == TIER_TYPE_ORDER:
        lines.append(
            f'accept: engram_add_edge("{cand["general"]}", "generalizes", '
            f'"{cand["specific"]}")'
        )
    lines.append("```")
    return lines


# --- cli --------------------------------------------------------------------

def default_db_path() -> Path:
    home = os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram")
    return Path(home) / "knowledge.db"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="propose-generalizes-migration.py",
        description=(
            "Propose `generalizes` edges for adjudication (#2753 WS3, D5). "
            "READ-ONLY: there is no --apply flag and no write path, by design."
        ),
    )
    p.add_argument("--db", default=None, help="path to knowledge.db (default: $ENGRAM_HOME/knowledge.db)")
    p.add_argument("--out", required=True, help="path to write the markdown report to")
    p.add_argument(
        "--include-ambiguous",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="include same-generality-rank pairs as an AMBIGUOUS tier (default: yes)",
    )
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db).expanduser() if args.db else default_db_path()

    try:
        conn = open_readonly(db_path)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        candidates, stats = collect_candidates(
            conn, include_ambiguous=args.include_ambiguous
        )
        summaries = _hydrate_summaries(conn, candidates)
        report = render_report(
            candidates, stats, db_path, args.include_ambiguous, summaries
        )
    finally:
        conn.close()

    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    counts = {t: sum(1 for c in candidates if c["tier"] == t) for t in TIER_ORDER}
    print(
        "proposed {total} candidate(s) -> {out} | ".format(
            total=len(candidates), out=out_path
        )
        + " | ".join(f"{t}: {counts[t]}" for t in TIER_ORDER)
        + " | scanned {scanned} principle->principle edges, "
          "dropped {sup} superseded-endpoint, {rel} supersedes/retracts, "
          "{done} already-generalizes".format(
              scanned=stats["principle_edges_scanned"],
              sup=stats["dropped_superseded_endpoint"],
              rel=stats["excluded_by_relation"],
              done=stats["dropped_already_generalizes"],
          )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
