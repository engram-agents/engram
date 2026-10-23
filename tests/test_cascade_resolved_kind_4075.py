"""Contract cells for #4075: the cascade marker kind `resolved` for contradictions.

Written BEFORE the implementation (Lei's contract-first ruling, #3045).  Design source:
Borges's design v1 on the issue (Lei's ruling, 2026-10-22), plus the builder's one decision
where the design is silent (several pending events on one contradiction).

The contract:
  1. A contradiction's SIDES are its legs.  A retracted side withdraws `acknowledged` and the
     refusal names the side (`acknowledge_blocked_by`).
  2. A superseded side allows all four kinds (retracted, superseded, acknowledged, resolved).
  3. `resolved` is eligible ONLY on contradiction nodes; every other type refuses it, naming why.
  4. `resolved` needs `resolving_node_id`: claim-bearing, current, and either a current side of
     this contradiction or the LIVE successor/replacement of a revised side (the chain walked to
     its live end, never one hop).  Every other resolver is refused and a refusal changes NOTHING.
  5. `resolved` stamps the marker `resolved` (replacement id = the resolver), delegates the status
     write to `_resolve_impl` (`resolves` edge, `resolved`/`partially_resolved` by confidence),
     and is retry-safe.
  6. `acknowledged` / `retracted` / `superseded` never touch the contradiction's status.
  7. `acknowledged` on a superseded side writes `acknowledged_contradicts` to the LIVE successor
     (chain end), keeps the `contradicts` edge, and a later supersede of that successor marks the
     contradiction again.
  8. Migration rung 10 (CASCADE_RESOLVED_KIND_RUNG) rebuilds `cascade_markers` with the fourth kind, preserving every row;
     running it twice is a no-op; `PRAGMA user_version` is 10 afterwards.
  9. Several pending events on one contradiction: `acknowledged` is refused if ANY event's root
     op is a retract (over-surface, never pick one).
 10. Derivation eligibility is unchanged.
"""

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 - must follow sys.path setup

import engram_core
import engram_derive
import engram_observation
import engram_revision


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def g(monkeypatch):
    """Fresh family modules against a temp store, bootstrapped through the real ladder."""
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    monkeypatch.delenv("ENGRAM_SKIP_PRE_MIGRATION_BACKUP", raising=False)
    d = tempfile.mkdtemp(prefix="engram_test_4075_")
    rev = fresh_module(engram_revision)
    import importlib
    obs = importlib.import_module("engram_observation")
    derive = importlib.import_module("engram_derive")
    core = sys.modules["engram_core"]
    core._configure_paths(d)
    core._ensure_data_dir()
    core._get_db().close()
    ns = types.SimpleNamespace(rev=rev, obs=obs, derive=derive, core=core, seq=[0], dir=d)
    try:
        yield ns
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _ob(g, claim, quote_type="attributed_analysis"):
    g.seq[0] += 1
    r = json.loads(g.obs._add_observation_impl(
        url=f"https://example.com/s4075-{g.seq[0]}", title=f"Source {g.seq[0]}",
        quoted_text=claim, interpretation=f"Interpretation: {claim}", claim=claim,
        quote_type=quote_type,
    ))
    assert "error" not in r, r
    return r["observation_id"]


def _dv(g, claim, supporting):
    r = json.loads(g.derive._derive_impl(
        claim=claim, supporting_ids=",".join(supporting),
        logical_chain=f"Derivation: {claim}", reasoning_type="deductive_modus_ponens",
    ))
    assert "error" not in r, r
    return r["derivation_id"]


def _ct(g, a, b, text="a versus b"):
    r = json.loads(g.derive._contradict_impl(node_id_a=a, node_id_b=b, description=text))
    assert "contradiction_id" in r, r
    return r["contradiction_id"]


def _supersede(g, old, new):
    r = json.loads(g.rev._supersede_impl(old_node_id=old, new_node_id=new, supersede_reason="r"))
    assert "error" not in r, r
    return r


def _retract(g, node, replacement=None):
    kw = {"replacement_id": replacement} if replacement else {
        "replacement_absent_because": "NONE_BECAUSE_NO_SUCCESSOR"}
    r = json.loads(g.rev._retract_impl(node_id=node, error_type="other", reason="r", **kw))
    assert "error" not in r, r
    return r


def _resolve(g, node, kind, **kw):
    return json.loads(g.rev._cascade_resolve_impl(node_id=node, resolution_kind=kind, **kw))


def _shapes(g, node):
    c = g.core._get_db()
    try:
        return g.rev._cascade_eligible_shapes(c, node)
    finally:
        c.close()


def _q(g, sql, args=()):
    c = g.core._get_db()
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _markers(g, node):
    return _q(g, "SELECT * FROM cascade_markers WHERE node_id = ? ORDER BY id", (node,))


def _node(g, nid):
    return _q(g, "SELECT * FROM nodes WHERE id = ?", (nid,))[0]


def _edges(g, source=None, relation=None):
    sql, args = "SELECT source_id, target_id, relation FROM edges WHERE 1=1", []
    if source:
        sql += " AND source_id = ?"
        args.append(source)
    if relation:
        sql += " AND relation = ?"
        args.append(relation)
    return _q(g, sql + " ORDER BY id", args)


def _snapshot(g, ct):
    """Everything a refused call must leave alone."""
    return {
        "markers": _markers(g, ct),
        "ct": _node(g, ct),
        "edges": _q(g, "SELECT * FROM edges ORDER BY id"),
        "nodes": _q(g, "SELECT COUNT(*) AS c FROM nodes")[0]["c"],
        "edit_history": _q(g, "SELECT COUNT(*) AS c FROM edit_history")[0]["c"],
    }


class _Sc:
    """a (to be revised) vs b (stands), contradiction ct between them."""


def _scenario(g):
    s = _Sc()
    s.a = _ob(g, "Alpha holds in every case.")
    s.b = _ob(g, "Alpha fails in some cases.")
    s.ct = _ct(g, s.a, s.b)
    return s


# --------------------------------------------------------------------------- cell 1, 2, 9


def test_cell1_retracted_side_blocks_acknowledged_and_names_the_side(g):
    s = _scenario(g)
    _retract(g, s.a)
    shapes = _shapes(g, s.ct)
    assert shapes["eligible"] == ["retracted", "superseded", "resolved"]
    assert shapes["acknowledge_blocked_by"] == [s.a]
    before = _snapshot(g, s.ct)
    r = _resolve(g, s.ct, "acknowledged")
    assert "error" in r, r
    assert r["acknowledge_blocked_by"] == [s.a]
    assert "acknowledged" not in r["eligible"]
    assert _snapshot(g, s.ct) == before


def test_cell2_superseded_side_allows_all_four_kinds(g):
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    _supersede(g, s.a, a2)
    shapes = _shapes(g, s.ct)
    assert shapes["eligible"] == ["retracted", "superseded", "acknowledged", "resolved"]
    assert shapes["acknowledge_blocked_by"] == []


def test_cell9_two_pending_events_one_retract_rooted_blocks_acknowledged(g):
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    _supersede(g, s.a, a2)          # event 1: supersede-rooted, side a
    _retract(g, s.b)                # event 2: retract-rooted, side b
    pend = [m for m in _markers(g, s.ct) if m["status"] == "pending"]
    assert sorted(m["root_op"] for m in pend) == ["retract", "supersede"], pend
    shapes = _shapes(g, s.ct)
    assert "acknowledged" not in shapes["eligible"]
    assert shapes["acknowledge_blocked_by"] == [s.b]
    before = _snapshot(g, s.ct)
    r = _resolve(g, s.ct, "acknowledged")
    assert "error" in r and r["acknowledge_blocked_by"] == [s.b], r
    assert _snapshot(g, s.ct) == before


def test_cell9_order_of_the_two_events_does_not_matter(g):
    s = _scenario(g)
    _retract(g, s.b)
    a2 = _ob(g, "Alpha holds in most cases.")
    _supersede(g, s.a, a2)
    shapes = _shapes(g, s.ct)
    assert shapes["acknowledge_blocked_by"] == [s.b]
    assert "acknowledged" not in shapes["eligible"]


def test_cell9_both_sides_retracted_names_both(g):
    s = _scenario(g)
    _retract(g, s.a)
    _retract(g, s.b)
    shapes = _shapes(g, s.ct)
    assert shapes["acknowledge_blocked_by"] == sorted([s.a, s.b])


# --------------------------------------------------------------------------- cell 3


def test_cell3_resolved_is_refused_on_a_derivation_with_a_reason(g):
    ob = _ob(g, "Base fact.")
    dv = _dv(g, "A derived claim.", [ob])
    _retract(g, ob)
    assert [m["status"] for m in _markers(g, dv)] == ["pending"]
    res = _ob(g, "Some resolver.")
    before = _snapshot(g, dv)
    r = _resolve(g, dv, "resolved", resolving_node_id=res)
    assert "error" in r, r
    assert "contradiction" in r["error"].lower(), r["error"]
    assert _snapshot(g, dv) == before


def test_cell3_resolved_is_refused_on_any_non_contradiction_type(g):
    ob = _ob(g, "A plain observation.")
    res = _ob(g, "Some resolver.")
    r = _resolve(g, ob, "resolved", resolving_node_id=res)
    assert "error" in r and "contradiction" in r["error"].lower(), r


def test_cell3_derivation_eligibility_is_unchanged_and_never_lists_resolved(g):
    ob = _ob(g, "Base fact.")
    dv = _dv(g, "A derived claim.", [ob])
    ob2 = _ob(g, "Base fact, restated.")
    _supersede(g, ob, ob2)
    sup = _shapes(g, dv)
    assert sup["eligible"] == ["retracted", "superseded", "acknowledged"]
    assert sup["acknowledge_blocked_by"] == []
    ob3 = _ob(g, "Second base fact.")
    dv2 = _dv(g, "Another derived claim.", [ob3])
    _retract(g, ob3)
    ret = _shapes(g, dv2)
    assert ret["eligible"] == ["retracted", "superseded"]
    assert ret["acknowledge_blocked_by"] == [ob3]


# --------------------------------------------------------------------------- cell 4


def _refusal_case(g, which):
    """Return (scenario, resolving_node_id-or-None) for one refusal shape."""
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    a3 = _ob(g, "Alpha holds in many cases.")
    _supersede(g, s.a, a2)
    _supersede(g, a2, a3)           # chain a -> a2 -> a3 (a3 is the live end)
    s.a2, s.a3 = a2, a3
    if which == "missing":
        return s, None
    if which == "unrelated":
        return s, _ob(g, "Unrelated claim.")
    if which == "contradiction_itself":
        return s, s.ct
    if which == "revised_side_itself":
        return s, s.a
    if which == "one_hop_off_the_chain":
        return s, a2                # non-current middle of the chain: one hop is not the live end
    if which == "not_claim_bearing":
        return s, _ct(g, _ob(g, "P."), _ob(g, "Not P."))
    if which == "nonexistent":
        return s, "ob_9999"
    if which == "non_current_other_side":
        c = _ob(g, "Replacement for b.")
        _supersede(g, s.b, c)
        return s, s.b
    raise AssertionError(which)


@pytest.mark.parametrize("which", [
    "missing", "unrelated", "contradiction_itself", "revised_side_itself",
    "one_hop_off_the_chain", "not_claim_bearing", "nonexistent", "non_current_other_side",
])
def test_cell4_resolved_refusals_change_nothing(g, which):
    s, resolver = _refusal_case(g, which)
    kw = {} if resolver is None else {"resolving_node_id": resolver}
    before = _snapshot(g, s.ct)
    r = _resolve(g, s.ct, "resolved", **kw)
    assert "error" in r, (which, r)
    assert r["error"].strip(), r
    assert _snapshot(g, s.ct) == before, which
    assert not _edges(g, relation="resolves"), which


def test_cell4_resolving_node_id_is_refused_for_the_other_kinds(g):
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    _supersede(g, s.a, a2)
    before = _snapshot(g, s.ct)
    for kind in ("acknowledged", "retracted", "superseded"):
        r = _resolve(g, s.ct, kind, resolving_node_id=s.b)
        assert "error" in r and "resolving_node_id" in r["error"], (kind, r)
    assert _snapshot(g, s.ct) == before


def test_cell4_replacement_id_is_not_the_resolver_for_resolved(g):
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    _supersede(g, s.a, a2)
    before = _snapshot(g, s.ct)
    r = _resolve(g, s.ct, "resolved", replacement_id=s.b)
    assert "error" in r and "resolving_node_id" in r["error"], r
    assert _snapshot(g, s.ct) == before


# --------------------------------------------------------------------------- cell 5


def test_cell5_resolved_with_a_current_side_confident_enough_resolves(g):
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    _supersede(g, s.a, a2)
    conf = _node(g, s.b)["confidence"]
    assert conf >= g.core.DEFAULT_RESOLUTION_THRESHOLD, conf
    r = _resolve(g, s.ct, "resolved", resolving_node_id=s.b)
    assert "error" not in r, r
    assert r["resolution_kind"] == "resolved"
    [m] = _markers(g, s.ct)
    assert (m["status"], m["resolution_kind"], m["resolution_replacement_id"]) == (
        "resolved", "resolved", s.b)
    assert m["resolved_at"]
    assert _node(g, s.ct)["status"] == "resolved"
    assert _node(g, s.ct)["resolved_by"] == s.b
    assert _edges(g, source=s.b, relation="resolves") == [
        {"source_id": s.b, "target_id": s.ct, "relation": "resolves"}]


def test_cell5_resolved_with_a_weak_resolver_is_partially_resolved(g):
    s = _scenario(g)
    weak = _ob(g, "A weak editorial resolver.", quote_type="editorial")
    assert _node(g, weak)["confidence"] < g.core.DEFAULT_RESOLUTION_THRESHOLD
    _retract(g, s.a, replacement=weak)
    # weak is the replacement of the retracted side a (live end of its chain)
    r = _resolve(g, s.ct, "resolved", resolving_node_id=weak)
    assert "error" not in r, r
    assert _node(g, s.ct)["status"] == "partially_resolved"
    [m] = _markers(g, s.ct)
    assert m["resolution_kind"] == "resolved" and m["resolution_replacement_id"] == weak


def test_cell5_a_second_identical_call_changes_nothing(g):
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    _supersede(g, s.a, a2)
    assert "error" not in _resolve(g, s.ct, "resolved", resolving_node_id=s.b)
    after_first = _snapshot(g, s.ct)
    second = _resolve(g, s.ct, "resolved", resolving_node_id=s.b)
    assert _snapshot(g, s.ct) == after_first, second
    assert len(_edges(g, source=s.b, relation="resolves")) == 1


def test_cell5_retry_after_the_status_write_landed_but_the_marker_did_not(g):
    """The phase split is not one transaction: `_resolve_impl` commits on its own connection.
    A retry must finish the job without a second resolves edge."""
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    _supersede(g, s.a, a2)
    first = json.loads(g.rev._resolve_impl(target_id=s.ct, resolving_node_id=s.b))
    assert first["status"] == "resolution_created", first
    assert [m["status"] for m in _markers(g, s.ct)] == ["pending"]
    r = _resolve(g, s.ct, "resolved", resolving_node_id=s.b)
    assert "error" not in r, r
    [m] = _markers(g, s.ct)
    assert (m["status"], m["resolution_kind"], m["resolution_replacement_id"]) == (
        "resolved", "resolved", s.b)
    assert len(_edges(g, source=s.b, relation="resolves")) == 1


def test_a_failing_status_write_leaves_every_marker_pending(g, monkeypatch):
    """The ORDER of the two effects: the status write is delegated first, so a death between the
    steps leaves the markers pending and a retry completes. Stamping first would leave the markers
    `resolved` with no `resolves` edge and the contradiction off the frontier for good (a retry
    would answer "no pending markers")."""
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    _supersede(g, s.a, a2)

    def _boom(**kw):
        raise RuntimeError("process died before the status write")

    monkeypatch.setattr(g.rev, "_resolve_impl", _boom)
    with pytest.raises(RuntimeError):
        _resolve(g, s.ct, "resolved", resolving_node_id=a2)
    ms = _markers(g, s.ct)
    assert ms and all(m["status"] == "pending" for m in ms), ms


def test_retracting_a_live_successor_resolver_reopens_the_contradiction(g):
    """Borges's ruling on #4088: `resolved` writes no side edge, because a side edge would say 'still
    contradicts'. A revised resolver is handled by the resolves-edge recompute, so retracting the
    resolver reopens the contradiction."""
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    _supersede(g, s.a, a2)
    r = _resolve(g, s.ct, "resolved", resolving_node_id=a2)
    assert "error" not in r, r
    assert _node(g, s.ct)["status"] == "resolved"
    assert _edges(g, source=s.ct, relation="acknowledged_contradicts") == []   # no side edge
    assert _edges(g, source=a2, relation="resolves") == [
        {"source_id": a2, "target_id": s.ct, "relation": "resolves"}]
    _retract(g, a2)
    # the resolves-edge recompute (#3586) reopens it: no edge, status back to open
    assert _edges(g, source=a2, relation="resolves") == []
    assert _node(g, s.ct)["status"] == "open"


def test_cell5_live_successor_of_a_revised_side_is_a_valid_resolver_chain_end_only(g):
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    a3 = _ob(g, "Alpha holds in many cases.")
    _supersede(g, s.a, a2)
    _supersede(g, a2, a3)
    r = _resolve(g, s.ct, "resolved", resolving_node_id=a2)
    assert "error" in r, "a2 is a hop, not the live end"
    r = _resolve(g, s.ct, "resolved", resolving_node_id=a3)
    assert "error" not in r, r
    [m] = _markers(g, s.ct)
    assert m["resolution_replacement_id"] == a3
    assert _node(g, s.ct)["status"] in ("resolved", "partially_resolved")


def test_cell5_replacement_of_a_retracted_side_is_a_valid_resolver(g):
    s = _scenario(g)
    fixed = _ob(g, "Alpha holds exactly when the input is sorted.")
    _retract(g, s.a, replacement=fixed)
    shapes = _shapes(g, s.ct)
    assert "resolved" in shapes["eligible"] and "acknowledged" not in shapes["eligible"]
    r = _resolve(g, s.ct, "resolved", resolving_node_id=fixed)
    assert "error" not in r, r
    assert _node(g, s.ct)["status"] in ("resolved", "partially_resolved")


# --------------------------------------------------------------------------- cell 6


def test_cell6_acknowledged_leaves_the_contradiction_node_byte_identical(g):
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    _supersede(g, s.a, a2)
    before = _node(g, s.ct)
    assert before["status"] == "active"
    r = _resolve(g, s.ct, "acknowledged")
    assert "error" not in r, r
    assert _node(g, s.ct) == before
    assert [m["resolution_kind"] for m in _markers(g, s.ct)] == ["acknowledged"]
    assert not _edges(g, relation="resolves")


# --------------------------------------------------------------------------- cell 7


def test_cell7_acknowledged_writes_the_edge_to_the_live_end_of_the_chain(g):
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    a3 = _ob(g, "Alpha holds in many cases.")
    _supersede(g, s.a, a2)
    _supersede(g, a2, a3)
    r = _resolve(g, s.ct, "acknowledged")
    assert "error" not in r, r
    assert _edges(g, source=s.ct, relation="acknowledged_contradicts") == [
        {"source_id": s.ct, "target_id": a3, "relation": "acknowledged_contradicts"}]
    # the original contradicts edges stay as provenance
    assert sorted(e["target_id"] for e in _edges(g, source=s.ct, relation="contradicts")) == sorted(
        [s.a, s.b])
    assert r["acknowledgement_edges"] == [{"premise_id": s.a, "replacement_id": a3}]


def test_cell7_a_later_supersede_of_the_successor_marks_the_contradiction_again(g):
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    _supersede(g, s.a, a2)
    assert "error" not in _resolve(g, s.ct, "acknowledged")
    assert [m["status"] for m in _markers(g, s.ct)] == ["resolved"]
    a3 = _ob(g, "Alpha holds in many cases.")
    _supersede(g, a2, a3)
    pend = [m for m in _markers(g, s.ct) if m["status"] == "pending"]
    assert len(pend) == 1, _markers(g, s.ct)
    assert (pend[0]["root_node"], pend[0]["root_op"]) == (a2, "supersede")


def test_cell7_a_later_retract_of_the_successor_marks_it_as_a_retract_root(g):
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    _supersede(g, s.a, a2)
    assert "error" not in _resolve(g, s.ct, "acknowledged")
    _retract(g, a2)
    pend = [m for m in _markers(g, s.ct) if m["status"] == "pending"]
    assert [(m["root_node"], m["root_op"]) for m in pend] == [(a2, "retract")]


def test_cell7_the_new_relation_is_registered_like_acknowledged_derives_from(g):
    c = g.core
    assert "acknowledged_contradicts" in c.VALID_RELATIONS
    cls = c.EDGE_CLASSIFICATIONS["acknowledged_contradicts"]
    assert cls["temporal_causal"] is False
    assert cls["cascade"] is False          # contradictions are non-claim-bearing: never walked
    assert cls["addable_after_creation"] is False and cls["removable"] is False
    assert "acknowledged_contradicts" in c.DAG_EXEMPT_RELATIONS
    assert "acknowledged_contradicts" in c.EDGE_TIERS


def test_cell7_acknowledged_contradicts_makes_the_successor_contested(g):
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    _supersede(g, s.a, a2)
    assert "error" not in _resolve(g, s.ct, "acknowledged")
    c = g.core._get_db()
    try:
        assert s.ct in g.core._open_contradiction_ids(c, a2)
        assert g.rev._affected_contradiction_ids(c, a2) == [s.ct]
    finally:
        c.close()


def test_cell7_a_side_with_no_live_successor_writes_no_edge_and_says_so(g):
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    _supersede(g, s.a, a2)
    _retract(g, a2)                  # the chain a -> a2 now ends in a retraction, no replacement
    r = _resolve(g, s.ct, "acknowledged")
    assert "error" not in r, r
    assert not _edges(g, relation="acknowledged_contradicts")
    assert [u["side_id"] for u in r["acknowledgement_unlinked_sides"]] == [s.a]
    assert r["acknowledgement_edges"] == []



def test_cell7_two_superseded_sides_each_get_their_own_edge_to_their_own_live_successor(g):
    """Round 2 (reviewer-fairy): the edge is written for EVERY superseded side, not only the
    first pending event's. Mutant: loop over `affected[:1]` in
    `_cascade_record_contradiction_ack_edges`."""
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    b2 = _ob(g, "Alpha fails in many cases.")
    _supersede(g, s.a, a2)
    _supersede(g, s.b, b2)
    assert len([m for m in _markers(g, s.ct) if m["status"] == "pending"]) == 2
    r = _resolve(g, s.ct, "acknowledged")
    assert "error" not in r, r
    got = sorted((e["source_id"], e["target_id"]) for e in
                 _edges(g, source=s.ct, relation="acknowledged_contradicts"))
    assert got == sorted([(s.ct, a2), (s.ct, b2)])
    assert sorted((e["premise_id"], e["replacement_id"]) for e in r["acknowledgement_edges"]) == \
        sorted([(s.a, a2), (s.b, b2)])
    assert "acknowledgement_unlinked_sides" not in r

# --------------------------------------------------------------------------- cell 8


_V8_CASCADE_MARKERS_DDL = """
CREATE TABLE cascade_markers (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id                 TEXT NOT NULL REFERENCES nodes(id),
    cascade_event           TEXT NOT NULL,
    root_node               TEXT NOT NULL,
    root_op                 TEXT NOT NULL CHECK (root_op IN ('retract', 'supersede')),
    status                  TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'resolved')),
    resolution_kind         TEXT CHECK (resolution_kind IN ('superseded', 'acknowledged', 'retracted') OR resolution_kind IS NULL),
    resolution_replacement_id TEXT,
    created_at              TEXT NOT NULL,
    resolved_at             TEXT,
    backfill_provenance     TEXT CHECK (backfill_provenance IN ('recovered', 'migration_time') OR backfill_provenance IS NULL),
    backfill_anomaly        INTEGER NOT NULL DEFAULT 0,
    UNIQUE(node_id, cascade_event)
)
"""  # frozen: the table as shipped at schema version 8 (dev 3cabb39e)

_V8_INDEXES = (
    "CREATE INDEX idx_cascade_markers_node ON cascade_markers(node_id)",
    "CREATE INDEX idx_cascade_markers_node_status ON cascade_markers(node_id, status)",
    "CREATE INDEX idx_cascade_markers_event ON cascade_markers(cascade_event)",
)

_OLD_ROWS = [
    # id, node, event, root, root_op, status, kind, repl, created, resolved, provenance, anomaly
    (3, "n1", "cev_a", "r1", "retract", "pending", None, None, "2026-01-01T00:00:00Z", None, None, 0),
    (7, "n2", "cev_a", "r1", "retract", "resolved", "retracted", None, "2026-01-01T00:00:01Z",
     "2026-01-02T00:00:00Z", "recovered", 0),
    (9, "n3", "cev_b", "r2", "supersede", "resolved", "superseded", "n9", "2026-01-01T00:00:02Z",
     "2026-01-03T00:00:00Z", "migration_time", 1),
    (12, "n4", "cev_b", "r2", "supersede", "resolved", "acknowledged", None, "2026-01-01T00:00:03Z",
     "2026-01-04T00:00:00Z", None, 0),
]


def _raw(core):
    c = sqlite3.connect(str(core.DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def _v8_store(core, extra_column=False, version=8):
    """Take the store the real ladder built and turn it into a genuine version-8 store: the
    frozen three-kind table, populated, `PRAGMA user_version = <version>`. At version 8 the NUL-guard
    triggers (rung 9, #3765) are dropped too, so the ladder has both rungs to apply; at version 9
    (what origin/dev's code leaves) they stay, only the cascade table is three-kind."""
    c = _raw(core)
    for nid in ("n1", "n2", "n3", "n4"):
        c.execute(
            "INSERT INTO nodes (id, type, claim, created_at, is_current, status) "
            "VALUES (?, 'observation_factual', ?, '2026-01-01T00:00:00Z', 1, 'active')",
            (nid, f"claim of {nid}"),
        )
    if version < core.TEXT_NUL_GUARD_RUNG:
        for trig in core._TEXT_NUL_GUARD_TRIGGER_NAMES:
            c.execute(f"DROP TRIGGER IF EXISTS {trig}")
    c.execute("DROP TABLE cascade_markers")
    c.execute(_V8_CASCADE_MARKERS_DDL)
    for ddl in _V8_INDEXES:
        c.execute(ddl)
    if extra_column:
        c.execute("ALTER TABLE cascade_markers ADD COLUMN surprise TEXT")
    for row in _OLD_ROWS:
        c.execute(
            "INSERT INTO cascade_markers (id, node_id, cascade_event, root_node, root_op, status, "
            "resolution_kind, resolution_replacement_id, created_at, resolved_at, "
            "backfill_provenance, backfill_anomaly) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
    c.execute(f"PRAGMA user_version = {version}")
    c.commit()
    # the genuine old store really refuses the new kind
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO cascade_markers (node_id, cascade_event, root_node, root_op, "
                  "status, resolution_kind, created_at) VALUES ('n1','cev_x','r','retract',"
                  "'resolved','resolved','t')")
    c.rollback()
    c.close()


def _ladder(core):
    c = _raw(core)
    try:
        core._run_db_one_time_setup(c)
        c.commit()
        return c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()


def _marker_dump(core):
    c = _raw(core)
    try:
        return [tuple(r) for r in c.execute("SELECT * FROM cascade_markers ORDER BY id").fetchall()]
    finally:
        c.close()


def _table_sql(core):
    c = _raw(core)
    try:
        return c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='cascade_markers'"
                         ).fetchone()[0]
    finally:
        c.close()


@pytest.fixture
def core(monkeypatch):
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    mod = fresh_module(engram_core)
    d = tempfile.mkdtemp(prefix="engram_test_4075_mig_")
    mod._configure_paths(d)
    mod._ensure_data_dir()
    mod._get_db().close()
    try:
        yield mod
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_cell8_latest_schema_version_is_10_and_the_rung_constant_is_tied_to_the_ladder():
    assert engram_core.LATEST_SCHEMA_VERSION == 10
    assert engram_core.CASCADE_RESOLVED_KIND_RUNG == 10 > engram_core.TEXT_NUL_GUARD_RUNG == 9
    src = Path(engram_core.__file__).read_text(encoding="utf-8")
    block = src.split("# Migration version 10 (#4075", 1)[1].split("# #2732 (A)", 1)[0]
    assert "_rung_ready(conn, CASCADE_RESOLVED_KIND_RUNG," in block
    assert 'conn.execute("PRAGMA user_version = %d")' % engram_core.CASCADE_RESOLVED_KIND_RUNG in block


def test_cell8_rung_preserves_every_marker_row_and_reaches_version_10(core):
    _v8_store(core)
    before = _marker_dump(core)
    assert len(before) == len(_OLD_ROWS)
    assert _ladder(core) == 10
    assert _marker_dump(core) == before            # every column of every row, ids included


def test_cell8_the_migrated_table_accepts_the_new_kind_and_keeps_its_constraints(core):
    _v8_store(core)
    assert _ladder(core) == 10
    c = _raw(core)
    try:
        c.execute("INSERT INTO cascade_markers (node_id, cascade_event, root_node, root_op, "
                  "status, resolution_kind, resolution_replacement_id, created_at) VALUES "
                  "('n1','cev_new','r','retract','resolved','resolved','n2','t')")
        assert c.execute("SELECT MAX(id) FROM cascade_markers").fetchone()[0] == 13  # AUTOINCREMENT kept
        c.commit()
        for bad in (
            "INSERT INTO cascade_markers (node_id, cascade_event, root_node, root_op, status, "
            "resolution_kind, created_at) VALUES ('n1','cev_bad','r','retract','resolved','bogus','t')",
            "INSERT INTO cascade_markers (node_id, cascade_event, root_node, root_op, status, created_at) "
            "VALUES ('n1','cev_a','r','retract','pending','t')",           # UNIQUE(node_id, event)
            "INSERT INTO cascade_markers (node_id, cascade_event, root_node, root_op, status, created_at) "
            "VALUES ('n1','cev_c','r','explode','pending','t')",           # root_op CHECK
        ):
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(bad)
            c.rollback()
        idx = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='cascade_markers'")}
        assert {"idx_cascade_markers_node", "idx_cascade_markers_node_status",
                "idx_cascade_markers_event"} <= idx
    finally:
        c.close()


def test_cell8_running_the_migration_twice_is_a_no_op(core):
    _v8_store(core)
    assert _ladder(core) == 10
    sql_once, rows_once = _table_sql(core), _marker_dump(core)
    assert _ladder(core) == 10
    assert _table_sql(core) == sql_once
    assert _marker_dump(core) == rows_once


def _structural_sql(sql):
    """The table's DDL minus comments and whitespace, so a fresh store and a migrated one compare."""
    return " ".join(re.sub(r"--[^\n]*", "", sql).replace("IF NOT EXISTS ", "").split())


def test_cell8_a_migrated_store_and_a_fresh_store_carry_the_same_table(core):
    fresh_sql = _structural_sql(_table_sql(core))
    fresh_cols = [tuple(r) for r in _raw(core).execute("PRAGMA table_info(cascade_markers)")]
    assert "'superseded', 'acknowledged', 'retracted', 'resolved'" in fresh_sql
    _v8_store(core)
    _ladder(core)
    assert [tuple(r) for r in _raw(core).execute("PRAGMA table_info(cascade_markers)")] == fresh_cols
    assert _structural_sql(_table_sql(core)) == fresh_sql


def test_cell8_an_unknown_column_refuses_the_rung_and_loses_nothing(core, capsys):
    _v8_store(core, extra_column=True)
    before = _marker_dump(core)
    sql_before = _table_sql(core)
    capsys.readouterr()
    # rung 9 (NUL guard) lands; rung 10 refuses the unknown column and is not bumped: it retries
    assert _ladder(core) == core.TEXT_NUL_GUARD_RUNG
    assert "migration 10" in capsys.readouterr().err
    assert _marker_dump(core) == before
    assert _table_sql(core) == sql_before


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")
def test_cell8_a_failed_pre_migration_backup_blocks_rung_10(core, capsys):
    _v8_store(core)
    backup_dir = Path(core.DATA_DIR) / "db-backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(str(backup_dir), 0o500)
    try:
        capsys.readouterr()
        assert _ladder(core) == 8
        assert "pre-migration backup FAILED" in capsys.readouterr().err
    finally:
        os.chmod(str(backup_dir), 0o700)
    assert "'retracted', 'resolved'" not in _structural_sql(_table_sql(core))


def test_cell8_a_populated_v8_store_gets_a_pre_migration_backup_first(core):
    _v8_store(core)
    assert _ladder(core) == 10
    backups = sorted((Path(core.DATA_DIR) / "db-backup").glob("pre-migration-v8-*.db"))
    assert len(backups) == 1, [p.name for p in backups]
    b = sqlite3.connect(str(backups[0]))
    try:
        assert b.execute("PRAGMA user_version").fetchone()[0] == 8
    finally:
        b.close()


@pytest.mark.owner_schema_gate_real
def test_cell8_the_owner_schema_gate_refuses_a_store_still_at_8():
    import engramd_owner as eo
    d = Path(tempfile.mkdtemp(prefix="engram_test_4075_gate_"))
    try:
        db = d / "knowledge.db"
        c = sqlite3.connect(str(db))
        c.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
        c.execute("PRAGMA user_version = 8")
        c.commit()
        c.close()
        with pytest.raises(eo.OwnerStoreBehindSchema):
            eo._require_store_schema_current(db)
        c = sqlite3.connect(str(db))
        c.execute("PRAGMA user_version = 9")
        c.commit()
        c.close()
        with pytest.raises(eo.OwnerStoreBehindSchema):   # 9 is the NUL guard's rung, not ours
            eo._require_store_schema_current(db)
        c = sqlite3.connect(str(db))
        c.execute("PRAGMA user_version = 10")
        c.commit()
        c.close()
        eo._require_store_schema_current(db)        # at 10 it passes
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---- rung ordering against the NUL guard (#3765, rung 9): a store at 8 or 9 reaches 10 -------------

def _nul_triggers(core):
    c = _raw(core)
    try:
        return {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='nodes'")
            } & set(core._TEXT_NUL_GUARD_TRIGGER_NAMES)
    finally:
        c.close()


def test_rung_order_a_store_at_9_with_the_nul_guard_migrates_to_10_keeping_every_row(core):
    _v8_store(core, version=9)
    assert _nul_triggers(core) == set(core._TEXT_NUL_GUARD_TRIGGER_NAMES)   # the dev-shaped store
    assert "'retracted', 'resolved')" not in _structural_sql(_table_sql(core))
    before = _marker_dump(core)
    assert _ladder(core) == 10
    assert _marker_dump(core) == before
    assert "'retracted', 'resolved')" in _structural_sql(_table_sql(core))
    assert _nul_triggers(core) == set(core._TEXT_NUL_GUARD_TRIGGER_NAMES)   # untouched


def test_rung_order_a_store_at_8_gets_both_rungs_in_order(core):
    _v8_store(core, version=8)
    assert _nul_triggers(core) == set()                                     # neither rung has run
    before = _marker_dump(core)
    assert _ladder(core) == 10
    assert _nul_triggers(core) == set(core._TEXT_NUL_GUARD_TRIGGER_NAMES)   # rung 9 applied
    assert "'retracted', 'resolved')" in _structural_sql(_table_sql(core))  # rung 10 applied
    assert _marker_dump(core) == before


def test_rung_order_a_fresh_store_lands_at_10_with_the_guard_and_the_four_kind_check(core):
    assert _ladder(core) == 10
    assert _nul_triggers(core) == set(core._TEXT_NUL_GUARD_TRIGGER_NAMES)
    assert "'superseded', 'acknowledged', 'retracted', 'resolved'" in _structural_sql(_table_sql(core))


# ---- round 2: migration properties the code has and nothing used to pin ---------------------------


class _CopyShortProxy:
    """A connection wrapper whose rebuild copy loses all rows but the first: the injected
    "the copy count differs" fault. Everything else passes straight through."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *a):
        if sql.lstrip().startswith("INSERT INTO cascade_markers (") and "FROM cascade_markers_prev" in sql:
            sql += " LIMIT 1"
        return self._conn.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _bump10(conn):
    return lambda: conn.execute("PRAGMA user_version = 10")


def _tables_named_cascade_markers(c):
    return sorted(r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'cascade_markers%'"))


def _seq(c):
    r = c.execute("SELECT seq FROM sqlite_sequence WHERE name = 'cascade_markers'").fetchone()
    return None if r is None else r[0]


def test_round2_the_autoincrement_counter_is_carried_over_when_the_top_marker_was_deleted(core):
    """Counter (12) greater than max id (9): the rebuild must keep 12, so the next marker is 13 and
    an id that was ever handed out is never reused. Mutant: skip the sqlite_sequence carry-over."""
    _v8_store(core, version=9)
    c = _raw(core)
    c.execute("DELETE FROM cascade_markers WHERE id = 12")
    c.commit()
    assert c.execute("SELECT MAX(id) FROM cascade_markers").fetchone()[0] == 9
    assert _seq(c) == 12
    c.close()
    assert _ladder(core) == 10
    c = _raw(core)
    try:
        assert _seq(c) == 12
        c.execute("INSERT INTO cascade_markers (node_id, cascade_event, root_node, root_op, status, "
                  "created_at) VALUES ('n1','cev_next','r','retract','pending','t')")
        assert c.execute("SELECT MAX(id) FROM cascade_markers").fetchone()[0] == 13
    finally:
        c.close()


def test_round2_a_copy_whose_row_count_differs_rolls_back_and_the_next_start_succeeds(core):
    """Mutant: remove the `n_old != n_new` check."""
    _v8_store(core, version=9)
    before = _marker_dump(core)
    sql_before = _table_sql(core)
    conn = _raw(core)
    with pytest.raises(sqlite3.DatabaseError, match="copied 1 of 4 rows"):
        core._rebuild_cascade_markers_for_resolved_kind(_CopyShortProxy(conn), _bump10(conn))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    assert _tables_named_cascade_markers(conn) == ["cascade_markers"]
    conn.close()
    assert _marker_dump(core) == before
    assert _table_sql(core) == sql_before
    assert _ladder(core) == 10                      # the retry on the next start works
    assert _marker_dump(core) == before


def test_round2_foreign_keys_are_off_around_the_copy_so_an_orphan_marker_survives(core):
    """A marker whose node row was hand-deleted is carried over as it is, `foreign_key_check` reads
    the same before and after, and the pragma is restored. Mutant: leave foreign_keys on."""
    _v8_store(core, version=9)
    conn = _raw(core)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM nodes WHERE id = 'n4'")           # marker 12 now points at nothing
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    fk_before = [tuple(r) for r in conn.execute("PRAGMA foreign_key_check")]
    assert any(r[0] == "cascade_markers" for r in fk_before), fk_before
    before = _marker_dump(core)
    core._rebuild_cascade_markers_for_resolved_kind(conn, _bump10(conn))
    assert [tuple(r) for r in conn.execute("PRAGMA foreign_key_check")] == fk_before
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1        # restored
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
    conn.close()
    assert _marker_dump(core) == before


def test_round2_a_fault_mid_rebuild_rolls_back_everything_and_the_next_start_succeeds(core):
    """A bad FINAL statement (the engine refuses the last index) after the table was renamed, the
    new one created and the rows copied: version stays at its old value, one cascade_markers table
    and no aside table remain, rows and indexes intact. Mutant: commit before the final statements
    (not one transaction)."""
    _v8_store(core, version=9)
    before = _marker_dump(core)
    sql_before = _table_sql(core)
    conn = _raw(core)
    idx_before = sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='cascade_markers'"))

    def deny_last_index(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_CREATE_INDEX and arg1 == "idx_cascade_markers_event":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(deny_last_index)
    with pytest.raises(sqlite3.DatabaseError):
        core._rebuild_cascade_markers_for_resolved_kind(conn, _bump10(conn))
    conn.set_authorizer(None)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    assert _tables_named_cascade_markers(conn) == ["cascade_markers"]
    assert sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='cascade_markers'")) == idx_before
    conn.close()
    assert _marker_dump(core) == before
    assert _table_sql(core) == sql_before
    assert _ladder(core) == 10                                              # next start succeeds
    assert _marker_dump(core) == before
    assert "'retracted', 'resolved')" in _structural_sql(_table_sql(core))


# ---- round 2: the two readers that were follow-ups ------------------------------------------------


def _obsolescence_candidates(g):
    import importlib
    qry = importlib.import_module("engram_query")
    conn = g.core._get_db()
    try:
        return qry._pattern_contradiction_obsolescence_ready(
            conn, dict(qry.PATTERN_QUERY_PRESETS["balanced"]))
    finally:
        conn.close()


def test_readers_a_contradiction_acknowledged_against_a_live_successor_is_not_ripe_for_resolution(g):
    """`engram_query.py` obsolescence scan: before the acknowledgement the superseded side makes
    the contradiction a candidate; after it, the live successor stands in for that side and the
    contradiction is NOT reported as "one side obsolete". Mutant: read `contradicts` edges only."""
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    _supersede(g, s.a, a2)
    assert [c["id"] for c in _obsolescence_candidates(g)] == [s.ct]      # control: ripe before
    assert "error" not in _resolve(g, s.ct, "acknowledged")
    assert _obsolescence_candidates(g) == []


def test_readers_the_successor_is_followed_along_the_whole_chain_and_a_dead_end_is_ripe_again(g):
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    a3 = _ob(g, "Alpha holds in many cases.")
    _supersede(g, s.a, a2)
    _supersede(g, a2, a3)
    assert "error" not in _resolve(g, s.ct, "acknowledged")              # edge goes to a3
    assert _obsolescence_candidates(g) == []
    a4 = _ob(g, "Alpha holds in few cases.")
    _supersede(g, a3, a4)                                                # the successor moved on
    assert [c["id"] for c in _obsolescence_candidates(g)] == [s.ct]      # acknowledged edge is stale


def test_readers_a_second_obsolete_side_still_makes_it_ripe(g):
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    _supersede(g, s.a, a2)
    assert "error" not in _resolve(g, s.ct, "acknowledged")
    _retract(g, s.b)                                                     # the other side is obsolete
    cands = _obsolescence_candidates(g)
    assert [c["id"] for c in cands] == [s.ct] and cands[0]["score"] == 1.0


def test_readers_the_compact_snapshot_shows_the_successor_as_a_conflict(g):
    s = _scenario(g)
    a2 = _ob(g, "Alpha holds in most cases.")
    _supersede(g, s.a, a2)
    assert "error" not in _resolve(g, s.ct, "acknowledged")
    c = g.core._get_db()
    try:
        snap = g.core._generate_snapshot(c)
    finally:
        c.close()
    block = snap.split(f"### {s.ct} ", 1)[1].split("###", 1)[0]
    assert sorted(l for l in block.splitlines() if l.startswith("Conflicts: ")) == sorted(
        f"Conflicts: {n}" for n in (s.a, s.b, a2))
