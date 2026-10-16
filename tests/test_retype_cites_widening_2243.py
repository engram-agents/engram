"""#2243 step 3 part A -- widen engram_retype_edge: cites -> every addable relation.

Contract cells, written BEFORE the implementation (contract-first). The
contract (C1-C4 of the dispatch spec):

  C1  RETYPE_ALLOWED_MOVES holds ("cites", X) for exactly the relations X with
      EDGE_CLASSIFICATIONS[X]["addable_after_creation"], derived (never a
      hand-typed list). Value None, except ("cites","exemplifies"), which
      shares the target types of ("derives_from","exemplifies").
  C2  ONE guarded writer for the relation-specific endpoint rules
      (`instantiates` target/source rules, `generalizes` pair rule):
      engram_cornerstone._relation_endpoint_error. add_edge's responses stay
      byte-identical; a retype to X is refused with X's own error text
      whenever add_edge to X would be refused for the same node types. (Bound:
      this holds for the node-type endpoint rules; a retype to `subtask_of`
      ALSO runs the grounding-family cycle check, which add_edge does not, so
      a retype can be refused where add_edge would not be.)
  C3  Current-ness is NOT required by retype (frozen history is retyped with
      its stratum): a loose cites whose SOURCE is superseded still retypes.
  C4  Post-commit side effects, best-effort: exemplifies -> incidents cache +
      cornerstone-anchors rebuild (+ `cache_rebuild` in the result);
      exemplifies/instantiates/serves/tensions -> principle-trigger rebuild.
      NEVER the principle-enactment reset (a relabel is not new evidence).

  T1 table  T2 one cell per new move  T3 refusals  T4 add_edge equivalence
  T5 refused moves  T6 superseded source  T7 side effects  T8 add_edge
  unchanged (byte-identity pin).

Nodes are inserted raw (fixed type / created_at) so every (type, type) pair
is reachable without driving each node-type's creation tool.
"""

import atexit
import hashlib
import itertools
import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_retype_cites_2243_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

OLD = "2026-01-01T00:00:00+00:00"
NEW = "2026-02-01T00:00:00+00:00"

NODE_TYPES = ["observation_factual", "lesson", "goal", "cornerstone",
              "axiom", "task", "definition"]


@pytest.fixture(scope="module")
def srv():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


_counter = itertools.count(1)


def _node(core, typ, created_at, nid=None, current=1):
    nid = nid or f"zz_{next(_counter):06d}"
    conn = core._get_db()
    try:
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, is_current, confidence) "
            "VALUES (?, ?, ?, ?, ?, 0.5)",
            (nid, typ, f"claim {nid}", created_at, current),
        )
        conn.commit()
    finally:
        conn.close()
    return nid


def _edge(core, rel, s, t):
    conn = core._get_db()
    try:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?, ?, ?, ?)", (s, t, rel, core._now()))
        conn.commit()
    finally:
        conn.close()


def _relations(core, s, t):
    conn = core._get_db()
    try:
        return sorted(r["relation"] for r in conn.execute(
            "SELECT relation FROM edges WHERE source_id=? AND target_id=?", (s, t)))
    finally:
        conn.close()


def _retype(srv, s, t, old, new, reason="step 3 test"):
    return json.loads(srv.engram_retype_edge(payload_json=json.dumps({
        "source_id": s, "target_id": t, "relation": old,
        "new_relation": new, "reason": reason})))


def _add(srv, s, t, rel):
    return json.loads(srv.engram_add_edge(payload_json=json.dumps({
        "source_id": s, "target_id": t, "relation": rel})))


def _addable(core):
    return {r for r, c in core.EDGE_CLASSIFICATIONS.items()
            if c["addable_after_creation"]}


# A VALID (source type, target type) pair per addable relation. Source is
# newer than target so the temporal rule (subtask_of) holds.
VALID_PAIR = {
    "about": ("observation_factual", "observation_factual"),
    "exemplifies": ("observation_factual", "lesson"),
    "generalizes": ("lesson", "lesson"),
    "instantiates": ("observation_factual", "goal"),
    "serves": ("task", "goal"),
    "subtask_of": ("task", "task"),
    "tensions": ("goal", "goal"),
    # #3615: question -> claim-bearing. Extends the three parametrized families
    # below (accepted retype, add_edge-equivalence, parity) to the new relation.
    "bears_on": ("question", "conjecture"),
}


def _loose_cites(core, src_type, tgt_type, src_at=NEW, tgt_at=OLD):
    t = _node(core, tgt_type, tgt_at)
    s = _node(core, src_type, src_at)
    _edge(core, "cites", s, t)
    return s, t


# ---------------------------------------------------------------------------
# T1 -- table
# ---------------------------------------------------------------------------

def test_t1_table_is_derived_from_classification(srv):
    core = srv.core
    moves = core.RETYPE_ALLOWED_MOVES
    assert {b for (a, b) in moves if a == "cites"} == _addable(core)
    assert core.EXEMPLIFIES_TARGET_TYPES == ("lesson", "cornerstone")
    assert moves[("cites", "exemplifies")] == core.EXEMPLIFIES_TARGET_TYPES
    assert moves[("derives_from", "exemplifies")] == core.EXEMPLIFIES_TARGET_TYPES
    for k, v in moves.items():
        if k not in {("cites", "exemplifies"), ("derives_from", "exemplifies")}:
            assert v is None, k
    # the four pre-existing moves keep their meaning
    assert moves[("supported_by", "about")] is None
    assert moves[("derives_from", "about")] is None
    assert moves[("cites", "about")] is None
    # nothing else crept in: non-cites sources are exactly the old three
    assert {k for k in moves if k[0] != "cites"} == {
        ("supported_by", "about"), ("derives_from", "about"),
        ("derives_from", "exemplifies")}


# ---------------------------------------------------------------------------
# T2 -- one cell per new move
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel", sorted(VALID_PAIR))
def test_t2_valid_pair_retypes(srv, rel, monkeypatch):
    core = srv.core
    monkeypatch.setattr(core, "_rebuild_incidents_cache", lambda: {"status": "ok"})
    monkeypatch.setattr(core, "_rebuild_cornerstone_anchors_cache", lambda: {})
    monkeypatch.setattr(core, "_rebuild_principle_triggers", lambda: {})
    st, tt = VALID_PAIR[rel]
    s, t = _loose_cites(core, st, tt)
    r = _retype(srv, s, t, "cites", rel, reason=f"reason for {rel}")
    assert r.get("status") == "retyped", r
    assert _relations(core, s, t) == [rel]
    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT details FROM edit_history WHERE action='edge_retyped' "
            "AND node_id=? ORDER BY id DESC LIMIT 1", (s,)).fetchone()
    finally:
        conn.close()
    d = json.loads(row["details"])
    assert (d["old_relation"], d["new_relation"], d["reason"]) == (
        "cites", rel, f"reason for {rel}")
    ins = srv.engram_inspect(payload_json=json.dumps(
        {"node_id": s, "view": "edges"}))
    assert f'"{rel}"' in ins and '"cites"' not in ins, ins


# ---------------------------------------------------------------------------
# T3 -- refusals carry the relation's own text; edge untouched
# ---------------------------------------------------------------------------

def _refused_untouched(srv, s, t, rel, needle):
    core = srv.core
    r = _retype(srv, s, t, "cites", rel)
    assert "error" in r and needle in r["error"], r
    assert _relations(core, s, t) == ["cites"]
    return r


def test_t3_instantiates_to_lesson(srv):
    s, t = _loose_cites(srv.core, "observation_factual", "lesson")
    _refused_untouched(srv, s, t, "instantiates", "does not accept lesson targets")


def test_t3_instantiates_bad_target_type(srv):
    s, t = _loose_cites(srv.core, "observation_factual", "task")
    _refused_untouched(srv, s, t, "instantiates", "targets must be one of")


def test_t3_instantiates_non_claim_bearing_source(srv):
    s, t = _loose_cites(srv.core, "task", "goal")
    _refused_untouched(srv, s, t, "instantiates", "sources must be claim-bearing")


def test_t3_generalizes_bad_pair(srv):
    s, t = _loose_cites(srv.core, "observation_factual", "observation_factual")
    _refused_untouched(srv, s, t, "generalizes", "does not accept a")


def test_t3_generalizes_cycle(srv):
    core = srv.core
    s, t = _loose_cites(core, "lesson", "lesson")
    _edge(core, "generalizes", t, s)          # t generalizes s; s -> t closes a loop
    _refused_untouched(srv, s, t, "generalizes", "cycle")


def test_t3_subtask_of_source_older(srv):
    s, t = _loose_cites(srv.core, "task", "task", src_at=OLD, tgt_at=NEW)
    _refused_untouched(srv, s, t, "subtask_of", "temporal_causal")


def test_t3_exemplifies_bad_target(srv):
    s, t = _loose_cites(srv.core, "observation_factual", "observation_factual")
    _refused_untouched(srv, s, t, "exemplifies", "requires the target to be")


# ---------------------------------------------------------------------------
# T4 -- EQUIVALENCE with add_edge, generated from a type-pair matrix
# ---------------------------------------------------------------------------

def _norm(text, s, t, S, T):
    return text.replace(s, "<S>").replace(t, "<T>")


def _equivalence_cells():
    # computed at collection time from the live classification
    from engram_core import EDGE_CLASSIFICATIONS
    rels = sorted(r for r, c in EDGE_CLASSIFICATIONS.items()
                  if c["addable_after_creation"])
    return [(r, st, tt) for r in rels
            for st, tt in itertools.product(NODE_TYPES, NODE_TYPES)]


@pytest.mark.parametrize("rel,st,tt", _equivalence_cells())
def test_t4_retype_refused_iff_add_refused(srv, monkeypatch, rel, st, tt):
    core = srv.core
    for name in ("_rebuild_incidents_cache", "_rebuild_cornerstone_anchors_cache",
                 "_rebuild_principle_triggers", "_reset_principle_enactments"):
        monkeypatch.setattr(core, name, lambda *a, **k: {})
    # pair A: add_edge, both endpoints current. pair B: loose cites, retype.
    ta = _node(core, tt, OLD)
    sa = _node(core, st, NEW)
    added = _add(srv, sa, ta, rel)
    sb, tb = _loose_cites(core, st, tt)
    retyped = _retype(srv, sb, tb, "cites", rel)
    add_refused = "error" in added
    retype_refused = "error" in retyped
    if rel == "exemplifies":
        # add_edge has no endpoint gate for exemplifies; the retype's extra,
        # spec-mandated (C1) condition is exactly the target-type tuple.
        expect_refused = tt not in core.EXEMPLIFIES_TARGET_TYPES
        assert not add_refused, added
        assert retype_refused == expect_refused, (retyped, rel, st, tt)
        return
    assert retype_refused == add_refused, (added, retyped)
    if add_refused:
        assert _norm(retyped["error"], sb, tb, "S", "T") == \
            _norm(added["error"], sa, ta, "S", "T")
        assert _relations(core, sb, tb) == ["cites"]
    else:
        assert _relations(core, sb, tb) == [rel]


def test_t4b_add_edge_endpoint_error_precedes_already_exists(srv):
    """Order of add_edge's refusals: the endpoint-rule error wins over the
    already-exists no-op. The pair (observation_factual -> lesson) violates
    the `instantiates` rule; an edge of another relation already links it,
    and a duplicate `instantiates` request must still get the endpoint error
    (never the idempotent no-op response)."""
    core = srv.core
    t = _node(core, "lesson", OLD)
    s = _node(core, "observation_factual", NEW)
    _edge(core, "about", s, t)
    # Make an `instantiates` row exist too, so the already-exists branch is
    # genuinely reachable for this pair (raw insert bypasses the gate).
    _edge(core, "instantiates", s, t)
    res = _add(srv, s, t, "instantiates")
    assert "error" in res, res
    assert "does not accept lesson targets" in res["error"], res
    assert res.get("status") != "no_op_already_exists", res


# ---------------------------------------------------------------------------
# T5 -- refused moves
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("new", [
    "derives_from", "supported_by", "acknowledged_derives_from", "predicted_by",
    "resolves", "retracts", "supersedes", "contradicts", "cites"])
def test_t5_non_addable_targets_refused(srv, new):
    core = srv.core
    s, t = _loose_cites(core, "observation_factual", "observation_factual")
    r = _retype(srv, s, t, "cites", new)
    assert "error" in r and "is not an allowed re-type move" in r["error"], r
    assert _relations(core, s, t) == ["cites"]


# ---------------------------------------------------------------------------
# T6 -- superseded / retracted source (C3)
# ---------------------------------------------------------------------------

def test_t6_superseded_source_retypes_to_about(srv):
    core = srv.core
    t = _node(core, "observation_factual", OLD)
    s = _node(core, "observation_factual", NEW, current=0)
    _edge(core, "cites", s, t)
    r = _retype(srv, s, t, "cites", "about")
    assert r.get("status") == "retyped", r
    assert _relations(core, s, t) == ["about"]


def test_t6_superseded_source_still_meets_gated_guards(srv):
    core = srv.core
    # task (non-claim-bearing) -> goal: refused by instantiates' source rule
    t = _node(core, "goal", OLD)
    s = _node(core, "task", NEW, current=0)
    _edge(core, "cites", s, t)
    r = _retype(srv, s, t, "cites", "instantiates")
    assert "error" in r and "sources must be claim-bearing" in r["error"], r
    assert _relations(core, s, t) == ["cites"]
    # claim-bearing superseded source, valid target: goes through
    t2 = _node(core, "goal", OLD)
    s2 = _node(core, "observation_factual", NEW, current=0)
    _edge(core, "cites", s2, t2)
    r2 = _retype(srv, s2, t2, "cites", "instantiates")
    assert r2.get("status") == "retyped", r2
    # lesson-lesson generalizes from a superseded source is admitted
    t3 = _node(core, "lesson", OLD)
    s3 = _node(core, "lesson", NEW, current=0)
    _edge(core, "cites", s3, t3)
    assert _retype(srv, s3, t3, "cites", "generalizes").get("status") == "retyped"


# ---------------------------------------------------------------------------
# T7 -- post-commit side effects (C4)
# ---------------------------------------------------------------------------

class _Spies:
    def __init__(self, core, monkeypatch):
        self.calls = []
        for name in ("_rebuild_incidents_cache", "_rebuild_cornerstone_anchors_cache",
                     "_rebuild_principle_triggers", "_reset_principle_enactments"):
            def make(n):
                def f(*a, **k):
                    self.calls.append(n)
                    return {"status": "ok", "spy": n}
                return f
            monkeypatch.setattr(core, name, make(name))

    def count(self, n):
        return self.calls.count(n)


@pytest.mark.parametrize("rel", sorted(VALID_PAIR))
def test_t7_side_effects_by_relation(srv, monkeypatch, rel):
    core = srv.core
    spies = _Spies(core, monkeypatch)
    s, t = _loose_cites(core, *VALID_PAIR[rel])
    r = _retype(srv, s, t, "cites", rel)
    assert r.get("status") == "retyped", r
    exp_inc = 1 if rel == "exemplifies" else 0
    assert spies.count("_rebuild_incidents_cache") == exp_inc
    assert spies.count("_rebuild_cornerstone_anchors_cache") == exp_inc
    exp_trig = 1 if rel in ("exemplifies", "instantiates", "serves", "tensions") else 0
    assert spies.count("_rebuild_principle_triggers") == exp_trig
    assert spies.count("_reset_principle_enactments") == 0
    assert ("cache_rebuild" in r) == (rel == "exemplifies"), r


def test_t7_derives_from_exemplifies_also_rebuilds(srv, monkeypatch):
    core = srv.core
    spies = _Spies(core, monkeypatch)
    t = _node(core, "lesson", OLD)
    s = _node(core, "observation_factual", NEW)
    p = _node(core, "observation_factual", OLD)
    _edge(core, "derives_from", s, t)
    _edge(core, "derives_from", s, p)      # keep a second premise
    r = _retype(srv, s, t, "derives_from", "exemplifies")
    assert r.get("status") == "retyped", r
    assert spies.count("_rebuild_incidents_cache") == 1
    assert spies.count("_rebuild_principle_triggers") == 1
    assert spies.count("_reset_principle_enactments") == 0
    assert "cache_rebuild" in r


@pytest.mark.parametrize("failing", [
    "_rebuild_incidents_cache", "_rebuild_cornerstone_anchors_cache",
    "_rebuild_principle_triggers"])
def test_t7_raising_rebuild_never_fails_the_retype(srv, monkeypatch, failing):
    core = srv.core
    for n in ("_rebuild_incidents_cache", "_rebuild_cornerstone_anchors_cache",
              "_rebuild_principle_triggers", "_reset_principle_enactments"):
        monkeypatch.setattr(core, n, lambda *a, **k: {})

    def boom(*a, **k):
        raise RuntimeError("rebuild exploded")
    monkeypatch.setattr(core, failing, boom)
    s, t = _loose_cites(core, *VALID_PAIR["exemplifies"])
    r = _retype(srv, s, t, "cites", "exemplifies")
    assert r.get("status") == "retyped", r
    assert _relations(core, s, t) == ["exemplifies"]


# ---------------------------------------------------------------------------
# T8 -- add_edge unchanged. The corpus below was captured from origin/dev
# BEFORE the refactor; the sha256 pins every response, the literals pin the
# gated texts so a failure is readable.
# ---------------------------------------------------------------------------

def _corpus(srv, monkeypatch):
    core = srv.core
    for n in ("_rebuild_incidents_cache", "_rebuild_cornerstone_anchors_cache",
              "_rebuild_principle_triggers", "_reset_principle_enactments"):
        monkeypatch.setattr(core, n, lambda *a, **k: {})
    out = []
    n = 0
    for rel in sorted(_addable(core)):
        for st, tt in itertools.product(NODE_TYPES, NODE_TYPES):
            n += 1
            tid = _node(core, tt, OLD, nid=f"cT{n:05d}")
            sid = _node(core, st, NEW, nid=f"cS{n:05d}")
            r = _add(srv, sid, tid, rel)
            r.pop("created_at", None)
            out.append((f"{rel}|{st}|{tt}", json.dumps(r, sort_keys=True)))
    # idempotent no-op, non-current endpoints, temporal violation, cycle
    a = _node(core, "lesson", OLD, nid="cX00001")
    b = _node(core, "lesson", NEW, nid="cX00002")
    d = _node(core, "lesson", NEW, nid="cX00003", current=0)
    for label, args in [
            ("first", (b, a, "generalizes")),
            ("again", (b, a, "generalizes")),
            ("cycle", (a, b, "generalizes")),
            ("src_dead", (d, a, "about")),
            ("tgt_dead", (a, d, "about")),
            ("temporal", (a, b, "subtask_of")),
            ("missing", ("nope", a, "about")),
            ("bad_rel", (a, b, "derives_from"))]:
        r = _add(srv, *args)
        r.pop("created_at", None)
        out.append((f"x|{label}", json.dumps(r, sort_keys=True)))
    return out


# Re-pinned for #3615 (`bears_on` joins the addable set). The corpus grew by 49
# `bears_on|<src>|<tgt>` rows; the node-id counter shifts every later relation's
# ids by 49; and the `x|bad_rel` "Allowed: [...]" list now names `bears_on`.
# Checked against the previous pin: every other row is byte-identical once the
# id shift is normalised, so no pre-existing relation's response changed.
_CORPUS_SHA256 = "73ed3a90bd5d69849a0a247304d4515ac49ec107b7fc16be835e5b2208f88bee"


def test_t8_add_edge_responses_byte_identical(srv, monkeypatch):
    corpus = _corpus(srv, monkeypatch)
    by_key = dict(corpus)
    lit = json.loads(by_key["instantiates|observation_factual|lesson"])
    assert lit["error"].startswith("'instantiates' does not accept lesson targets")
    lit = json.loads(by_key["instantiates|observation_factual|task"])
    assert lit["error"].startswith("'instantiates' targets must be one of")
    lit = json.loads(by_key["instantiates|task|goal"])
    assert lit["error"].startswith("'instantiates' sources must be claim-bearing")
    lit = json.loads(by_key["generalizes|observation_factual|goal"])
    assert lit["error"].startswith("'generalizes' does not accept a observation_factual -> goal pair.")
    blob = "\n".join(f"{k}\t{v}" for k, v in corpus)
    assert hashlib.sha256(blob.encode()).hexdigest() == _CORPUS_SHA256


# ---------------------------------------------------------------------------
# T9-T12 -- an observation -> evidence `cites` is provenance: NOT retypable.
#
# Contract (colleague-found defect on the widening): a `cites` edge whose pair
# `_insert_cites_edge` accepts (observation source, `ev_` evidence target) is
# refused by engram_retype_edge for EVERY new relation; the edge row and the
# edit log are untouched. A loose `cites` (any pair `_insert_cites_edge` would
# refuse) retypes exactly as before. The refusal comes before every other
# check that could name a different error. `_insert_cites_edge` itself is
# unchanged (accept/refuse behaviour and error text pinned in T12).
# ---------------------------------------------------------------------------

GUARD_TEXT = "is provenance and is not retypable"


def _observation_types(core):
    """Every node type the shared predicate treats as an observation: derived
    from core's own type set via the same prefix rule, never a literal list."""
    return sorted(t for t in core.VALID_NODE_TYPES if t.startswith("observation_"))


def _legit_cites(core, obs_type="observation_factual"):
    ev = _node(core, "evidence", OLD, nid=f"ev_{next(_counter):06d}")
    s = _node(core, obs_type, NEW)
    _edge(core, "cites", s, ev)
    return s, ev


def _edit_log_count(core):
    conn = core._get_db()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM edit_history").fetchone()["c"]
    finally:
        conn.close()


def _assert_guard_refusal(core, r, s, t, before):
    assert "error" in r and GUARD_TEXT in r["error"], r
    assert s in r["error"] and t in r["error"], r
    assert r.get("status") != "retyped", r
    assert _relations(core, s, t) == ["cites"]
    assert _edit_log_count(core) == before


def _t9_cells():
    from engram_core import EDGE_CLASSIFICATIONS
    return sorted(r for r, c in EDGE_CLASSIFICATIONS.items()
                  if c["addable_after_creation"])


def _quiet(core, monkeypatch):
    for n in ("_rebuild_incidents_cache", "_rebuild_cornerstone_anchors_cache",
              "_rebuild_principle_triggers", "_reset_principle_enactments"):
        monkeypatch.setattr(core, n, lambda *a, **k: {})


@pytest.mark.parametrize("rel", _t9_cells())
def test_t9_observation_to_evidence_cites_not_retypable(srv, monkeypatch, rel):
    core = srv.core
    _quiet(core, monkeypatch)
    s, t = _legit_cites(core)
    before = _edit_log_count(core)
    r = _retype(srv, s, t, "cites", rel)
    _assert_guard_refusal(core, r, s, t, before)


def test_t9_every_observation_subtype(srv):
    core = srv.core
    types = _observation_types(core)
    assert "observation_factual" in types
    for typ in types:
        s, t = _legit_cites(core, typ)
        before = _edit_log_count(core)
        r = _retype(srv, s, t, "cites", "about")
        _assert_guard_refusal(core, r, s, t, before)


def test_t9_refusal_runs_no_side_effect(srv, monkeypatch):
    core = srv.core
    spies = _Spies(core, monkeypatch)
    for rel in _t9_cells():
        s, t = _legit_cites(core)
        _retype(srv, s, t, "cites", rel)
        assert _relations(core, s, t) == ["cites"], rel
    assert spies.calls == []


def test_t10_loose_cites_still_retypes(srv, monkeypatch):
    core = srv.core
    _quiet(core, monkeypatch)
    loose = []
    # non-observation source onto an ev_ target
    for src_type in ("lesson", "definition"):
        ev = _node(core, "evidence", OLD, nid=f"ev_{next(_counter):06d}")
        s = _node(core, src_type, NEW)
        _edge(core, "cites", s, ev)
        loose.append((s, ev))
    # observation source onto a non-evidence target
    loose.append(_loose_cites(core, "observation_factual", "lesson"))
    loose.append(_loose_cites(core, "task", "goal"))
    for s, t in loose:
        r = _retype(srv, s, t, "cites", "about")
        assert r.get("status") == "retyped", (s, r)
        assert _relations(core, s, t) == ["about"]


@pytest.mark.parametrize("rel", sorted(VALID_PAIR))
def test_t10_loose_cites_to_every_relation_unchanged(srv, monkeypatch, rel):
    core = srv.core
    _quiet(core, monkeypatch)
    st, tt = VALID_PAIR[rel]
    s, t = _loose_cites(core, st, tt)
    assert _retype(srv, s, t, "cites", rel).get("status") == "retyped"
    assert _relations(core, s, t) == [rel]


def test_t10_other_moves_unaffected(srv):
    core = srv.core
    t = _node(core, "observation_factual", OLD)
    s = _node(core, "observation_factual", NEW)
    _edge(core, "supported_by", s, t)
    assert _retype(srv, s, t, "supported_by", "about").get("status") == "retyped"


def test_t11_guard_text_wins_over_pending_marker(srv, monkeypatch):
    core = srv.core
    s, t = _legit_cites(core)
    monkeypatch.setattr(core, "_pending_markers_for_node", lambda c, n: [{"x": 1}])
    before = _edit_log_count(core)
    r = _retype(srv, s, t, "cites", "about")
    _assert_guard_refusal(core, r, s, t, before)
    assert "pending cascade marker" not in r["error"]


@pytest.mark.parametrize("rel", ["instantiates", "generalizes", "exemplifies",
                                 "subtask_of", "tensions", "serves"])
def test_t11_guard_text_wins_over_endpoint_and_target_rules(srv, rel):
    """The legitimate pair also violates these relations' own rules (target
    type, endpoint pair, temporal); the guard's text is still the one shown."""
    core = srv.core
    s, t = _legit_cites(core)
    before = _edit_log_count(core)
    r = _retype(srv, s, t, "cites", rel)
    _assert_guard_refusal(core, r, s, t, before)


def test_t11_missing_edge_error_still_wins_over_guard(srv):
    core = srv.core
    ev = _node(core, "evidence", OLD, nid=f"ev_{next(_counter):06d}")
    s = _node(core, "observation_factual", NEW)
    r = _retype(srv, s, ev, "cites", "about")
    assert "nothing to retype" in r.get("error", ""), r
    assert GUARD_TEXT not in r["error"]


def test_t12_insert_cites_edge_unchanged(srv):
    core = srv.core
    ev = _node(core, "evidence", OLD, nid=f"ev_{next(_counter):06d}")
    plain = _node(core, "lesson", OLD)
    lesson = _node(core, "lesson", NEW)
    cases = [
        ("obs_ev", _node(core, "observation_factual", NEW), ev),
        ("pred_ev", _node(core, "observation_predictive", NEW), ev),
        ("obs_plain", _node(core, "observation_factual", NEW), plain),
        ("lesson_ev", lesson, ev),
        ("missing_src", "zz_missing", ev)]
    conn = core._get_db()
    try:
        results = {}
        for label, s, t in cases:
            try:
                core._insert_cites_edge(conn, s, t, "2026-10-06T00:00:00Z")
                results[label] = ("ok", None)
            except ValueError as e:
                results[label] = ("refused", str(e).replace(s, "<S>").replace(t, "<T>"))
        conn.rollback()
    finally:
        conn.close()
    assert results == {
        "obs_ev": ("ok", None),
        "pred_ev": ("ok", None),
        "obs_plain": ("refused",
                      "cites targets evidence nodes only; use 'about' for a "
                      "context reference: <T>"),
        "lesson_ev": ("refused",
                      "cites carries observation->evidence only; source is "
                      "not an observation node: <S>"),
        "missing_src": ("refused",
                        "cites carries observation->evidence only; source is "
                        "not an observation node: <S>"),
    }
