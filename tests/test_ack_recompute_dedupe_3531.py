"""#3531 -- a recompute after an acknowledgement must not count a superseded
premise T alongside its replacement S.

CONTRACT (premise set of ``_recompute_node_confidence`` for node N):
  every ``derives_from`` and ``acknowledged_derives_from`` target of N, EXCEPT a
  ``derives_from`` target T for which N also has an ``acknowledged_derives_from``
  edge to a node S that supersedes T, directly or through a supersede chain.
  Everything else (pending nodes, unrelated non-current premises) unchanged.

  1. corroboration class: N = discount x S only (T not counted).
  2. min class: S credited (S above T), T dropped.
  3. min class, S weaker than T: N uses S (not "max").
  4. pending (no ack edge): T still counted -- frontier untouched.
  5. supersede chain T <- T2 <- S, ack edge to S: T dropped.
  6. non-current premise with NO acknowledged successor from N: still counted.
  7. multi-premise: only the acknowledged-superseded one is dropped.
  8. descendants of N are still recomputed from N's new value.
  S1. sweep dry-run writes nothing; S2. --apply changes exactly the predicted
  nodes; S3. second apply is a no-op.
"""

import atexit
import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402

import engram_core  # noqa: E402,F401
import engram_derive  # noqa: E402,F401
import engram_observation  # noqa: E402,F401
import engram_revision  # noqa: E402

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_3531_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

REPO_ROOT = Path(__file__).resolve().parent.parent
SWEEP_PATH = REPO_ROOT / "src" / "engram" / "tools" / "ack_recompute_sweep.py"

HARD, OFFICIAL, ANALYSIS, EDITORIAL = (
    "hard_data", "official_statement", "attributed_analysis", "editorial")
C = {HARD: 0.95, OFFICIAL: 0.85, ANALYSIS: 0.70, EDITORIAL: 0.35}


def _fresh():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    rev = fresh_module(engram_revision)
    obs = importlib.import_module("engram_observation")
    derive = importlib.import_module("engram_derive")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return rev, obs, derive, core


_SEQ = [0]


def _ob(obs, qt):
    _SEQ[0] += 1
    r = json.loads(obs._add_observation_impl(
        url=f"https://example.com/s3531-{_SEQ[0]}", title=f"S{_SEQ[0]}",
        quoted_text=f"Claim {_SEQ[0]}. in the cited source", interpretation="i",
        claim=f"Claim {_SEQ[0]}.", quote_type=qt))
    assert "error" not in r, r
    return r["observation_id"]


def _dv(derive, premises, rtype):
    _SEQ[0] += 1
    r = json.loads(derive._derive_impl(
        claim=f"Derived from {','.join(premises)} ({_SEQ[0]}, {rtype}).",
        supporting_ids=",".join(premises), logical_chain="chain",
        reasoning_type=rtype))
    assert "error" not in r, r
    return r["derivation_id"]


def _supersede(rev, old, new):
    r = json.loads(rev._supersede_impl(
        old_node_id=old, new_node_id=new, supersede_reason="refined"))
    assert "error" not in r, r


def _ack_edge(core, n, s):
    conn = core._get_db()
    try:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?, ?, 'acknowledged_derives_from', ?)", (n, s, core._now()))
        conn.commit()
    finally:
        conn.close()


def _recompute(core, nid):
    conn = core._get_db()
    try:
        d = core._recompute_node_confidence(conn, nid)
        conn.commit()
        return d
    finally:
        conn.close()


def _conf(core, nid):
    conn = core._get_db()
    try:
        return conn.execute("SELECT confidence FROM nodes WHERE id=?",
                            (nid,)).fetchone()["confidence"]
    finally:
        conn.close()


def test_c1_corroboration_class_counts_only_the_replacement():
    rev, obs, derive, core = _fresh()
    t = _ob(obs, HARD)
    s = _ob(obs, EDITORIAL)
    n = _dv(derive, [t], "inductive_generalization")
    _supersede(rev, t, s)
    _ack_edge(core, n, s)
    d = _recompute(core, n)
    assert d["new"] == round(0.95 * C[EDITORIAL], 3), d


def test_c2_min_class_credits_a_stronger_replacement():
    rev, obs, derive, core = _fresh()
    a = _ob(obs, HARD)
    t = _ob(obs, EDITORIAL)
    s = _ob(obs, OFFICIAL)
    n = _dv(derive, [a, t], "deductive_modus_ponens")
    _supersede(rev, t, s)
    _ack_edge(core, n, s)
    assert _recompute(core, n)["new"] == round(0.98 * min(C[HARD], C[OFFICIAL]), 3)


def test_c3_min_class_weaker_replacement_is_used_not_max():
    rev, obs, derive, core = _fresh()
    a = _ob(obs, HARD)
    t = _ob(obs, OFFICIAL)
    s = _ob(obs, EDITORIAL)
    n = _dv(derive, [a, t], "deductive_modus_ponens")
    _supersede(rev, t, s)
    _ack_edge(core, n, s)
    assert _recompute(core, n)["new"] == round(0.98 * C[EDITORIAL], 3)


def test_c4_pending_node_keeps_the_superseded_premise():
    rev, obs, derive, core = _fresh()
    a = _ob(obs, HARD)
    t = _ob(obs, EDITORIAL)
    s = _ob(obs, OFFICIAL)
    n = _dv(derive, [a, t], "deductive_modus_ponens")
    _supersede(rev, t, s)  # no acknowledgement edge: still pending
    assert _recompute(core, n)["new"] == round(0.98 * C[EDITORIAL], 3)


def test_c5_supersede_chain_drops_the_original_premise():
    rev, obs, derive, core = _fresh()
    t = _ob(obs, HARD)
    t2 = _ob(obs, ANALYSIS)
    s = _ob(obs, EDITORIAL)
    n = _dv(derive, [t], "inductive_generalization")
    _supersede(rev, t, t2)
    _supersede(rev, t2, s)
    _ack_edge(core, n, s)
    assert _recompute(core, n)["new"] == round(0.95 * C[EDITORIAL], 3)


def test_c6_non_current_premise_without_acknowledged_successor_still_counted():
    rev, obs, derive, core = _fresh()
    a = _ob(obs, HARD)
    u = _ob(obs, EDITORIAL)      # superseded, but N never acknowledges v
    v = _ob(obs, OFFICIAL)
    w = _ob(obs, HARD)           # unrelated acknowledged target
    n = _dv(derive, [a, u], "deductive_modus_ponens")
    _supersede(rev, u, v)
    _ack_edge(core, n, w)
    assert _recompute(core, n)["new"] == round(0.98 * C[EDITORIAL], 3)


def test_c7_multi_premise_drops_only_the_acknowledged_superseded_one():
    rev, obs, derive, core = _fresh()
    a = _ob(obs, ANALYSIS)
    b = _ob(obs, OFFICIAL)
    t = _ob(obs, EDITORIAL)
    s = _ob(obs, OFFICIAL)
    n = _dv(derive, [a, b, t], "deductive_modus_ponens")
    _supersede(rev, t, s)
    _ack_edge(core, n, s)
    # A (0.70) must still bind: dropping T must not drop A or B.
    assert _recompute(core, n)["new"] == round(0.98 * C[ANALYSIS], 3)


def _premises(core, nid):
    conn = core._get_db()
    try:
        return core._recompute_premise_ids(conn, nid)
    finally:
        conn.close()


def _chain_reack_graph(rev, obs, derive, core, rtype, qts):
    """T <- S <- S2 (S supersedes T, S2 supersedes S); N derives_from T (and a
    second premise ``a`` when qts[0] is set) and has ack edges to BOTH S and S2
    (acks accumulate; the old one is never removed).
    ``qts`` = (quote_type of a or None, of T, of S, of S2)."""
    t = _ob(obs, qts[1])
    s = _ob(obs, qts[2])
    s2 = _ob(obs, qts[3])
    a = _ob(obs, qts[0]) if qts[0] else None
    n = _dv(derive, [a, t] if a else [t], rtype)
    _supersede(rev, t, s)
    _supersede(rev, s, s2)
    _ack_edge(core, n, s)
    _ack_edge(core, n, s2)
    return n, a, t, s, s2


def test_r1a_reacknowledgement_corroboration_counts_only_the_latest():
    rev, obs, derive, core = _fresh()
    n, a, t, s, s2 = _chain_reack_graph(
        rev, obs, derive, core, "inductive_generalization",
        (None, HARD, OFFICIAL, HARD))
    assert _premises(core, n) == [s2]
    # noisy-OR over {S, S2} would be 0.943; S2 alone is 0.95 x 0.95.
    assert _recompute(core, n)["new"] == round(0.95 * C[HARD], 3)


def test_r1b_reacknowledgement_min_class_uses_the_latest_not_the_stale_ack():
    rev, obs, derive, core = _fresh()
    n, a, t, s, s2 = _chain_reack_graph(
        rev, obs, derive, core, "deductive_modus_ponens",
        (HARD, OFFICIAL, EDITORIAL, ANALYSIS))
    assert sorted(_premises(core, n)) == sorted([a, s2])
    # S (0.35) is stale: min(A 0.95, S2 0.70) binds, not S.
    assert _recompute(core, n)["new"] == round(0.98 * C[ANALYSIS], 3)


def test_r2_partly_acknowledged_keeps_the_still_pending_premise():
    rev, obs, derive, core = _fresh()
    t1 = _ob(obs, HARD)
    t2 = _ob(obs, EDITORIAL)
    s1 = _ob(obs, OFFICIAL)
    s2 = _ob(obs, HARD)
    n = _dv(derive, [t1, t2], "deductive_modus_ponens")
    _supersede(rev, t1, s1)
    _supersede(rev, t2, s2)
    _ack_edge(core, n, s1)  # only T1's replacement is acknowledged
    assert sorted(_premises(core, n)) == sorted([s1, t2])
    assert _recompute(core, n)["new"] == round(0.98 * C[EDITORIAL], 3)


def test_r3_supersede_cycle_among_ack_targets_never_empties_the_premise_set():
    rev, obs, derive, core = _fresh()
    a = _ob(obs, HARD)
    b = _ob(obs, OFFICIAL)
    c = _ob(obs, ANALYSIS)  # N's own derives_from premise, unrelated to the cycle
    n = _dv(derive, [c], "deductive_modus_ponens")
    conn = core._get_db()
    try:
        for src, tgt in ((a, b), (b, a)):  # malformed: each supersedes the other
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at) "
                "VALUES (?, ?, 'supersedes', ?)", (src, tgt, core._now()))
        conn.commit()
    finally:
        conn.close()
    _ack_edge(core, n, a)
    _ack_edge(core, n, b)
    first = _premises(core, n)
    assert first, "premise set must never be emptied by a supersede cycle"
    assert sorted(first) == sorted([a, b, c])
    assert _premises(core, n) == first  # deterministic


def test_c8_descendants_are_recomputed_from_the_new_value():
    rev, obs, derive, core = _fresh()
    a = _ob(obs, HARD)
    t = _ob(obs, EDITORIAL)
    s = _ob(obs, OFFICIAL)
    n = _dv(derive, [a, t], "deductive_modus_ponens")
    child = _dv(derive, [n], "deductive_modus_ponens")
    _supersede(rev, t, s)
    _ack_edge(core, n, s)
    conn = core._get_db()
    try:
        rev._cascade_recompute_confidence_transitive(conn, t)
        conn.commit()
    finally:
        conn.close()
    n_new = round(0.98 * C[OFFICIAL], 3)
    assert _conf(core, n) == n_new
    assert _conf(core, child) == round(n_new * 0.98, 3)


# ---------------------------------------------------------------------------
# Sibling copy: tools/surgical.py::_compute_confidence_for_node must use the
# SAME premise set as _recompute_node_confidence (one definition).
# ---------------------------------------------------------------------------

SURGICAL_PATH = REPO_ROOT / "src" / "engram" / "tools" / "surgical.py"


def _surgical_conf(core, nid):
    sys.modules.pop("surgical_3531", None)
    spec = importlib.util.spec_from_file_location("surgical_3531", SURGICAL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    conn = core._get_db()
    try:
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (nid,)).fetchone()
        return mod._compute_confidence_for_node(conn, dict(row))
    finally:
        conn.close()


def test_x1_surgical_matches_recompute_min_class_c2_shape():
    rev, obs, derive, core = _fresh()
    a = _ob(obs, HARD)
    t = _ob(obs, EDITORIAL)
    s = _ob(obs, OFFICIAL)
    n = _dv(derive, [a, t], "deductive_modus_ponens")
    _supersede(rev, t, s)
    _ack_edge(core, n, s)
    want = _recompute(core, n)["new"]
    assert want == round(0.98 * min(C[HARD], C[OFFICIAL]), 3)
    assert _surgical_conf(core, n) == want


def test_x2_surgical_matches_recompute_corroboration_c1_shape():
    rev, obs, derive, core = _fresh()
    t = _ob(obs, HARD)
    s = _ob(obs, EDITORIAL)
    n = _dv(derive, [t], "inductive_generalization")
    _supersede(rev, t, s)
    _ack_edge(core, n, s)
    want = _recompute(core, n)["new"]
    assert want == round(0.95 * C[EDITORIAL], 3)
    assert _surgical_conf(core, n) == want


# ---------------------------------------------------------------------------
# Standing-population sweep
# ---------------------------------------------------------------------------


def _load_sweep():
    spec = importlib.util.spec_from_file_location("ack_recompute_sweep", SWEEP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _inflated_graph():
    """Two acknowledged nodes whose STORED values are the pre-fix (inflated /
    pinned) numbers, one plain node that must not be touched, and one
    descendant. Returns ids."""
    rev, obs, derive, core = _fresh()
    t = _ob(obs, EDITORIAL)
    s = _ob(obs, OFFICIAL)
    a = _ob(obs, HARD)
    n1 = _dv(derive, [t], "inductive_generalization")
    n2 = _dv(derive, [a, t], "deductive_modus_ponens")
    child = _dv(derive, [n2], "deductive_modus_ponens")
    plain = _dv(derive, [a], "deductive_modus_ponens")
    _supersede(rev, t, s)
    _ack_edge(core, n1, s)
    _ack_edge(core, n2, s)
    # Pre-fix state: noisy-OR over {T,S} for n1, min(A,T,S) for n2.
    n1_old = round(0.95 * (1 - (1 - C[EDITORIAL]) * (1 - C[OFFICIAL])), 3)
    n2_old = round(0.98 * C[EDITORIAL], 3)
    conn = core._get_db()
    try:
        for nid, v in ((n1, n1_old), (n2, n2_old),
                       (child, round(n2_old * 0.98, 3))):
            conn.execute("UPDATE nodes SET confidence=? WHERE id=?", (v, nid))
        conn.commit()
    finally:
        conn.close()
    return core, dict(n1=n1, n2=n2, child=child, plain=plain,
                      n1_old=n1_old, n2_old=n2_old)


def _snapshot(core):
    conn = core._get_db()
    try:
        return {r["id"]: r["confidence"] for r in conn.execute(
            "SELECT id, confidence FROM nodes").fetchall()}
    finally:
        conn.close()


def test_s1_dry_run_writes_nothing():
    core, g = _inflated_graph()
    sweep = _load_sweep()
    before = _snapshot(core)
    conn = core._get_db()
    try:
        report = sweep.sweep(conn, apply=False)
    finally:
        conn.close()
    assert _snapshot(core) == before
    changed = {r["node_id"] for r in report["changes"]}
    assert {g["n1"], g["n2"], g["child"]} <= changed
    assert g["plain"] not in changed
    dropped = {r["node_id"]: r["dropped"] for r in report["changes"]}
    assert dropped[g["n1"]] and dropped[g["n2"]]


def test_s2_apply_changes_exactly_the_predicted_nodes():
    core, g = _inflated_graph()
    sweep = _load_sweep()
    conn = core._get_db()
    try:
        predicted = sweep.sweep(conn, apply=False)
    finally:
        conn.close()
    before = _snapshot(core)
    conn = core._get_db()
    try:
        applied = sweep.sweep(conn, apply=True)
    finally:
        conn.close()
    after = _snapshot(core)
    moved = {k for k in after if after[k] != before[k]}
    assert moved == {r["node_id"] for r in predicted["changes"]}
    assert moved == {g["n1"], g["n2"], g["child"]}
    assert {(r["node_id"], r["new"]) for r in applied["changes"]} == \
           {(r["node_id"], r["new"]) for r in predicted["changes"]}
    assert after[g["n1"]] == round(0.95 * C[OFFICIAL], 3)
    assert after[g["n2"]] == round(0.98 * C[OFFICIAL], 3)


def test_s3_second_apply_is_a_no_op():
    core, g = _inflated_graph()
    sweep = _load_sweep()
    conn = core._get_db()
    try:
        sweep.sweep(conn, apply=True)
    finally:
        conn.close()
    once = _snapshot(core)
    conn = core._get_db()
    try:
        second = sweep.sweep(conn, apply=True)
    finally:
        conn.close()
    assert second["changes"] == []
    assert _snapshot(core) == once


# ---------------------------------------------------------------------------
# Fold 3 -- one premise counted once. N with BOTH a derives_from and an
# acknowledged_derives_from edge to the same P must list P once.
# ---------------------------------------------------------------------------


def test_d1_corroboration_premise_with_both_edges_counted_once():
    rev, obs, derive, core = _fresh()
    p = _ob(obs, HARD)
    n = _dv(derive, [p], "inductive_generalization")
    _ack_edge(core, n, p)
    assert _premises(core, n) == [p]
    # discount x c(P); a doubled premise would noisy-OR to 1-(1-c)^2.
    assert _recompute(core, n)["new"] == round(0.95 * C[HARD], 3)


def test_d2_min_class_premise_with_both_edges_listed_once_value_unchanged():
    rev, obs, derive, core = _fresh()
    a = _ob(obs, HARD)
    p = _ob(obs, OFFICIAL)
    n = _dv(derive, [a, p], "deductive_modus_ponens")
    _ack_edge(core, n, p)
    assert sorted(_premises(core, n)) == sorted([a, p])
    assert _recompute(core, n)["new"] == round(0.98 * min(C[HARD], C[OFFICIAL]), 3)


def test_d3_surgical_matches_recompute_on_the_both_edges_shape():
    rev, obs, derive, core = _fresh()
    p = _ob(obs, HARD)
    n = _dv(derive, [p], "inductive_generalization")
    _ack_edge(core, n, p)
    want = _recompute(core, n)["new"]
    assert want == round(0.95 * C[HARD], 3)
    assert _surgical_conf(core, n) == want
