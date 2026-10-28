"""Tests for #3586: supersede (and the startup reconciler) must keep a target's
resolution state coherent with its LIVE resolvers, exactly as retract has since
#211/#762.

Contract cells (written before the implementation; see the #3586 ruling):

  H1-H5  core._recompute_resolution_target(conn, target_id, threshold)
  R1     retract behaviour unchanged (pinned by the pre-existing retract tests,
         plus R1-parity cells here)
  R2/R3  _resolve_impl no-op self-heal re-points a NON-live resolved_by
  S1-S7  _supersede_impl recomputes every target the old node resolved
  B1-B4  core._reconcile_resolution_targets + its startup call site

Fixtures use a temp DB only (never the live data dir). Stale states are built
with a raw sqlite3 connection or the helper's own connection, never by relying
on the startup reconciler.
"""

import atexit
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402

import engram_core
import engram_derive
import engram_epistemic
import engram_observation
import engram_revision

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_supersede_resolution_3586_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

THRESHOLD = 0.7
_SEQ = [0]


def _mods():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    rev = fresh_module(engram_revision)
    obs = importlib.import_module("engram_observation")
    derive = importlib.import_module("engram_derive")
    epi = importlib.import_module("engram_epistemic")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    # Open once so the schema exists; one-time setup is then marked done for
    # this path and will not re-run the reconciler behind the tests' backs.
    core._get_db().close()
    return rev, obs, derive, epi, core


def _ob(obs, claim="A source claim."):
    _SEQ[0] += 1
    r = json.loads(obs._add_observation_impl(
        url=f"https://example.com/s{_SEQ[0]}", title=f"S{_SEQ[0]}",
        quoted_text=claim, interpretation=f"I: {claim}", claim=claim,
        quote_type="hard_data"))
    assert "observation_id" in r, r
    return r["observation_id"]


def _dv(derive, ob_id, claim, conf=None, core=None):
    r = json.loads(derive._derive_impl(
        claim=claim, supporting_ids=ob_id,
        logical_chain=f"Chain: {claim}",
        reasoning_type="deductive_modus_ponens"))
    assert "derivation_id" in r, r
    dv = r["derivation_id"]
    if conf is not None:
        _sql(core, "UPDATE nodes SET confidence = ? WHERE id = ?", (conf, dv))
    return dv


def _q(derive, text="Is it so?"):
    r = json.loads(derive._ask_impl(question=text))
    assert "question_id" in r, r
    return r["question_id"]


def _sql(core, sql, args=()):
    c = sqlite3.connect(str(core.DB_PATH))
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _row(core, node_id):
    c = sqlite3.connect(str(core.DB_PATH))
    c.row_factory = sqlite3.Row
    try:
        return c.execute(
            "SELECT id, type, status, resolved_by, is_current FROM nodes WHERE id = ?",
            (node_id,)).fetchone()
    finally:
        c.close()


def _edit_rows(core, action="resolution_recomputed"):
    c = sqlite3.connect(str(core.DB_PATH))
    try:
        return c.execute(
            "SELECT node_id, details FROM edit_history WHERE action = ?",
            (action,)).fetchall()
    finally:
        c.close()


def _resolve(rev, target, resolver, **kw):
    r = json.loads(rev._resolve_impl(
        target_id=target, resolving_node_id=resolver, **kw))
    assert r.get("status") in ("resolution_created", "no_op"), r
    return r


def _edge_exists(core, src, tgt, rel="resolves"):
    c = sqlite3.connect(str(core.DB_PATH))
    try:
        return c.execute(
            "SELECT 1 FROM edges WHERE source_id=? AND target_id=? AND relation=?",
            (src, tgt, rel)).fetchone() is not None
    finally:
        c.close()


def _recompute(core, target, cause="test"):
    conn = core._get_db()
    try:
        res = core._recompute_resolution_target(conn, target, THRESHOLD, cause=cause)
        conn.commit()
        return res
    finally:
        conn.close()


def _supersede(rev, old, new):
    r = json.loads(rev._supersede_impl(
        old_node_id=old, new_node_id=new, supersede_reason="test"))
    assert r.get("status") == "superseded", r
    return r


def _resolved_question(rev, obs, derive, core, conf=0.9):
    """Q resolved by a single resolver dv1. Returns (q, dv1, ob)."""
    ob = _ob(obs)
    q = _q(derive)
    dv1 = _dv(derive, ob, "Resolver one.", conf, core)
    _resolve(rev, q, dv1)
    return q, dv1, ob


# ── H: the helper ────────────────────────────────────────────────────────


def test_H1_no_live_resolver_reopens():
    rev, obs, derive, epi, core = _mods()
    q, dv1, ob = _resolved_question(rev, obs, derive, core)
    _sql(core, "UPDATE nodes SET is_current = 0 WHERE id = ?", (dv1,))
    res = _recompute(core, q)
    assert res["action"] == "reopened", res
    row = _row(core, q)
    assert row["status"] == "open" and row["resolved_by"] is None
    assert res["before_status"] == "resolved" and res["after_status"] == "open"
    assert res["before_resolved_by"] == dv1 and res["after_resolved_by"] is None
    assert set(res) >= {"id", "type", "claim", "action", "before_status",
                        "after_status", "before_resolved_by", "after_resolved_by"}
    logged = _edit_rows(core)
    assert len(logged) == 1 and json.loads(logged[0][1])["cause"] == "test"


def test_H1_retracted_resolver_also_not_live():
    rev, obs, derive, epi, core = _mods()
    q, dv1, ob = _resolved_question(rev, obs, derive, core)
    _sql(core, "UPDATE nodes SET status = 'retracted' WHERE id = ?", (dv1,))
    assert _recompute(core, q)["action"] == "reopened"


def test_H2_argmax_and_threshold():
    rev, obs, derive, epi, core = _mods()
    ob = _ob(obs)
    q = _q(derive)
    hi = _dv(derive, ob, "High resolver.", 0.9, core)
    lo = _dv(derive, ob, "Low resolver.", 0.6, core)
    _resolve(rev, q, hi)
    _resolve(rev, q, lo)
    # Stale: pointer at the weaker one, status partially_resolved.
    _sql(core, "UPDATE nodes SET resolved_by = ?, status = 'partially_resolved' WHERE id = ?",
         (lo, q))
    res = _recompute(core, q)
    assert res["action"] == "repointed", res
    row = _row(core, q)
    assert row["resolved_by"] == hi and row["status"] == "resolved"
    # Now drop the strong one below threshold-reaching: only lo remains live.
    _sql(core, "UPDATE nodes SET is_current = 0 WHERE id = ?", (hi,))
    res = _recompute(core, q)
    assert res["action"] == "repointed", res
    row = _row(core, q)
    assert row["resolved_by"] == lo and row["status"] == "partially_resolved"


def test_H2_null_confidence_counts_as_zero():
    rev, obs, derive, epi, core = _mods()
    q, dv1, ob = _resolved_question(rev, obs, derive, core)
    _sql(core, "UPDATE nodes SET confidence = NULL WHERE id = ?", (dv1,))
    res = _recompute(core, q)
    row = _row(core, q)
    assert row["status"] == "partially_resolved" and row["resolved_by"] == dv1, res
    assert res["action"] == "repointed", res


def test_H3_categorical_only_pointer_moves():
    rev, obs, derive, epi, core = _mods()
    ob = _ob(obs)
    cj = json.loads(epi._add_conjecture_impl(claim="A hunch.", basis="b"))
    cj_id = cj.get("conjecture_id") or cj.get("id")
    assert cj_id, cj
    d1 = _dv(derive, ob, "Supports hunch.", 0.9, core)
    d2 = _dv(derive, ob, "Also supports hunch.", 0.8, core)
    _resolve(rev, cj_id, d1, prediction_outcome="supported")
    _resolve(rev, cj_id, d2, prediction_outcome="supported")
    assert _row(core, cj_id)["resolved_by"] == d2
    _sql(core, "UPDATE nodes SET is_current = 0 WHERE id = ?", (d2,))
    res = _recompute(core, cj_id)
    row = _row(core, cj_id)
    assert row["resolved_by"] == d1 and row["status"] == "supported", row
    assert res["action"] == "repointed", res


def test_H4_consistent_writes_nothing():
    rev, obs, derive, epi, core = _mods()
    q, dv1, ob = _resolved_question(rev, obs, derive, core)
    open_q = _q(derive, "Never resolved?")
    before = len(_edit_rows(core))
    r1 = _recompute(core, q)
    r2 = _recompute(core, open_q)
    assert r1["action"] == "unchanged" and r2["action"] == "unchanged"
    assert _row(core, q)["resolved_by"] == dv1
    assert _row(core, open_q)["status"] == "open"
    assert len(_edit_rows(core)) == before == 0


def test_H5_non_current_target_untouched():
    rev, obs, derive, epi, core = _mods()
    q, dv1, ob = _resolved_question(rev, obs, derive, core)
    _sql(core, "UPDATE nodes SET is_current = 0 WHERE id IN (?, ?)", (dv1, q))
    res = _recompute(core, q)
    assert res["action"] == "unchanged"
    row = _row(core, q)
    assert row["status"] == "resolved" and row["resolved_by"] == dv1


# ── R: retract parity + resolve self-heal ────────────────────────────────


def test_R1_retract_parity_reopen_and_repoint():
    """Retract of the only resolver reopens; of the stronger one re-points.
    (The pre-existing retract suites are the full R1 pin; this is a smoke.)"""
    rev, obs, derive, epi, core = _mods()
    ob = _ob(obs)
    q = _q(derive)
    hi = _dv(derive, ob, "Strong.", 0.9, core)
    lo = _dv(derive, ob, "Weak.", 0.6, core)
    _resolve(rev, q, hi)
    _resolve(rev, q, lo)
    r = json.loads(rev._retract_impl(
        node_id=hi, error_type="other", reason="r",
        replacement_absent_because="NONE_BECAUSE_UNKNOWN",
        replacement_absent_note="test fixture"))
    assert "error" not in r, r
    row = _row(core, q)
    assert row["resolved_by"] == lo and row["status"] == "partially_resolved"
    assert "reopened_targets" not in r or r.get("reopened_count", 0) == 0
    r = json.loads(rev._retract_impl(
        node_id=lo, error_type="other", reason="r",
        replacement_absent_because="NONE_BECAUSE_UNKNOWN",
        replacement_absent_note="test fixture"))
    assert "error" not in r, r
    row = _row(core, q)
    assert row["status"] == "open" and row["resolved_by"] is None
    assert r["reopened_count"] == 1
    assert r["reopened_targets"][0]["id"] == q
    assert set(r["reopened_targets"][0]) == {"id", "type", "claim"}


def test_R2_resolve_noop_repoints_non_live_resolved_by():
    rev, obs, derive, epi, core = _mods()
    ob = _ob(obs)
    q = _q(derive)
    d1 = _dv(derive, ob, "First.", 0.9, core)
    d2 = _dv(derive, ob, "Second.", 0.8, core)
    _resolve(rev, q, d1)
    _resolve(rev, q, d2)
    assert _row(core, q)["resolved_by"] == d1
    _sql(core, "UPDATE nodes SET is_current = 0 WHERE id = ?", (d1,))
    r = _resolve(rev, q, d2)
    assert r["status"] == "no_op"
    row = _row(core, q)
    assert row["resolved_by"] == d2
    assert row["status"] == "resolved", "status must stay untouched (pointer only)"


def test_R3_resolve_noop_null_resolved_by_still_heals():
    rev, obs, derive, epi, core = _mods()
    q, dv1, ob = _resolved_question(rev, obs, derive, core)
    _sql(core, "UPDATE nodes SET resolved_by = NULL WHERE id = ?", (q,))
    r = _resolve(rev, q, dv1)
    assert r["status"] == "no_op"
    assert _row(core, q)["resolved_by"] == dv1


# ── S: supersede ─────────────────────────────────────────────────────────


def test_S1_S3_S4_only_resolver_superseded_reopens_keeps_old_edge_no_new_edge():
    rev, obs, derive, epi, core = _mods()
    q, dv1, ob = _resolved_question(rev, obs, derive, core)
    dv2 = _dv(derive, ob, "Resolver one, restated.", 0.9, core)
    _supersede(rev, dv1, dv2)
    row = _row(core, q)
    assert row["status"] == "open" and row["resolved_by"] is None          # S1
    assert _edge_exists(core, dv1, q), "old resolves edge is audit trail"   # S3
    assert not _edge_exists(core, dv2, q), "no carry to successor"          # S4


def test_S2_other_live_resolver_partially_resolved():
    rev, obs, derive, epi, core = _mods()
    ob = _ob(obs)
    q = _q(derive)
    r1 = _dv(derive, ob, "Strong.", 0.9, core)
    r2 = _dv(derive, ob, "Weak.", 0.6, core)
    _resolve(rev, q, r1)
    _resolve(rev, q, r2)
    r1b = _dv(derive, ob, "Strong, restated.", 0.9, core)
    _supersede(rev, r1, r1b)
    row = _row(core, q)
    assert row["status"] == "partially_resolved" and row["resolved_by"] == r2


def test_S5_response_surfaces_targets_and_hint():
    rev, obs, derive, epi, core = _mods()
    q, dv1, ob = _resolved_question(rev, obs, derive, core)
    dv2 = _dv(derive, ob, "Resolver one, restated.", 0.9, core)
    resp = _supersede(rev, dv1, dv2)
    targets = resp["resolution_targets"]
    assert len(targets) == 1 and targets[0]["id"] == q
    assert targets[0]["action"] == "reopened"
    assert targets[0]["before_status"] == "resolved"
    assert targets[0]["after_status"] == "open"
    hint = resp["resolution_hint"]
    assert "engram_resolve" in hint
    assert f"resolving_node_id={dv2}" in hint
    assert q in hint
    assert "only if the successor answers it" in hint
    logged = _edit_rows(core)
    assert len(logged) == 1
    assert json.loads(logged[0][1])["cause"] == f"supersede:{dv1}->{dv2}"


def test_S5_keys_omitted_when_nothing_resolved():
    rev, obs, derive, epi, core = _mods()
    ob = _ob(obs)
    d1 = _dv(derive, ob, "Resolves nothing.", None, core)
    d2 = _dv(derive, ob, "Resolves nothing, restated.", None, core)
    resp = _supersede(rev, d1, d2)
    assert "resolution_targets" not in resp
    assert "resolution_hint" not in resp


def test_S6_cascade_resolve_superseded_route_recomputes():
    rev, obs, derive, epi, core = _mods()
    ob = _ob(obs)
    q = _q(derive)
    dv = _dv(derive, ob, "Resolver of Q.", 0.9, core)
    _resolve(rev, q, dv)
    ob2 = _ob(obs, "Replacement premise.")
    _supersede(rev, ob, ob2)  # dv becomes a pending-marker frontier node
    dv_new = _dv(derive, ob2, "Resolver of Q, rebuilt.", 0.9, core)
    resp = json.loads(rev._cascade_resolve_impl(
        node_id=dv, resolution_kind="superseded", replacement_id=dv_new))
    assert "error" not in resp, resp
    row = _row(core, q)
    assert row["status"] == "open" and row["resolved_by"] is None


def test_S7_categorical_only_resolver_superseded_reopens():
    rev, obs, derive, epi, core = _mods()
    ob = _ob(obs)
    cj = json.loads(epi._add_conjecture_impl(claim="Another hunch.", basis="b"))
    cj_id = cj.get("conjecture_id") or cj.get("id")
    d1 = _dv(derive, ob, "Supports.", 0.9, core)
    _resolve(rev, cj_id, d1, prediction_outcome="supported")
    d2 = _dv(derive, ob, "Supports, restated.", 0.9, core)
    _supersede(rev, d1, d2)
    row = _row(core, cj_id)
    assert row["status"] == "open" and row["resolved_by"] is None


# ── B: startup reconciler ────────────────────────────────────────────────


def _stale_graph(rev, obs, derive, core):
    """The three shapes from the ruling. Returns (q1, q2, q3, live_resolver)."""
    ob = _ob(obs)
    # shape 1: NULL resolved_by + only-superseded resolver
    q1 = _q(derive, "Shape one?")
    a = _dv(derive, ob, "Resolver A.", 0.9, core)
    _resolve(rev, q1, a)
    # shape 2: stale resolved_by + no live resolver
    q2 = _q(derive, "Shape two?")
    b = _dv(derive, ob, "Resolver B.", 0.9, core)
    _resolve(rev, q2, b)
    # shape 3 (seen on a live graph): stale resolved_by + a live resolver exists
    q3 = _q(derive, "Shape three?")
    c = _dv(derive, ob, "Resolver C.", 0.9, core)
    live = _dv(derive, ob, "Live resolver.", 0.8, core)
    _resolve(rev, q3, c)
    _resolve(rev, q3, live)
    _sql(core, "UPDATE nodes SET is_current = 0 WHERE id IN (?, ?, ?)", (a, b, c))
    _sql(core, "UPDATE nodes SET resolved_by = NULL WHERE id = ?", (q1,))
    return q1, q2, q3, live


def _reconcile(core):
    conn = core._get_db()
    try:
        out = core._reconcile_resolution_targets(conn)
        conn.commit()
        return out
    finally:
        conn.close()


def test_B1_all_three_shapes_fixed():
    rev, obs, derive, epi, core = _mods()
    q1, q2, q3, live = _stale_graph(rev, obs, derive, core)
    out = _reconcile(core)
    assert {r["id"] for r in out} == {q1, q2, q3}, out
    for q in (q1, q2):
        row = _row(core, q)
        assert row["status"] == "open" and row["resolved_by"] is None, (q, dict(row))
    row3 = _row(core, q3)
    assert row3["resolved_by"] == live and row3["status"] == "resolved"
    causes = {json.loads(d)["cause"] for _, d in _edit_rows(core)}
    assert causes == {"startup_reconcile"}


def test_B1_categorical_non_live_resolved_by_repointed():
    rev, obs, derive, epi, core = _mods()
    ob = _ob(obs)
    cj = json.loads(epi._add_conjecture_impl(claim="Third hunch.", basis="b"))
    cj_id = cj.get("conjecture_id") or cj.get("id")
    d1 = _dv(derive, ob, "One.", 0.9, core)
    d2 = _dv(derive, ob, "Two.", 0.8, core)
    _resolve(rev, cj_id, d1, prediction_outcome="supported")
    _resolve(rev, cj_id, d2, prediction_outcome="supported")
    _sql(core, "UPDATE nodes SET is_current = 0 WHERE id = ?", (d2,))
    out = _reconcile(core)
    assert [r["id"] for r in out] == [cj_id]
    row = _row(core, cj_id)
    assert row["resolved_by"] == d1 and row["status"] == "supported"


def test_B2_idempotent():
    rev, obs, derive, epi, core = _mods()
    _stale_graph(rev, obs, derive, core)
    assert len(_reconcile(core)) == 3
    n = len(_edit_rows(core))
    assert _reconcile(core) == []
    assert len(_edit_rows(core)) == n


def test_B3_consistent_graph_untouched():
    rev, obs, derive, epi, core = _mods()
    q, dv1, ob = _resolved_question(rev, obs, derive, core)
    assert _reconcile(core) == []
    row = _row(core, q)
    assert row["status"] == "resolved" and row["resolved_by"] == dv1
    assert _edit_rows(core) == []


def test_B4_call_site_one_stderr_line_when_nonzero(capsys):
    rev, obs, derive, epi, core = _mods()
    _stale_graph(rev, obs, derive, core)
    capsys.readouterr()
    conn = core._get_db()
    try:
        core._run_db_one_time_setup(conn)
    finally:
        conn.close()
    cap = capsys.readouterr()
    lines = [ln for ln in cap.err.splitlines() if "reconcil" in ln.lower()]
    assert len(lines) == 1, cap.err
    assert "3" in lines[0], lines
    assert "reconcil" not in cap.out.lower(), "stdout is the MCP channel"


def test_B4_call_site_silent_when_zero(capsys):
    rev, obs, derive, epi, core = _mods()
    _resolved_question(rev, obs, derive, core)
    capsys.readouterr()
    conn = core._get_db()
    try:
        core._run_db_one_time_setup(conn)
    finally:
        conn.close()
    cap = capsys.readouterr()
    assert "reconcil" not in (cap.err + cap.out).lower()


def test_B5_resolved_target_with_zero_resolves_edges_left_alone():
    """A resolved target that NEVER had a resolves edge is a different defect
    from #3586's ("its resolvers went non-live"); the reconciler must not
    reopen it under #3586's name."""
    rev, obs, derive, epi, core = _mods()
    q = _q(derive, "Resolved with no edge ever?")
    _sql(core, "UPDATE nodes SET status = 'resolved', resolved_by = NULL WHERE id = ?", (q,))
    assert _reconcile(core) == []
    assert _row(core, q)["status"] == "resolved"
    assert _edit_rows(core) == []


# ── single source for the "live node" predicate ──────────────────────────


def test_live_node_predicate_has_one_source():
    """`is_current = 1 AND status != 'retracted'` is written once (the
    _LIVE_NODE_PREDICATE constant in engram_core); every other resolution query
    formats it in, so the notion of "live" cannot drift between them."""
    import re
    src_dir = Path(engram_core.__file__).resolve().parent
    pat = re.compile(r"is_current\s*=\s*1\s+AND\s+[\w{}.]*status\s*!=\s*'retracted'")
    core_text = (src_dir / "engram_core.py").read_text()
    # #3542: the six fixed dag_edge_set SQL templates are LITERALS (the owner read
    # registry's scanner admits nothing built at import time), so three of them
    # carry the live-node text by necessity. They are exempt here ONLY because
    # tests/test_edge_liveness_ssot_3542.py pins each, character for character, to
    # what _CURRENT_EDGE_PREDICATE (built on _LIVE_NODE_PREDICATE) generates.
    import ast
    original_text = core_text
    tree = ast.parse(core_text)
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.startswith("_DAG_EDGES_")
                and node.targets[0].id.endswith("_SQL")):
            core_text = core_text.replace(ast.get_source_segment(original_text, node), "")
    core_hits = pat.findall(core_text)
    rev_hits = pat.findall((src_dir / "engram_revision.py").read_text())
    assert len(core_hits) == 1, core_hits
    assert rev_hits == [], rev_hits


def test_is_live_node():
    rev, obs, derive, epi, core = _mods()
    ob = _ob(obs)
    d = _dv(derive, ob, "Some derivation.", 0.9, core)
    conn = core._get_db()
    try:
        assert core._is_live_node(conn, d) is True
        assert core._is_live_node(conn, None) is False
        assert core._is_live_node(conn, "dv_9999") is False
        _sql(core, "UPDATE nodes SET is_current = 0 WHERE id = ?", (d,))
        assert core._is_live_node(conn, d) is False
        _sql(core, "UPDATE nodes SET is_current = 1, status = 'retracted' WHERE id = ?", (d,))
        assert core._is_live_node(conn, d) is False
    finally:
        conn.close()


def test_H6_stale_resolver_successor_is_chain_head_and_not_carried():
    rev, obs, derive, epi, core = _mods()
    ob = _ob(obs)
    q = _q(derive)
    a = _dv(derive, ob, "Resolver A.", 0.9, core)
    b = _dv(derive, ob, "Resolver B.", 0.9, core)
    c = _dv(derive, ob, "Resolver C.", 0.9, core)
    _resolve(rev, q, a)
    # A -> B -> C by supersede; the first supersede reopens Q, which is fine.
    _supersede(rev, a, b)
    _supersede(rev, b, c)
    # Re-create the stale state so the recompute under test does the work.
    _sql(core, "UPDATE nodes SET status = 'resolved', resolved_by = ? WHERE id = ?", (a, q))
    res = _recompute(core, q)
    assert res["action"] == "reopened", res
    assert res["stale_resolver_successors"] == [c], res
    logged = json.loads(_edit_rows(core)[-1][1])
    assert logged["stale_resolver_successors"] == [c]
    assert not _edge_exists(core, c, q) and not _edge_exists(core, b, q)


def test_H6_no_successor_for_dead_end_or_cycle():
    rev, obs, derive, epi, core = _mods()
    q, dv1, ob = _resolved_question(rev, obs, derive, core)
    _sql(core, "UPDATE nodes SET is_current = 0 WHERE id = ?", (dv1,))  # dead end
    assert _recompute(core, q)["stale_resolver_successors"] == []
    q2, d2, _ = _resolved_question(rev, obs, derive, core)
    _sql(core, "UPDATE nodes SET is_current = 0, superseded_by = ? WHERE id = ?", (d2, d2))  # cycle
    assert _recompute(core, q2)["stale_resolver_successors"] == []


def test_H7_status_change_flags_needs_tightening_only_with_summary():
    rev, obs, derive, epi, core = _mods()
    ob = _ob(obs)
    with_sum, _, _ = _resolved_question(rev, obs, derive, core)
    no_sum, _, _ = _resolved_question(rev, obs, derive, core)
    _sql(core, "UPDATE nodes SET recall_summary = 'Resolved: yes.', needs_tightening = 0 "
               "WHERE id = ?", (with_sum,))
    _sql(core, "UPDATE nodes SET recall_summary = NULL, needs_tightening = 0 WHERE id = ?",
         (no_sum,))
    # Pointer-only repoint (categorical, status kept) with a summary.
    cj = json.loads(epi._add_conjecture_impl(claim="H7 hunch.", basis="b"))
    cj_id = cj.get("conjecture_id") or cj.get("id")
    d1 = _dv(derive, ob, "One.", 0.9, core)
    d2 = _dv(derive, ob, "Two.", 0.8, core)
    _resolve(rev, cj_id, d1, prediction_outcome="supported")
    _resolve(rev, cj_id, d2, prediction_outcome="supported")
    _sql(core, "UPDATE nodes SET recall_summary = 'Supported.', needs_tightening = 0 "
               "WHERE id = ?", (cj_id,))
    # Orphan all resolvers of the two questions, and the newer conjecture resolver.
    _sql(core, "UPDATE nodes SET is_current = 0 WHERE id IN "
               "(SELECT source_id FROM edges WHERE relation = 'resolves' "
               "AND target_id IN (?, ?))", (with_sum, no_sum))
    _sql(core, "UPDATE nodes SET is_current = 0 WHERE id = ?", (d2,))
    assert len(_reconcile(core)) == 3

    def nt(i):
        c = sqlite3.connect(str(core.DB_PATH))
        try:
            return c.execute("SELECT needs_tightening FROM nodes WHERE id = ?", (i,)).fetchone()[0]
        finally:
            c.close()
    assert _row(core, with_sum)["status"] == "open" and nt(with_sum) == 1
    assert _row(core, no_sum)["status"] == "open" and nt(no_sum) == 0
    assert _row(core, cj_id)["status"] == "supported" and nt(cj_id) == 0


def test_H8_threshold_boundary_is_inclusive():
    """A best live resolver whose confidence EQUALS the threshold resolves the
    target; one just below leaves it partially_resolved (pins `>=`, not `>`)."""
    rev, obs, derive, epi, core = _mods()
    ob = _ob(obs)
    q_at = _q(derive, "At the threshold?")
    q_below = _q(derive, "Just below the threshold?")
    at = _dv(derive, ob, "Exactly at threshold.", THRESHOLD, core)
    below = _dv(derive, ob, "Just below threshold.", THRESHOLD - 0.001, core)
    _resolve(rev, q_at, at)
    _resolve(rev, q_below, below)
    # Force a stale status so the recompute (not the resolve path) decides it.
    _sql(core, "UPDATE nodes SET status = 'open', resolved_by = NULL "
               "WHERE id IN (?, ?)", (q_at, q_below))
    assert _recompute(core, q_at)["after_status"] == "resolved"
    assert _recompute(core, q_below)["after_status"] == "partially_resolved"
    assert _row(core, q_at)["status"] == "resolved"
    assert _row(core, q_below)["status"] == "partially_resolved"
