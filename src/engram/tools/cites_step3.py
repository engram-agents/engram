#!/usr/bin/env python3
"""cites_step3.py -- per-seat migration of legacy loose `cites` edges (#2243 step 3, part B).

DEV-TIER TOOL. Run it only on your OWN graph (ENGRAM_HOME's); there is no flag
to point it elsewhere and nobody runs it on another seat's data.

A "loose" cites is every `cites` edge that is not a pair the engine's shared
predicate (core._cites_pair_error) accepts (observation source -> `ev_`
evidence target); that legitimate pair is never selected in any phase. Lei's ruling: `cites` carries observation -> evidence only, so
each loose one is converted to `about` or another NON-provenance relation. The
conversion is driven by the seat's own sampling report (--report) and per-edge
labels (--labels), in six phases that always run in this order (default
`--phases a,b,c,d,e,f`; a subset still runs in this order):

    a  duplicate pre-pass  a loose cites duplicating any other typed edge on the
                           same pair (either direction) -> about
    b  structural          feeling_report / question source -> about
    c  bulk                every loose cites of a bulk-ELIGIBLE stratum -> about
                           (eligibility is recomputed here, never read from the
                           report's own `decision` field)
    d  per-edge            the labelled edges, by their effective label
    e  wrong-target        edges labelled wrong-target -> about, listed apart
    f  frozen-history      every loose cites left whose SOURCE is non-current
                           (superseded or retracted) -> about

Phase f exists because no loose cites may remain, and a frozen node cannot carry
a premise edge that any live cascade route or confidence will read again; a
refile retracts and refiles the CURRENT node, so a loose cites with a superseded
or retracted source can only ever become `about`. It takes only what no earlier
phase touched: never the keeper pair, never an edge named in the labels file,
never an edge with an earlier ledger entry (applied, refused, deferred or on the
needs-seat-read / refile lists). Selection is by the SOURCE alone: a superseded
TARGET changes nothing. The set is listed by stratum (source type -> target
type, count) in the plan and the ledger header, and exported with its edge ids to
`frozen-history-<ts>.json` ({edge_id, source_id, target_id, source_status,
target_current}) so a seat can spot-read some before `--apply`.
Re-running `--phases f` re-applies no edge: what applied is no longer a cites;
an edge the engine refused (a duplicate of an `about`) or deferred (a pending
cascade marker) is retried and recorded again as a fresh f entry, as in
phases a-e.

A mis-filed-premise edge whose source is a DERIVATION is never retyped (Lei's
amendment): it stays `cites`, and is exported on the refile list GROUPED BY
SOURCE NODE (action `retract+refile`; the seat acts once per source). A
mis-filed-premise (or `relies`) edge whose source is any other type is retyped
to `about` (reason `non-derivation-premise`) and exported on the promotion
candidate list. The tool never creates a provenance edge.

HAND-ONLY derivation sources (Kepler's ruling on #2243). A premise wired into an
`inductive_generalization` must be part of that generalization's basis, one of
its cases; a precision sentence, a methodological recommendation or a
restatement is not a case and belongs on `about` (wiring one as a premise was
measured to inflate a derivation's confidence 0.777 -> 0.941). So a refile-list
source whose `reasoning_type` belongs to the engine's reasoning CLASS
`inductive_corroboration` (the engine's own REASONING_CLASS: today
`inductive_generalization`, `inductive_enumeration` and `authority_consensus`;
`inductive_corroboration` is that class name, not a type, and a type added to
the class later is covered automatically) is listed with action `hand-only`,
never `retract+refile`: the seat reads the premise role from the chain by hand (a
premise of a generalization is a case, anything else goes to `about`). The
`reasoning_type` is read with the guarded json_extract form (a malformed blob
yields NULL, never aborts the run); a `reasoning_type` key present twice, counted
with a guarded json_each, is unreadable (json_extract alone would return the
first, the engine's json.loads the last). A source
whose type is missing, empty, not a plain type name, duplicated, or in an
unreadable metadata blob is ALSO hand-only, with `why` `reasoning-type-unreadable`
(unknown is not reported as the safe case); a well-formed name that is not one of
the engine's reasoning types (a typo, a spelling variant, a future name) is
hand-only with `why` `reasoning-type-unrecognized`. Every other recognized type
keeps `retract+refile`. The per-edge needs-seat-read entry, the refile export, the
needs-seat-read file, the printed summary and the ledger header
(`hand_only_counts`) all say the same thing. The tool itself never refiles or
retracts either way: its only graph write is the cites -> about retype.

Per-stratum `lost-relation` export: every SAMPLED edge (from the report and
from the labels file's effective labels) whose effective label names a relation
that cannot be added after creation (resolves, supersedes, contradicts, ...,
or a name that is no relation at all) is listed, grouped by stratum, with the
phase that acted on it and what was done (`about` or `none`). Bulk keeps the
link as `about` and loses only its type name; the proper home of a real
resolves link is the engram_resolve verb, which a cites edge never performed.
So the SEAT runs that verb by hand on the listed entries; this tool never
calls it. A keeper pair (the legitimate cites) is never listed.

Phase c (bulk) skips every edge named in the labels file: the seat's per-edge
pass (phase d) decides those. A labelled mis-filed-premise derivation edge is
likewise skipped by the duplicate pre-pass (phase a), so it is never retyped in
any phase; the plan header counts the labelled edges that sit in a bulk-eligible
stratum (`labelled_in_bulk_strata`) and a WARNING on stderr lists them. The
refile list holds exactly the edges that stay `cites`: an edge retyped to
`about` (for instance a derivation-source `relies` edge labelled correct-about)
is not on it and is shown on the promotion list flagged `retyped`.

Dry run is the default. It writes nothing to the graph: the graph's data file
bytes are unchanged (SQLite may create empty -wal/-shm sidecars when opening a
WAL database read-only). `--apply` needs the sampling report, the labels, and a
cross-lineage hand-check file naming at least min(20, correct-about derivation
edges) DISTINCT edge ids. Every retype goes through the engine's guarded verb
(_retype_edge_impl); this file never writes to the graph itself.

The DAG audit gates every phase. It aborts the run (exit 3, status
`aborted-audit`) when the UNCAPPED `cycle_count` rises, or when a node lies on a
non-trivial strongly connected component of one family's graph after the phase
and on none before it (a genuinely new cyclic component). The components are
computed by this tool from the graph itself, over the same edges the audit walks
(each family's relations, endpoints resolved through the supersedes chain to
their current node), so neither rule reads the audit's cycle listing and the
listing's cap (MAX_CYCLES_PER_FAMILY, 20 per family) does not matter. The run
does NOT abort because the listed back-edge path differs while the count did not
rise and every node was already cyclic: the audit lists one cycle per DFS back
edge, so removing a `cites` edge can change which path is listed without any new
cycle.

`--apply` first takes a `sqlite3` online backup of the graph into `--out-dir`
(`knowledge-before-cites-step3-<ts>.db`, mode 0600), checks that it opens and holds
the same number of edges, records `backup: {path, bytes, edges}` in the ledger
header, and REFUSES to start (exit 2, nothing written, no retype tried) if that
fails. The dry run takes none.

A finished run is not a finished migration. The header (and the dry run's plan)
carry `loose_cites_remaining`: the loose `cites` edges still there after the run
(plan: after the planned retypes), by stratum, with how many have a non-current
(superseded or retracted) source and target. `status` is `complete` only when that
count is 0, else `complete-with-remaining`. What to do with the remainder is the
maintainer's decision; the tool only reports it. A cites that duplicates an
existing `about` can never be removed (the engine refuses and never deletes): such
refusals are listed under `duplicate-of-about-unremovable` in the header and plan.

An unexpected error after the first retype still writes the ledger (status
`aborted-error`, the error text, the entries so far) and the output files, then
exits 1. Exit codes: 0 ok (incl. dry run and --help), 2 input / identity /
precondition refusal, 3 audit abort, 1 unexpected error.

Usage:
    ENGRAM_HOME=~/.engram python tools/cites_step3.py --report R.json --labels L.json
    ENGRAM_HOME=~/.engram python tools/cites_step3.py --report R.json --labels L.json \\
        --handcheck H.json --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# append (not insert(0)) -- #2565: src/engram at sys.path POSITION 0 would outrank the repo-root tools/ package.
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from tools import _owner_read as owner_read  # noqa: E402

LABELS = ("correct-about", "mis-filed-premise", "other-relation", "unforeseen", "cannot-judge")
PHASES = ("a", "b", "c", "d", "e", "f")
STRUCTURAL_SOURCES = ("feeling_report", "question")
MARKER_TEXT = "pending cascade marker"
# #2243 (Kepler): a premise of a derivation in this reasoning CLASS must be one of
# the cases, so a derivation source of any type in the class is never listed as a
# plain refile. The members come from the engine's REASONING_CLASS at call time.
HAND_ONLY_CLASS = "inductive_corroboration"
HAND_ONLY_WHY = ("a premise of a generalization is one of its cases; a precision sentence, "
                 "recommendation or restatement goes to about -- read the premise role from the chain")
UNREADABLE_WHY = "reasoning-type-unreadable"
UNRECOGNIZED_WHY = "reasoning-type-unrecognized"
_RTYPE_NAME = re.compile(r"[a-z][a-z_]*")
# The premise-ancestry walk (fold: "already covered") follows these two
# relations from a derivation source, over current nodes only, at most this
# many hops; past the cap the candidate counts as NOT covered.
PREMISE_RELATIONS = ("derives_from", "supported_by")
MAX_ANCESTRY_HOPS = 50
# A cites that duplicates an `about` on the same pair can never be removed: the
# engine refuses the retype and never deletes (header bucket name).
DUP_ABOUT_BUCKET = "duplicate-of-about-unremovable"
# Phase f's reason code (ledger `reason`, and the engine's reason after the prefix).
FROZEN_CODE = "frozen-history"


def _non_current(is_current, status):
    """A node that is no longer live: superseded (is_current != 1) or retracted.
    The one definition `loose_cites_remaining` and phase f share."""
    return is_current != 1 or status == "retracted"


class Refusal(Exception):
    """An input / identity / precondition refusal (exit 2)."""


class HelpShown(Exception):
    """--help was printed; a clean exit (0), not a refusal."""


def _ro_uri(path):
    """A read-only SQLite URI for `path`, with `#`, `?`, `%` and spaces escaped."""
    return f"file:{urllib.parse.quote(str(path))}?mode=ro"


# ---------------------------------------------------------------------------
# engine loading
# ---------------------------------------------------------------------------

def _find_engine_dir() -> Path:
    cands = []
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root:
        cands.append(Path(root))
    here = Path(__file__).resolve()
    for p in (here.parent, *here.parents):
        cands.append(p)
        cands.append(p / "src" / "engram")
    for c in cands:
        if (c / "engram_core.py").exists():
            return c
    raise RuntimeError("could not locate engram_core.py (set CLAUDE_PLUGIN_ROOT)")


def _load_engine():
    try:
        import engram_core  # noqa: F401
    except ImportError:
        sys.path.append(str(_find_engine_dir()))
    import engram_core as core
    import engram_cornerstone as cornerstone
    import engram_dag_conformance as dagc
    _reasoning_constants()          # a missing constants module refuses here, before any write
    return core, cornerstone, dagc


def _reasoning_constants():
    """(REASONING_TYPES, REASONING_CLASS) from the engine's own module, read at
    call time (never copied into this tool). If the module cannot be imported
    the ImportError propagates: this tool does not guess the engine's types."""
    try:
        import engram_confidence as ec
    except ImportError:
        sys.path.append(str(_find_engine_dir()))
        import engram_confidence as ec
    return ec.REASONING_TYPES, ec.REASONING_CLASS


# ---------------------------------------------------------------------------
# input validation (everything here happens before the first write)
# ---------------------------------------------------------------------------

def _need(cond, msg):
    if not cond:
        raise Refusal(msg)


def _read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise Refusal(f"{what} file {path!r} unreadable or not JSON: {exc}")


def _norm(x):
    if x is None:
        return None
    x = str(x).strip().lower()
    return x or None


def _check_label_obj(obj, where, need_labeller):
    _need(isinstance(obj, dict), f"{where}: must be an object")
    _need(obj.get("label") in LABELS,
          f"{where}.label: {obj.get('label')!r} is not one of {list(LABELS)}")
    _need(obj.get("relation_if_any") is None or isinstance(obj["relation_if_any"], str),
          f"{where}.relation_if_any: must be null or a string")
    _need(obj.get("premise_test") is None or isinstance(obj["premise_test"], str),
          f"{where}.premise_test: must be null or a string")
    _need(isinstance(obj.get("reason", ""), str), f"{where}.reason: must be a string")
    if need_labeller:
        _need(isinstance(obj.get("labeller"), str) and obj["labeller"],
              f"{where}.labeller: required non-empty string")


def validate_report(rep):
    _need(isinstance(rep, dict), "report: top level must be an object")
    for f in ("seat", "lineage"):
        _need(isinstance(rep.get(f), str) and rep[f], f"report.{f}: required non-empty string")
    _need(isinstance(rep.get("frame"), dict) and "seed" in rep["frame"],
          "report.frame.seed: required")
    _need(isinstance(rep.get("strata"), list), "report.strata: required list")
    for i, s in enumerate(rep["strata"]):
        w = f"report.strata[{i}]"
        _need(isinstance(s, dict), f"{w}: must be an object")
        _need(isinstance(s.get("stratum"), str) and "->" in s["stratum"],
              f"{w}.stratum: must look like '<source_type>-><target_type>'")
        for f in ("population", "n", "judged"):
            _need(isinstance(s.get(f), int) and not isinstance(s[f], bool) and s[f] >= 0,
                  f"{w}.{f}: required non-negative integer")
        _need(isinstance(s.get("counts"), dict), f"{w}.counts: required object")
        for k, v in s["counts"].items():
            _need(k in LABELS, f"{w}.counts: unknown label {k!r}")
            _need(isinstance(v, int) and not isinstance(v, bool) and v >= 0,
                  f"{w}.counts[{k!r}]: must be a non-negative integer")
        _need(sum(s["counts"].values()) == s["n"],
              f"{w}: counts sum to {sum(s['counts'].values())} but n is {s['n']}")
    _need(isinstance(rep.get("edges"), list), "report.edges: required list")
    for i, e in enumerate(rep["edges"]):
        w = f"report.edges[{i}]"
        _need(isinstance(e, dict), f"{w}: must be an object")
        _need(isinstance(e.get("edge_id"), int), f"{w}.edge_id: required integer")
        for f in ("source_id", "target_id", "source_type", "target_type"):
            _need(isinstance(e.get(f), str) and e[f], f"{w}.{f}: required non-empty string")
        _need(isinstance(e.get("target_id_named_in_source_text"), bool),
              f"{w}.target_id_named_in_source_text: required boolean")
        _check_label_obj(e.get("label"), f"{w}.label", False)
    ua = rep.get("unchecked_axes", [])
    _need(isinstance(ua, list), "report.unchecked_axes: must be a list")
    return rep


def validate_labels(doc):
    _need(isinstance(doc, dict) and isinstance(doc.get("edges"), list),
          "labels: top level must be {'edges': [...]}")
    for i, e in enumerate(doc["edges"]):
        w = f"labels.edges[{i}]"
        _need(isinstance(e, dict), f"{w}: must be an object")
        _need(isinstance(e.get("edge_id"), int), f"{w}.edge_id: required integer")
        for f in ("source_id", "target_id"):
            _need(isinstance(e.get(f), str) and e[f], f"{w}.{f}: required non-empty string")
        labs = e.get("labels")
        _need(isinstance(labs, list) and 1 <= len(labs) <= 2, f"{w}.labels: must hold 1 or 2 labels")
        for j, l in enumerate(labs):
            _check_label_obj(l, f"{w}.labels[{j}]", True)
        res = e.get("seat_resolution")
        if res is not None:
            _check_label_obj(res, f"{w}.seat_resolution", False)
        if "target_id_named_in_source_text" in e:
            _need(isinstance(e["target_id_named_in_source_text"], bool),
                  f"{w}.target_id_named_in_source_text: must be a boolean")
    return doc


def validate_handcheck(h, rep=None):
    _need(isinstance(h, dict), "handcheck: top level must be an object")
    for f in ("reviewer", "reviewer_lineage"):
        _need(isinstance(h.get(f), str) and h[f], f"handcheck.{f}: required non-empty string")
    _need(isinstance(h.get("edges_read"), list), "handcheck.edges_read: required list")
    _need(all(isinstance(x, (int, str)) and not isinstance(x, bool) for x in h["edges_read"]),
          "handcheck.edges_read: every entry must be an edge id (an integer or a string)")
    if rep is not None:
        have = {str(e["edge_id"]) for e in rep["edges"]}
        missing = sorted({str(x) for x in h["edges_read"]} - have, key=lambda v: (len(v), v))
        _need(not missing,
              f"handcheck.edges_read: edge id(s) {', '.join(missing)} are not edges of the report's "
              f"`edges` (the hand-check must read edges the report sampled)")
    for f in ("agree", "disagree"):
        _need(isinstance(h.get(f), int) and not isinstance(h[f], bool) and h[f] >= 0,
              f"handcheck.{f}: required non-negative integer")
    return h


def handcheck_minimum(rep):
    """min(20, correct-about labels in the report's derivation-source strata),
    and at least 1: how many distinct edges the hand-check must read."""
    n_ca = sum(s["counts"].get("correct-about", 0) for s in rep["strata"]
               if s["stratum"].split("->")[0] == "derivation")
    return max(1, min(20, n_ca)), n_ca


def check_handcheck_sources(h, rep):
    """When the report carries labelled derivation-source `correct-about` edges,
    at least the minimum number of the hand-check's distinct edge ids must be
    among them (the minimum is capped at how many such edges the report lists)."""
    need, _n_ca = handcheck_minimum(rep)
    deriv = {str(e["edge_id"]) for e in rep["edges"]
             if e["source_type"] == "derivation" and e["label"]["label"] == "correct-about"}
    if not deriv:
        return
    required = min(need, len(deriv))
    got = len({str(x) for x in h["edges_read"]} & deriv)
    _need(got >= required,
          f"handcheck.edges_read: {got} distinct edge id(s) are labelled derivation-source "
          f"correct-about edges of the report; need at least {required}")


# ---------------------------------------------------------------------------
# pure decision logic
# ---------------------------------------------------------------------------

def addable_nonabout(core):
    return {r for r, c in core.EDGE_CLASSIFICATIONS.items()
            if c.get("addable_after_creation") and r != "about"}


def bulk_eligibility(rep, addable):
    """Recompute per-stratum bulk eligibility from counts and sampled edges.
    The report's own `decision` field is never read."""
    out = {}
    sampled = {}
    for e in rep["edges"]:
        sampled.setdefault(f"{e['source_type']}->{e['target_type']}", []).append(e)
    for s in rep["strata"]:
        name, n, c = s["stratum"], s["n"], s["counts"]
        edges = sampled.get(name, [])
        tally = {}
        for e in edges:
            tally[e["label"]["label"]] = tally.get(e["label"]["label"], 0) + 1
        cj = c.get("cannot-judge", 0)
        judged = n - cj
        why = []
        if judged < 5:
            why.append(f"judged {judged} < 5")
        if max(c.get("mis-filed-premise", 0), tally.get("mis-filed-premise", 0)) > 0:
            why.append("has mis-filed-premise")
        if max(c.get("unforeseen", 0), tally.get("unforeseen", 0)) > 0:
            why.append("has unforeseen")
        named = [_norm(e["label"].get("relation_if_any")) for e in edges
                 if e["label"]["label"] == "other-relation"]
        if any(r in addable for r in named):
            why.append("other-relation names an addable relation")
        if c.get("other-relation", 0) > len(named):
            why.append("other-relation edges not all visible in report.edges")
        if n and cj * 5 > n:
            why.append(f"cannot-judge {cj}/{n} > 20%")
        out[name] = {"eligible": not why, "reasons": why}
    return out


def effective_label(le):
    """The label to act on, or None when the labels disagree."""
    if le.get("seat_resolution"):
        return le["seat_resolution"]
    labs = le["labels"]
    if len(labs) == 1:
        return labs[0]
    a, b = labs
    if a["label"] == b["label"] and _norm(a.get("relation_if_any")) == _norm(b.get("relation_if_any")):
        return a
    return None


def read_reasoning_type(conn, node_id):
    """The node's `reasoning_type` (lower-cased), or None when it cannot be
    read: no such row, NULL / non-JSON / non-object metadata, a missing,
    empty or non-string value, a `reasoning_type` key present twice, or
    anything that is not a plain type name. The node is read by id whatever its
    currentness or status. Every JSON function takes the guarded form (an
    unguarded one over the column aborts the whole statement on one malformed
    blob). json_extract alone returns the FIRST duplicate key while the engine's
    json.loads returns the LAST, so the top-level members named reasoning_type
    are COUNTED with json_each and anything but exactly one is unreadable."""
    row = conn.execute(
        "SELECT json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.reasoning_type'), "
        "(SELECT COUNT(*) FROM json_each(CASE WHEN json_valid(metadata) THEN metadata END) "
        "WHERE key = 'reasoning_type') FROM nodes WHERE id = ?", (node_id,)).fetchone()
    # json_each walks every member of the top-level object, duplicates included
    if row is None or row[1] != 1 or not isinstance(row[0], str):
        return None
    value = row[0]
    name = value.strip().lower()
    return name if _RTYPE_NAME.fullmatch(name) else None


def classify_derivation(conn, source_id):
    """-> (reasoning_type or None, hand_only, why or None) for a derivation
    source. Hand-only when the type's engine reasoning CLASS is
    `inductive_corroboration`, and (fail closed) whenever the type cannot be
    read or is not a type the engine knows."""
    rtype = read_reasoning_type(conn, source_id)
    if rtype is None:
        return None, True, UNREADABLE_WHY
    types, classes = _reasoning_constants()
    if rtype not in types:
        return rtype, True, UNRECOGNIZED_WHY
    if classes.get(rtype) == HAND_ONLY_CLASS:
        return rtype, True, HAND_ONLY_WHY
    return rtype, False, None


def decide(le, named, addable, source_type):
    """Decision for one labelled edge -> (decision dict, candidate or None).

    The candidate (a mis-filed-premise effective label, or `relies` on any
    label) is routed later by SOURCE type: a derivation source goes to the
    refile list, any other source type to the promotion list."""
    labs = le["labels"]
    eff = effective_label(le)
    cand = None
    relies = [l for l in labs if _norm(l.get("premise_test")) == "relies"]
    misfiled = eff is not None and eff["label"] == "mis-filed-premise"
    if misfiled or relies:
        src = eff if misfiled else relies[0]
        cand = {"edge_id": le["edge_id"], "source_id": le["source_id"],
                "source_type": source_type, "target_id": le["target_id"],
                "label": src["label"], "reason": src.get("reason", ""),
                "relies": bool(relies), "labellers": [l["labeller"] for l in labs]}
    if eff is None:
        return {"kind": "needs", "bucket": "labels-disagree", "action": "read"}, cand
    rel = _norm(eff.get("relation_if_any"))
    reason = (eff.get("reason") or "").strip().lower()
    lbl = eff["label"]
    if rel == "wrong-target" or reason == "wrong-target":
        return {"kind": "wrong-target"}, cand
    if lbl == "correct-about":
        return {"kind": "act", "to": "about", "code": "correct-about"}, cand
    if lbl == "other-relation":
        if rel in addable:
            return {"kind": "act", "to": rel, "code": f"other-relation:{rel}", "fallback": rel}, cand
        return {"kind": "act", "to": "about", "code": f"other-relation:{rel or 'unnamed'}"}, cand
    if lbl == "mis-filed-premise":
        if source_type == "derivation":
            return {"kind": "needs", "bucket": "mis-filed-premise", "action": "retract+refile"}, cand
        return {"kind": "act", "to": "about", "code": "non-derivation-premise"}, cand
    if lbl == "cannot-judge":
        if named:
            return {"kind": "needs", "bucket": "cannot-judge-named", "action": "read"}, cand
        return {"kind": "act", "to": "about", "code": "no-textual-trace"}, cand
    return {"kind": "needs", "bucket": "unforeseen", "action": "read"}, cand


def premise_path(conn, source_id, target_id, stats=None):
    """Shortest path source -> ... -> target along PREMISE_RELATIONS edges
    through CURRENT, non-retracted nodes, or None. Breadth-first with a visited
    set (a cyclic premise graph terminates, and a lattice with many routes to
    one ancestor stays linear) and a hop cap (MAX_ANCESTRY_HOPS; a target only
    reachable past the cap is NOT covered). core has no path-returning premise
    walk (its upstream walk covers the cascade relations only and returns a
    set), so this one is local. `stats`, when given, receives `expanded`: the
    number of nodes whose premise edges were read."""
    marks = ",".join("?" for _ in PREMISE_RELATIONS)
    parent = {source_id: None}
    frontier = [source_id]
    for _hop in range(MAX_ANCESTRY_HOPS):
        nxt = []
        for node in frontier:
            if stats is not None:
                stats["expanded"] = stats.get("expanded", 0) + 1
            rows = conn.execute(
                f"SELECT e.target_id AS t FROM edges e JOIN nodes n ON n.id = e.target_id "
                f"WHERE e.source_id = ? AND e.relation IN ({marks}) AND n.is_current = 1 "
                f"AND COALESCE(n.status, 'active') != 'retracted' ORDER BY e.target_id",
                (node, *PREMISE_RELATIONS)).fetchall()
            for r in rows:
                t = r["t"]
                if t in parent:
                    continue
                parent[t] = node
                if t == target_id:
                    path = [t]
                    while parent[path[-1]] is not None:
                        path.append(parent[path[-1]])
                    return path[::-1]
                nxt.append(t)
        if not nxt:
            return None
        frontier = nxt
    return None


def canon_cycle(c):
    path = list(c["path"])
    if len(path) > 1 and path[0] == path[-1]:
        path = path[:-1]
    if not path:
        return (c["family"], ())
    i = path.index(min(path))
    return (c["family"], tuple(path[i:] + path[:i]))


def cyclic_nodes(edges):
    """The nodes on a non-trivial strongly connected component of the directed
    graph `edges` (a component of more than one node, or a node with a self-loop).
    Iterative Tarjan: no recursion limit on a long chain."""
    adj = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, [])
    index, low, on, stack, comp = {}, {}, set(), [], {}
    counter = 0
    for root in sorted(adj):
        if root in index:
            continue
        work = [(root, iter(sorted(set(adj[root]))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nb in it:
                if nb not in index:
                    index[nb] = low[nb] = counter
                    counter += 1
                    stack.append(nb)
                    on.add(nb)
                    work.append((nb, iter(sorted(set(adj[nb])))))
                    advanced = True
                    break
                if nb in on:
                    low[node] = min(low[node], index[nb])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                members = []
                while True:
                    m = stack.pop()
                    on.discard(m)
                    members.append(m)
                    if m == node:
                        break
                for m in members:
                    comp[m] = (node, len(members))
    selfloop = {a for a, b in edges if a == b}
    return {n for n, (_rep, size) in comp.items() if size > 1} | selfloop


def audit_regression(prev, after):
    """Did the audit get WORSE between two snapshots? -> the offenders (a
    non-empty list) or [].

    Worse means: a node lies on a non-trivial strongly connected component of
    one family's graph after the phase and on none before it (a genuinely new
    cyclic component; the offenders are, per family, those nodes), or the
    uncapped `cycle_count` rose (the offenders are the AFTER cycles not seen
    before, for reporting). Both rules read the tool's own computation from the
    graph (`cyclic`), never the audit's listing, which is capped per family: a
    changed listing with a falling count and no newly cyclic node is NOT a
    regression, however many cycles exist."""
    out = []
    for fam in sorted(after["cyclic"]):
        new = after["cyclic"][fam] - prev["cyclic"].get(fam, frozenset())
        if new:
            out.append((fam, tuple(sorted(new))))
    if out:
        return out
    if after["cycle_count"] > prev["cycle_count"]:
        old = set(prev["cycles"])
        return [c for c in after["cycles"] if c not in old] or list(after["cycles"])
    return []


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

class Run:
    def __init__(self, core, cornerstone, dagc, home, rep, labels, apply_, phases, quiet=False):
        self.core, self.cs, self.dagc = core, cornerstone, dagc
        self.home, self.rep, self.labels = home, rep, labels
        self.apply, self.phases = apply_, phases
        self.addable = addable_nonabout(core)
        self.entries = []
        self.claimed = {}          # edge_id -> status of its (terminal) entry
        self.candidates = []
        self.fallbacks = 0
        self.wrong_target = []
        self.eligibility = bulk_eligibility(rep, self.addable)
        self.report_edges = {}
        for e in rep["edges"]:
            self.report_edges[(e["source_id"], e["target_id"])] = e
        # every edge the seat labelled: the per-edge pass decides these, never bulk
        self.labelled = {(le["source_id"], le["target_id"]) for le in labels["edges"]}
        self.retyped_to = {}       # edge_id -> relation it was (or would be) retyped to
        self.error = None
        self._labelled_in_bulk = []
        self.audit = {"before": None, "after_phase": {}, "final": None}
        self.status = "complete"
        self._lost = None
        self.backup = None          # {path, bytes, edges}, set by --apply before the first write
        self._remaining = None
        self.frozen_meta = {}       # edge_id -> export row, for what phase f selected

    # -- connections ------------------------------------------------------
    def open(self):
        if self.apply:
            return self.core._get_db()
        c = sqlite3.connect(_ro_uri(self.core.DB_PATH), uri=True)
        c.row_factory = sqlite3.Row
        return c

    def loose_rows(self, conn):
        rows = conn.execute(
            "SELECT e.id AS id, e.source_id AS s, e.target_id AS t, ns.type AS st, "
            "nt.type AS tt FROM edges e JOIN nodes ns ON ns.id = e.source_id "
            "JOIN nodes nt ON nt.id = e.target_id WHERE e.relation = 'cites' ORDER BY e.id"
        ).fetchall()
        return [r for r in rows
                if not self.is_keeper(conn, r["s"], r["t"])
                and r["id"] not in self.claimed]

    def is_keeper(self, conn, source_id, target_id):
        """The one legitimate cites: the pair the engine's shared predicate
        (core._cites_pair_error, the same one the cites writer and the retype
        guard use) accepts. This tool holds no copy of that test."""
        return self.core._cites_pair_error(conn, source_id, target_id) is None

    # -- audit ------------------------------------------------------------
    def snapshot_audit(self):
        conn = self.open()
        try:
            r = self.dagc.check_dag_conformance(conn)
            cyclic = {}
            for fam in self.core.DAG_FAMILIES:
                # the audit's own scope: the family's relations within DAG_RELATIONS,
                # endpoints resolved through the supersedes chain to current nodes
                walked = self.core.dag_family_relations(fam) & self.core.DAG_RELATIONS
                cyclic[fam] = frozenset(cyclic_nodes(self.dagc._live_dag_edges(conn, walked)))
        finally:
            conn.close()
        return {"cycle_count": r["cycle_count"], "cycles": [canon_cycle(c) for c in r["cycles"]],
                "cyclic": cyclic}

    # -- entry helpers ----------------------------------------------------
    def add(self, phase, row, to, reason, status, **extra):
        e = {"phase": phase, "edge_id": row["id"], "source_id": row["s"], "target_id": row["t"],
             "from": "cites", "to": to, "reason": reason, "status": status,
             "stratum": f"{row['st']}->{row['tt']}"}
        e.update(extra)
        self.entries.append(e)
        if status != "skipped-not-cites":
            self.claimed[row["id"]] = status
        if status in ("applied", "planned"):
            self.retyped_to[row["id"]] = to
        return e

    def engine(self, row, new, phase, code):
        reason = f"cites-step3 {phase}: {code}"
        raw = self.cs._retype_edge_impl(source_id=row["s"], target_id=row["t"], relation="cites",
                                        new_relation=new, reason=reason)
        try:
            res = json.loads(raw)
        except (TypeError, ValueError):
            return False, f"unparseable engine reply: {str(raw)[:80]}"
        if res.get("status") == "retyped":
            return True, ""
        return False, str(res.get("error") or res)

    def still_cites(self, conn, row):
        return conn.execute(
            "SELECT 1 FROM edges WHERE source_id=? AND target_id=? AND relation='cites'",
            (row["s"], row["t"])).fetchone() is not None

    def act(self, conn, phase, row, to, code, fallback=None):
        """One planned retype: check it is still a cites at its turn, then
        (apply) hand it to the engine or (dry run) just record it."""
        if row["id"] in self.claimed:
            if self.claimed[row["id"]] in ("applied", "planned"):
                self.add(phase, row, None, code, "skipped-not-cites")
            return
        if not self.still_cites(conn, row):
            self.add(phase, row, None, code, "skipped-not-cites")
            return
        if not self.apply:
            if to == "about" and self.about_exists(conn, row):
                # the engine will refuse this move and never deletes: predict it
                self.add(phase, row, to, code,
                         "refused:predicted: an 'about' edge already exists -- nothing to retype into",
                         bucket=DUP_ABOUT_BUCKET)
                return
            extra = {"fallback_to_about_if_engine_refuses": True} if fallback else {}
            self.add(phase, row, to, code, "planned", **extra)
            return
        ok, msg = self.engine(row, to, phase, code)
        if ok:
            self.add(phase, row, to, code, "applied")
            return
        if MARKER_TEXT in msg:
            self.add(phase, row, to, code, "deferred-pending-marker")
            return
        if fallback:
            fcode = f"fallback:{fallback}-refused:{msg[:80]}"
            self.fallbacks += 1
            ok2, msg2 = self.engine(row, "about", phase, fcode)
            if ok2:
                self.add(phase, row, "about", fcode, "applied")
            elif MARKER_TEXT in msg2:
                self.add(phase, row, "about", fcode, "deferred-pending-marker")
            else:
                self.refuse(conn, phase, row, "about", fcode, msg2)
            return
        self.refuse(conn, phase, row, to, code, msg)

    def about_exists(self, conn, row):
        return conn.execute(
            "SELECT 1 FROM edges WHERE source_id=? AND target_id=? AND relation='about'",
            (row["s"], row["t"])).fetchone() is not None

    def refuse(self, conn, phase, row, to, code, msg):
        """Record an engine refusal. The engine's "an 'about' edge ... already
        exists -- nothing to retype into" (matched by its wording AND by a real
        `about` edge on the pair, same direction) gets its own bucket."""
        dup = (to == "about"
               and re.search(rf"an? 'about' edge from {re.escape(row['s'])} to {re.escape(row['t'])} "
                             rf"already exists -- nothing to retype into", msg) is not None
               and self.about_exists(conn, row))
        extra = {"bucket": DUP_ABOUT_BUCKET} if dup else {}
        self.add(phase, row, to, code, f"refused:{msg}", **extra)

    def dup_about_unremovable(self):
        es = sorted((e for e in self.entries if e.get("bucket") == DUP_ABOUT_BUCKET),
                    key=lambda e: e["edge_id"])
        return {"count": len(es),
                "entries": [{"edge_id": e["edge_id"], "source_id": e["source_id"],
                             "target_id": e["target_id"]} for e in es]}

    def loose_remaining(self):
        """The loose cites edges still there after the run (dry run: after the
        planned retypes), by stratum, and how many have a non-current source /
        target (superseded or retracted). The keeper pair is never loose."""
        if self._remaining is not None:
            return self._remaining
        conn = self.open()
        try:
            rows = conn.execute(
                "SELECT e.id AS id, e.source_id AS s, e.target_id AS t, ns.type AS st, nt.type AS tt, "
                "ns.is_current AS sc, ns.status AS ss, nt.is_current AS tc, nt.status AS ts "
                "FROM edges e JOIN nodes ns ON ns.id = e.source_id JOIN nodes nt ON nt.id = e.target_id "
                "WHERE e.relation = 'cites' ORDER BY e.id").fetchall()
            per, total, nc_s, nc_t = {}, 0, 0, 0
            for r in rows:
                if r["id"] in self.retyped_to or self.is_keeper(conn, r["s"], r["t"]):
                    continue
                total += 1
                k = f"{r['st']}->{r['tt']}"
                per[k] = per.get(k, 0) + 1
                nc_s += 1 if _non_current(r["sc"], r["ss"]) else 0
                nc_t += 1 if _non_current(r["tc"], r["ts"]) else 0
        finally:
            conn.close()
        self._remaining = {"total": total, "per_stratum": dict(sorted(per.items())),
                           "non_current_source": nc_s, "non_current_target": nc_t}
        return self._remaining

    # -- phases -----------------------------------------------------------
    def protected_pairs(self, conn):
        """Labelled edges whose effective label is mis-filed-premise and whose
        source is a derivation: never retyped by an earlier phase (the per-edge
        pass routes them to the refile list (retract+refile, or hand-only for
        a generalization / corroboration), or to already-covered)."""
        out = set()
        for le in self.labels["edges"]:
            eff = effective_label(le)
            if eff is None or eff["label"] != "mis-filed-premise":
                continue
            n = conn.execute("SELECT type FROM nodes WHERE id=?", (le["source_id"],)).fetchone()
            if n is not None and n["type"] == "derivation":
                out.add((le["source_id"], le["target_id"]))
        return out

    def phase_a(self, conn):
        protected = self.protected_pairs(conn)
        for r in self.loose_rows(conn):
            if (r["s"], r["t"]) in protected:
                continue
            typed = conn.execute(
                "SELECT relation, source_id FROM edges WHERE relation != 'cites' AND "
                "((source_id=? AND target_id=?) OR (source_id=? AND target_id=?))",
                (r["s"], r["t"], r["t"], r["s"])).fetchall()
            if not typed:
                continue
            best = min((x["relation"], x["source_id"] != r["s"]) for x in typed)
            code = f"duplicate-of:{best[0]}" + (":reverse" if best[1] else "")
            self.act(conn, "a", r, "about", code)

    def phase_b(self, conn):
        for r in self.loose_rows(conn):
            if r["st"] in STRUCTURAL_SOURCES:
                self.act(conn, "b", r, "about", "structural")

    def phase_c(self, conn):
        ok = {k for k, v in self.eligibility.items() if v["eligible"]}
        for r in self.loose_rows(conn):
            k = f"{r['st']}->{r['tt']}"
            if (r["s"], r["t"]) in self.labelled:
                continue       # the seat labelled it: phase d decides, bulk never does
            if k in ok:
                self.act(conn, "c", r, "about", f"bulk:{k}")

    def is_named(self, conn, le):
        if "target_id_named_in_source_text" in le:
            return le["target_id_named_in_source_text"]
        re_ = self.report_edges.get((le["source_id"], le["target_id"]))
        if re_ is not None:
            return re_["target_id_named_in_source_text"]
        row = conn.execute(
            "SELECT claim, logical_chain, interpretation, quoted_text FROM nodes WHERE id=?",
            (le["source_id"],)).fetchone()
        if row is None:
            return False
        text = " ".join(str(x) for x in row if x)
        # a word match: an id is not named by a longer id that merely starts with it
        # (ids are runs of [A-Za-z0-9_])
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(le['target_id'])}(?![A-Za-z0-9_])",
                         text) is not None

    def label_decisions(self, conn):
        out = []
        for le in self.labels["edges"]:
            row = self.label_row(conn, le)
            d, cand = decide(le, self.is_named(conn, le), self.addable, row["st"])
            if self.is_keeper(conn, row["s"], row["t"]):
                cand = None    # the legitimate cites is on no export list
            out.append((le, row, d, cand))
        return out

    def label_row(self, conn, le):
        r = conn.execute(
            "SELECT e.id AS id, e.source_id AS s, e.target_id AS t, ns.type AS st, nt.type AS tt "
            "FROM edges e JOIN nodes ns ON ns.id=e.source_id JOIN nodes nt ON nt.id=e.target_id "
            "WHERE e.source_id=? AND e.target_id=? AND e.relation='cites'",
            (le["source_id"], le["target_id"])).fetchone()
        if r is not None:
            return r
        n = {x: conn.execute("SELECT type FROM nodes WHERE id=?", (le[x],)).fetchone()
             for x in ("source_id", "target_id")}
        return {"id": le["edge_id"], "s": le["source_id"], "t": le["target_id"],
                "st": n["source_id"]["type"] if n["source_id"] else "?",
                "tt": n["target_id"]["type"] if n["target_id"] else "?"}

    def phase_d(self, conn):
        for le, row, d, cand in self.label_decisions(conn):
            if cand is not None and cand["source_type"] == "derivation":
                (cand["reasoning_type"], cand["hand_only"],
                 cand["why"]) = classify_derivation(conn, cand["source_id"])
                if d["kind"] == "needs" and d.get("action") == "retract+refile" and cand["hand_only"]:
                    d = dict(d, action="hand-only")
            if cand is not None and not any(c["edge_id"] == cand["edge_id"] for c in self.candidates):
                if cand["source_type"] == "derivation":
                    cand["covered_path"] = premise_path(conn, cand["source_id"], cand["target_id"])
                cand["is_cites"] = self.still_cites(conn, row)
                self.candidates.append(cand)
            if d["kind"] == "wrong-target":
                continue
            if cand is not None and cand.get("covered_path") and d["kind"] == "needs":
                # the premise is already wired: the cites edge is redundant context
                d = {"kind": "act", "to": "about", "code": "already-covered"}
            if self.claimed.get(row["id"], "skipped-not-cites") not in ("applied", "planned", "skipped-not-cites"):
                continue
            if self.still_cites(conn, row) and self.is_keeper(conn, row["s"], row["t"]):
                self.add("d", row, None, "keeper", "refused:not-loose-cites")
                continue
            if d["kind"] == "needs":
                if not self.still_cites(conn, row):
                    self.add("d", row, None, d["bucket"], "skipped-not-cites")
                    continue
                extra = ({"reasoning_type": cand["reasoning_type"]}
                         if cand is not None and "reasoning_type" in cand
                         and d["action"] in ("retract+refile", "hand-only") else {})
                self.add("d", row, None, d["bucket"], "needs-seat-read",
                         bucket=d["bucket"], action=d["action"], **extra)
                continue
            self.act(conn, "d", row, d["to"], d["code"], d.get("fallback"))

    def phase_e(self, conn):
        for le, row, d, cand in self.label_decisions(conn):
            if d["kind"] != "wrong-target":
                continue
            if self.claimed.get(row["id"], "skipped-not-cites") not in ("applied", "planned", "skipped-not-cites"):
                continue
            if self.still_cites(conn, row) and self.is_keeper(conn, row["s"], row["t"]):
                self.add("e", row, None, "keeper", "refused:not-loose-cites")
                continue
            self.act(conn, "e", row, "about", "wrong-target")
            self.wrong_target.append({"edge_id": row["id"], "source_id": row["s"],
                                      "target_id": row["t"]})

    def phase_f(self, conn):
        """frozen-history: every loose cites whose SOURCE is non-current and
        that no earlier phase touched (still a cites, not in the labels file,
        no ledger entry). The keeper pair is never selected."""
        rows = conn.execute(
            "SELECT e.id AS id, e.source_id AS s, e.target_id AS t, ns.type AS st, nt.type AS tt, "
            "ns.is_current AS sc, ns.status AS ss, nt.is_current AS tc, nt.status AS ts "
            "FROM edges e JOIN nodes ns ON ns.id = e.source_id JOIN nodes nt ON nt.id = e.target_id "
            "WHERE e.relation = 'cites' ORDER BY e.id").fetchall()
        for r in rows:
            # The claimed filter matters in a dry run: an edge an earlier phase
            # only PLANNED is still a cites in the DB, so without it act() would
            # record a spurious (f, skipped-not-cites) entry.
            if (r["id"] in self.claimed or (r["s"], r["t"]) in self.labelled
                    or self.is_keeper(conn, r["s"], r["t"])
                    or not _non_current(r["sc"], r["ss"])):
                continue
            self.frozen_meta[r["id"]] = {
                "edge_id": r["id"], "source_id": r["s"], "target_id": r["t"],
                "source_status": "retracted" if r["ss"] == "retracted" else "superseded",
                "target_current": not _non_current(r["tc"], r["ts"])}
            self.act(conn, "f", r, "about", FROZEN_CODE)

    # -- driver -----------------------------------------------------------
    def go(self):
        self._labelled_in_bulk = self.labelled_in_bulk()     # as the graph stands BEFORE any retype
        self.audit["before"] = self.snapshot_audit()
        prev = self.audit["before"]
        try:
            for ph in PHASES:
                if ph not in self.phases:
                    continue
                conn = self.open()
                try:
                    if ph == "a":
                        self.phase_a(conn)
                    elif ph == "b":
                        self.phase_b(conn)
                    elif ph == "c":
                        self.phase_c(conn)
                    elif ph == "d":
                        self.phase_d(conn)
                    elif ph == "e":
                        self.phase_e(conn)
                    else:
                        self.phase_f(conn)
                finally:
                    conn.close()
                if not self.apply:
                    continue
                after = self.snapshot_audit()
                self.audit["after_phase"][ph] = after
                new = audit_regression(prev, after)
                if new:
                    self.status = "aborted-audit"
                    self.abort = {"phase": ph, "new_cycles": new,
                                  "before_count": prev["cycle_count"], "after_count": after["cycle_count"]}
                    self.audit["final"] = after
                    return
                prev = after
        except Exception:
            self.audit["final"] = prev      # the last audit that completed
            raise
        self.audit["final"] = prev
        if self.apply and self.status == "complete" and self.loose_remaining()["total"] > 0:
            self.status = "complete-with-remaining"

    # -- reports ----------------------------------------------------------
    def refile_by_source(self):
        """Derivation-source premise candidates GROUPED BY SOURCE NODE: the
        seat acts once per source node, so one entry carries all of that
        source's targets."""
        groups = {}
        for c in sorted(self.candidates, key=lambda c: c["edge_id"]):
            if c["source_type"] != "derivation" or c.get("covered_path") or not self.stays_cites(c):
                continue
            if c["source_id"] not in groups:
                g = {"source_id": c["source_id"], "source_type": c["source_type"],
                     "action": "hand-only" if c["hand_only"] else "retract+refile",
                     "reasoning_type": c["reasoning_type"]}
                if c["hand_only"]:
                    g["why"] = c["why"]
                g["targets"] = []
                groups[c["source_id"]] = g
            g = groups[c["source_id"]]
            g["targets"].append({"edge_id": c["edge_id"], "target_id": c["target_id"],
                                 "reason": c["reason"], "relies": c["relies"]})
        return list(groups.values())

    def stays_cites(self, c):
        """True when the candidate's edge is a `cites` edge now and no phase of
        this run retypes (or plans to retype) it."""
        return bool(c.get("is_cites")) and self.claimed.get(c["edge_id"]) not in ("applied", "planned")

    def already_covered(self):
        """Derivation-source premise candidates whose target is already a
        transitive premise ancestor of the source: no refile needed."""
        return [{"source_id": c["source_id"], "target_id": c["target_id"],
                 "edge_id": c["edge_id"], "path": c["covered_path"], "reason": c["reason"]}
                for c in sorted(self.candidates, key=lambda c: c["edge_id"])
                if c.get("covered_path")]

    def promotion_candidates(self):
        """Premise candidates that are not on the refile or already-covered
        lists, one entry per edge: non-derivation sources, and derivation
        sources whose edge this run retypes anyway (a `relies` edge labelled
        correct-about). `retyped` is the relation the edge was (or would be)
        retyped to, else None."""
        return [{"source_id": c["source_id"], "source_type": c["source_type"],
                 "target_id": c["target_id"], "edge_id": c["edge_id"],
                 "reason": c["reason"], "relies": c["relies"],
                 "retyped": self.retyped_to.get(c["edge_id"])}
                for c in sorted(self.candidates, key=lambda c: c["edge_id"])
                if c["source_type"] != "derivation"
                or (not c.get("covered_path") and not self.stays_cites(c)
                    and self.retyped_to.get(c["edge_id"]))]

    def lost_relation(self):
        """Sampled edges whose EFFECTIVE label names a non-addable relation,
        grouped by stratum (source type -> target type). Computed once, after
        the phases, so each entry can say which phase acted on its edge."""
        if self._lost is not None:
            return self._lost
        addable_all = {r for r, c in self.core.EDGE_CLASSIFICATIONS.items()
                       if c.get("addable_after_creation")}
        sampled = {}      # (source, target) -> (edge_id, stratum, effective label or None)
        for e in self.rep["edges"]:
            sampled[(e["source_id"], e["target_id"])] = (
                e["edge_id"], f"{e['source_type']}->{e['target_type']}", e["label"])
        conn = self.open()
        try:
            for le in self.labels["edges"]:
                key = (le["source_id"], le["target_id"])
                row = self.label_row(conn, le)
                stratum = f"{row['st']}->{row['tt']}"
                if key in sampled and row["st"] == "?":
                    stratum = sampled[key][1]
                # the labels file is the double-labelled pass: it wins over the
                # report's single label, even when its labels disagree (None)
                sampled[key] = (le["edge_id"], stratum, effective_label(le))
            acted = {}
            for en in self.entries:
                if en["status"] in ("applied", "planned") and en["to"] == "about":
                    acted.setdefault((en["source_id"], en["target_id"]), en["phase"])
            strata = {}
            for (s, t), (eid, stratum, label) in sampled.items():
                if label is None:
                    continue
                name = (label.get("relation_if_any") or "").strip()
                if not name or _norm(name) == "wrong-target":
                    continue       # names nothing (wrong-target is a flag, not a relation)
                if _norm(name) in addable_all:
                    continue       # an addable relation is not lost
                if self.is_keeper(conn, s, t):
                    continue       # the legitimate cites is never acted on, so nothing is lost
                phase = acted.get((s, t))
                strata.setdefault(stratum, []).append({
                    "edge_id": eid, "source_id": s, "target_id": t,
                    "relation_named": name, "reason": label.get("reason", ""),
                    "phase_applied": phase, "action_taken": "about" if phase else "none"})
        finally:
            conn.close()
        strata = {k: sorted(v, key=lambda x: x["edge_id"]) for k, v in sorted(strata.items())}
        self._lost = {"header": {"total": sum(len(v) for v in strata.values()),
                                 "per_stratum": {k: len(v) for k, v in strata.items()}},
                      "strata": strata}
        return self._lost

    def labelled_in_bulk(self):
        """Labelled edges (still `cites`, not the legitimate pair) that sit in a
        bulk-eligible stratum: bulk skips them, the seat's labels decide."""
        ok = {k for k, v in self.eligibility.items() if v["eligible"]}
        out = []
        conn = self.open()
        try:
            for le in self.labels["edges"]:
                row = conn.execute(
                    "SELECT e.id AS id, ns.type AS st, nt.type AS tt FROM edges e "
                    "JOIN nodes ns ON ns.id = e.source_id JOIN nodes nt ON nt.id = e.target_id "
                    "WHERE e.source_id=? AND e.target_id=? AND e.relation='cites'",
                    (le["source_id"], le["target_id"])).fetchone()
                if row is None or f"{row['st']}->{row['tt']}" not in ok:
                    continue
                if self.is_keeper(conn, le["source_id"], le["target_id"]):
                    continue
                out.append({"edge_id": row["id"], "source_id": le["source_id"],
                            "target_id": le["target_id"], "stratum": f"{row['st']}->{row['tt']}"})
        finally:
            conn.close()
        return out

    def frozen_history(self):
        """The edges phase f selected (every status but skipped), by edge id:
        {edge_id, source_id, target_id, source_status, target_current}."""
        ids = {e["edge_id"] for e in self.entries
               if e["phase"] == "f" and e["reason"] == FROZEN_CODE
               and e["status"] != "skipped-not-cites"}
        return [self.frozen_meta[i] for i in sorted(ids)]

    def frozen_history_counts(self):
        per = {}
        for e in self.entries:
            if (e["phase"] == "f" and e["reason"] == FROZEN_CODE
                    and e["status"] != "skipped-not-cites"):
                per[e["stratum"]] = per.get(e["stratum"], 0) + 1
        return {"total": sum(per.values()), "per_stratum": dict(sorted(per.items()))}

    def k7(self):
        retyped = {e["stratum"] for e in self.entries
                   if e["reason"] == "no-textual-trace" and e["status"] in ("applied", "planned")}
        out = {}
        for st in sorted(retyped):
            sample = [e for e in self.rep["edges"] if f"{e['source_type']}->{e['target_type']}" == st
                      and e["target_id_named_in_source_text"]]
            out[st] = {"named_sampled": len(sample),
                       "named_mis_filed": sum(1 for e in sample if e["label"]["label"] == "mis-filed-premise")}
        return out

    def header(self, seat, lineage):
        def tally(key_fn):
            d = {}
            for e in self.entries:
                d[key_fn(e)] = d.get(key_fn(e), 0) + 1
            return d
        per_phase = {}
        for e in self.entries:
            st = e["status"].split(":")[0] if e["status"].startswith("refused") else e["status"]
            per_phase.setdefault(e["phase"], {})
            per_phase[e["phase"]][st] = per_phase[e["phase"]].get(st, 0) + 1
        audit = {"before": _audit_json(self.audit["before"]),
                 "after_phase": {k: _audit_json(v) for k, v in self.audit["after_phase"].items()},
                 "final": _audit_json(self.audit["final"])}
        return {
            "mode": "apply" if self.apply else "dry-run",
            "seat": seat, "lineage": lineage, "status": self.status,
            "phases": [p for p in PHASES if p in self.phases],
            "counts_per_phase": per_phase,
            "counts_per_reason": tally(lambda e: e["reason"]),
            "counts_per_status": tally(lambda e: e["status"].split(":")[0]
                                       if e["status"].startswith("refused") else e["status"]),
            "fallbacks": self.fallbacks,
            "refile_counts": {"edges": sum(len(g["targets"]) for g in self.refile_by_source()),
                              "sources": len(self.refile_by_source())},
            "hand_only_counts": {
                "edges": sum(len(g["targets"]) for g in self.refile_by_source()
                             if g["action"] == "hand-only"),
                "sources": sum(1 for g in self.refile_by_source() if g["action"] == "hand-only")},
            "already_covered_counts": {
                "edges": len(self.already_covered()),
                "sources": len({c["source_id"] for c in self.already_covered()})},
            "promotion_candidate_count": len(self.promotion_candidates()),
            "labelled_in_bulk_strata": len(self._labelled_in_bulk),
            "lost_relation_counts": self.lost_relation()["header"],
            "frozen_history_counts": self.frozen_history_counts(),
            DUP_ABOUT_BUCKET: self.dup_about_unremovable(),
            "loose_cites_remaining": self.loose_remaining(),
            **({"backup": self.backup} if self.backup else {}),
            "wrong_target": self.wrong_target,
            "named_edge_mis_filed_rate": self.k7(),
            "bulk_eligibility": self.eligibility,
            "audit": audit,
            "unchecked_axes": list(self.rep.get("unchecked_axes", [])),
            **({"error": self.error} if self.error else {}),
        }


def _audit_json(a):
    if a is None:
        return None
    return {"cycle_count": a["cycle_count"],
            "cycles": [{"family": c[0], "path": list(c[1])} for c in a["cycles"]]}


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------

def _write_private(path: Path, obj):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1)
    os.chmod(str(path), 0o600)


def _make_dir(d: Path):
    if d.exists():
        return
    d.mkdir(parents=True, mode=0o700)
    os.chmod(str(d), 0o700)


def needs_list(entries):
    return [{"edge_id": e["edge_id"], "source_id": e["source_id"], "target_id": e["target_id"],
             "bucket": e["bucket"], "action": e["action"], "phase": e["phase"]}
            for e in entries if e["status"] == "needs-seat-read"]


def print_plan(run, header, out=None):
    out = out or sys.stdout
    verb = "WOULD retype" if not run.apply else "retype"
    for ph in PHASES:
        if ph not in run.phases:
            continue
        es = [e for e in run.entries if e["phase"] == ph]
        print(f"phase {ph}: {len(es)} edge(s)", file=out)
        for e in es:
            if e["status"] == "needs-seat-read":
                print(f"  edge {e['edge_id']} {e['source_id']} -> {e['target_id']} "
                      f"needs-seat-read [{e['bucket']}] action={e['action']}", file=out)
            elif e["status"] == "skipped-not-cites":
                print(f"  edge {e['edge_id']} {e['source_id']} -> {e['target_id']} "
                      f"skipped: no longer a cites edge ({e['reason']}) [{e['status']}]", file=out)
            elif e["reason"] == "keeper":
                print(f"  edge {e['edge_id']} {e['source_id']} -> {e['target_id']} "
                      f"kept as cites (the legitimate pair is never retyped) [{e['status']}]", file=out)
            else:
                print(f"  edge {e['edge_id']} {e['source_id']} -> {e['target_id']} "
                      f"{verb} cites -> {e['to']} ({e['reason']}) [{e['status']}]", file=out)
    refile = run.refile_by_source()
    hand = [g for g in refile if g["action"] == "hand-only"]
    print(f"refile list: {len(refile)} source(s) "
          f"(retract+refile: {len(refile) - len(hand)}, hand-only: {len(hand)}); "
          f"{sum(len(g['targets']) for g in refile)} edge(s); "
          f"hand-only sources are never refiled, the premise role is read from the chain",
          file=out)
    for g in refile:
        tag = (f" [HAND-ONLY {g['reasoning_type'] or 'reasoning type unreadable'}: {g['why']}]"
               if g["action"] == "hand-only" else "")
        print(f"  source {g['source_id']} ({g['source_type']}): "
              f"{', '.join(t['target_id'] for t in g['targets'])}{tag}", file=out)
    cov = run.already_covered()
    print(f"already covered (premise already wired): {len(cov)}", file=out)
    for c in cov:
        print(f"  edge {c['edge_id']}: {' -> '.join(c['path'])}", file=out)
    promo = run.promotion_candidates()
    print(f"promotion candidates: {len(promo)}", file=out)
    for c in promo:
        print(f"  candidate edge {c['edge_id']} {c['source_id']} ({c['source_type']}) -> "
              f"{c['target_id']}: {c['reason']}"
              + (f" (retyped: {c['retyped']})" if c.get("retyped") else ""), file=out)
    lost = run.lost_relation()
    print(f"lost-relation (a non-addable relation was named; the seat re-homes these by hand): "
          f"{lost['header']['total']}", file=out)
    for stratum, es in lost["strata"].items():
        for x in es:
            print(f"  [{stratum}] edge {x['edge_id']} {x['source_id']} -> {x['target_id']} names "
                  f"{x['relation_named']!r}: {x['reason']} "
                  f"(phase {x['phase_applied']}, action {x['action_taken']})", file=out)
    fh = run.frozen_history_counts()
    if "f" in run.phases:
        print(f"frozen-history (phase f: a loose cites with a non-current source -> about; "
              f"spot-read some before --apply): {fh['total']}", file=out)
        for k, n in fh["per_stratum"].items():
            print(f"  [{k}] {n}", file=out)
    dup = run.dup_about_unremovable()
    print(f"{DUP_ABOUT_BUCKET} (an about already exists, the engine never deletes; the seat decides "
          f"by hand): {dup['count']}", file=out)
    for x in dup["entries"]:
        print(f"  edge {x['edge_id']} {x['source_id']} -> {x['target_id']}", file=out)
    rem = run.loose_remaining()
    print(f"loose_cites_remaining ({'after this run' if run.apply else 'projected after the plan'}): "
          f"{rem['total']} (non-current source {rem['non_current_source']}, "
          f"non-current target {rem['non_current_target']})", file=out)
    for k, n in rem["per_stratum"].items():
        print(f"  [{k}] {n}", file=out)
    if run.backup:
        print(f"backup: {run.backup['path']} ({run.backup['bytes']} bytes, "
              f"{run.backup['edges']} edges)", file=out)
    print(f"status: {run.status}", file=out)
    print("SUMMARY", file=out)
    print(json.dumps(header, indent=1), file=out)


# ---------------------------------------------------------------------------
# backup (before the first write of an --apply)
# ---------------------------------------------------------------------------

def _backup_db(src_path, dest_path):
    """A `sqlite3` online backup of the database at `src_path` into `dest_path`."""
    src = sqlite3.connect(_ro_uri(src_path), uri=True)
    try:
        dst = sqlite3.connect(str(dest_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _edge_count(path):
    c = sqlite3.connect(_ro_uri(path), uri=True)
    try:
        return c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    finally:
        c.close()


def take_backup(db_path, out_dir, ts):
    """Back the graph up into `out_dir` (0600), verify it opens and has the live
    edge count, and return {path, bytes, edges}. Any failure -> Refusal (the
    caller has written nothing to the graph yet), and no half backup is left."""
    dest = Path(out_dir) / f"knowledge-before-cites-step3-{ts}.db"
    made = False
    try:
        _make_dir(Path(out_dir))
        os.close(os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        made = True
        _backup_db(db_path, dest)
        os.chmod(str(dest), 0o600)
        live, got = _edge_count(db_path), _edge_count(dest)
        if live != got:
            raise RuntimeError(f"the backup holds {got} edge(s) but the graph holds {live}")
        return {"path": str(dest), "bytes": dest.stat().st_size, "edges": got}
    except Exception as exc:
        if made:
            try:
                dest.unlink()
            except OSError:
                pass
        raise Refusal(f"cannot take the backup the --apply needs first ({type(exc).__name__}: {exc}); "
                      f"nothing was written to the graph and no retype was tried")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _parse(argv):
    ap = argparse.ArgumentParser(
        description="Migrate this seat's own loose `cites` edges (#2243 step 3 part B).",
        epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report")
    ap.add_argument("--labels")
    ap.add_argument("--handcheck")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out-dir")
    ap.add_argument("--plan-out")
    ap.add_argument("--phases", default=",".join(PHASES))
    try:
        return ap.parse_args(argv)
    except SystemExit as exc:
        if exc.code in (0, None):
            raise HelpShown()
        raise Refusal(f"bad command line (argparse exit {exc.code})")


def _main(argv):
    args = _parse(argv)
    phases = [p.strip() for p in args.phases.split(",") if p.strip()]
    _need(phases and all(p in PHASES for p in phases),
          f"--phases: must be a subset of {','.join(PHASES)}, got {args.phases!r}")
    _need(args.report, "--report is required")
    _need(not (args.apply and args.plan_out), "--plan-out is for a dry run only; drop one of --apply / --plan-out")
    _need(not (args.plan_out and os.path.lexists(args.plan_out)),
          f"--plan-out {args.plan_out!r} already exists; it is never overwritten")
    rep = validate_report(_read_json(args.report, "report"))
    labels = validate_labels(_read_json(args.labels, "labels")) if args.labels else None

    home = Path(os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram")).expanduser().resolve()
    cfg = _read_json(home / "config.json", "config")
    agent, lineage = cfg.get("agent_name"), cfg.get("self_lineage")
    _need(agent and lineage, f"config.json at {home} lacks agent_name / self_lineage")
    _need(rep["seat"] == agent,
          f"report is for seat {rep['seat']!r} but this graph's agent_name is {agent!r}: own graph only")
    _need(rep["lineage"] == lineage,
          f"report lineage {rep['lineage']!r} differs from this graph's self_lineage {lineage!r}: own graph only")
    # #3962: classify the store path without opening it (a `mode=ro` open of a FIFO blocks on macOS, and
    # `exists()` calls a loop, a dangling link and an unsearchable parent "missing"). Every `_ro_uri` connect below
    # reads `core.DB_PATH` (= this path after `_configure_paths(home)`) or a file this run made itself.
    try:
        owner_read.check_store_path(home / "knowledge.db")
    except FileNotFoundError:
        raise Refusal(f"no knowledge.db under {home}")
    except owner_read.StoreUnusable as exc:
        raise Refusal(str(exc))

    if args.apply:
        _need(labels is not None, "--apply requires --labels")
        _need(args.handcheck, "--apply requires --handcheck (cross-lineage hand-check evidence)")
        hc = validate_handcheck(_read_json(args.handcheck, "handcheck"), rep)
        _need(hc["reviewer_lineage"] != rep["lineage"],
              f"handcheck reviewer_lineage {hc['reviewer_lineage']!r} equals the report lineage: "
              f"the hand-check must come from a different model class")
        need, n_ca = handcheck_minimum(rep)
        distinct = len(set(hc["edges_read"]))
        _need(distinct >= need,
              f"handcheck.edges_read has {distinct} distinct edge id(s) ({len(hc['edges_read'])} listed); "
              f"need at least {need} (min(20, {n_ca} correct-about in derivation-source strata), "
              f"and at least 1)")
        check_handcheck_sources(hc, rep)
        _need(hc["disagree"] <= 2,
              f"handcheck disagree={hc['disagree']} > 2: the whole apply is refused; the derivation "
              f"strata then need per-edge review by the seat")

    core, cornerstone, dagc = _load_engine()
    core._configure_paths(home)

    if labels is not None:
        conn = sqlite3.connect(_ro_uri(core.DB_PATH), uri=True)
        try:
            for le in labels["edges"]:
                r = conn.execute("SELECT id FROM edges WHERE source_id=? AND target_id=? "
                                 "AND relation='cites'", (le["source_id"], le["target_id"])).fetchone()
                _need(r is None or r[0] == le["edge_id"],
                      f"labels edge_id {le['edge_id']} for {le['source_id']} -> {le['target_id']} "
                      f"is edge {r[0] if r else None} in this graph: wrong graph or stale labels")
        finally:
            conn.close()

    out_dir = None
    backup = None
    if args.apply:
        out_dir = Path(args.out_dir) if args.out_dir else home / "migrations" / "cites-step3"
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = take_backup(core.DB_PATH, out_dir, ts)       # refuses before any write

    run = Run(core, cornerstone, dagc, home, rep, labels or {"edges": []}, args.apply, phases)
    run.backup = backup
    if labels is None and any(p in phases for p in ("d", "e")):
        print("note: no --labels given; phases d and e plan nothing", file=sys.stderr)
    failure = None
    try:
        run.go()
    except Exception as exc:
        if not args.apply:
            raise
        failure = exc            # a ledger is still written below, whatever broke
        run.status = "aborted-error"
        run.error = f"{type(exc).__name__}: {exc}"
    if run._labelled_in_bulk:
        print(f"WARNING: {len(run._labelled_in_bulk)} labelled edge(s) sit in bulk-eligible strata; "
              f"bulk (phase c) skips them and their labels (phase d) decide:", file=sys.stderr)
        for x in run._labelled_in_bulk:
            print(f"  edge {x['edge_id']} {x['source_id']} -> {x['target_id']} [{x['stratum']}]",
                  file=sys.stderr)
    try:
        header = run.header(agent, lineage)
    except Exception:
        if failure is None:
            raise
        header = {"mode": "apply", "seat": agent, "lineage": lineage, "status": run.status,
                  "error": run.error, **({"backup": run.backup} if run.backup else {})}

    if args.apply:
        _make_dir(out_dir)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        _write_private(out_dir / f"ledger-{ts}.json", {"header": header, "entries": run.entries})
        _write_private(out_dir / f"refile-by-source-{ts}.json", run.refile_by_source())
        _write_private(out_dir / f"promotion-candidates-{ts}.json", run.promotion_candidates())
        _write_private(out_dir / f"already-covered-{ts}.json", run.already_covered())
        _write_private(out_dir / f"lost-relation-{ts}.json", run.lost_relation())
        _write_private(out_dir / f"needs-seat-read-{ts}.json", needs_list(run.entries))
        _write_private(out_dir / f"frozen-history-{ts}.json", run.frozen_history())
        print_plan(run, header)
        if failure is not None:
            print(f"ERROR: {run.error}. Retypes already committed are NOT rolled back (the engine "
                  f"commits per edge; the verb has no inverse). Ledger written to {out_dir}.",
                  file=sys.stderr)
            return 1
        if run.status == "aborted-audit":
            ab = run.abort
            print(f"ABORTED (aborted-audit) after phase {ab['phase']}: the DAG audit found "
                  f"cycle_count {ab['before_count']} -> {ab['after_count']}.", file=sys.stderr)
            for c in ab["new_cycles"]:
                print(f"  new cyclic nodes / cycle [{c[0]}]: {' -> '.join(c[1])}", file=sys.stderr)
            print("Retypes already committed in this and earlier phases are NOT rolled back "
                  "(the engine commits per edge; the verb has no inverse). Ledger written to "
                  f"{out_dir}.", file=sys.stderr)
            return 3
        return 0

    print_plan(run, header)
    if args.plan_out:
        _write_private(Path(args.plan_out), {"header": header, "entries": run.entries,
                                             "refile_by_source": run.refile_by_source(),
                                             "promotion_candidates": run.promotion_candidates(),
                                             "already_covered": run.already_covered(),
                                             "lost_relation": run.lost_relation(),
                                             "frozen_history": run.frozen_history(),
                                             "needs_seat_read": needs_list(run.entries)})
    return 0


def main(argv=None) -> int:
    try:
        return _main(sys.argv[1:] if argv is None else list(argv))
    except HelpShown:
        return 0
    except Refusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # unexpected
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
