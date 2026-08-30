"""DAG-exemptions for back-reference and relational edges (issue #1076).

Three relation classes now have temporal_causal=False per Borges's three-tier edge taxonomy:

  - resolves   back-reference: resolver can legitimately pre-date the question
               (question filed retroactively about a pre-existing answer).
  - tensions   relational tier, symmetric/cross-temporal — same class as
               contradicts (already exempt). No causal direction.
  - serves     classification tier — a goal can be articulated after the node
               that serves it (same shape as instantiates, already exempt).

Coverage:
  1.  resolves/tensions/serves are in DAG_EXEMPT_RELATIONS.
  2.  Startup scan SQL produces zero violations for back-reference resolves
      edge (resolver older than question).
  3.  Mutation guard — the same resolves edge WOULD appear as a violation if
      resolves were not exempt (verifies the test would catch a regression).
  4.  tensions in DAG_EXEMPT_RELATIONS — constant-membership guard.
  5.  serves in DAG_EXEMPT_RELATIONS — constant-membership guard.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_dag_resolves_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    if "server" in sys.modules:
        del sys.modules["server"]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


# ── helpers ───────────────────────────────────────────────────────────────────

def _add_obs(server):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/dag-resolves-test",
        "title": "Pre-existing finding",
        "quoted_text": "The effect is well-documented.",
        "interpretation": "Strong evidence for the phenomenon.",
        "claim": "The effect holds under standard conditions.",
        "quote_type": "hard_data",
    })))
    assert "error" not in r, r
    return r["observation_id"]


def _add_question(server):
    r = json.loads(server.engram_ask(payload_json=json.dumps({
        "question": "Does the effect hold under standard conditions?",
        "category": "research",
    })))
    assert "error" not in r, r
    return r["question_id"]


def _count_dag_violations(server, include_resolves=False):
    """Run the startup scan SQL; optionally include resolves in the NOT IN set."""
    import engram_core as core
    conn = server._get_db()
    try:
        if include_resolves:
            # Exclude resolves from the exempt set to see what the bug looked like.
            exempt = frozenset(core.DAG_EXEMPT_RELATIONS - {"resolves"})
        else:
            exempt = core.DAG_EXEMPT_RELATIONS
        placeholders = ",".join("?" * len(exempt))
        rows = conn.execute(
            f"""SELECT e.source_id, e.target_id, e.relation,
                       s.created_at as src_time, t.created_at as tgt_time
                FROM edges e
                JOIN nodes s ON e.source_id = s.id
                JOIN nodes t ON e.target_id = t.id
                WHERE s.created_at < t.created_at
                  AND e.relation NOT IN ({placeholders})""",
            tuple(exempt),
        ).fetchall()
        return rows
    finally:
        conn.close()


# ── 1. Constant-membership guards ────────────────────────────────────────────

def test_resolves_in_dag_exempt_relations():
    """resolves must be in DAG_EXEMPT_RELATIONS (issue #1076 fix)."""
    import engram_core as core
    assert "resolves" in core.DAG_EXEMPT_RELATIONS, (
        "resolves is not in DAG_EXEMPT_RELATIONS — fix not applied. "
        "Set temporal_causal=False for 'resolves' in EDGE_CLASSIFICATIONS."
    )


def test_tensions_in_dag_exempt_relations():
    """tensions must be in DAG_EXEMPT_RELATIONS (issue #1076, issue #1076, three-tier edge taxonomy).

    tensions is in the relational tier alongside contradicts (already exempt).
    Symmetric/cross-temporal: no causal direction, so source may legitimately
    pre-date target.
    """
    import engram_core as core
    assert "tensions" in core.DAG_EXEMPT_RELATIONS, (
        "tensions is not in DAG_EXEMPT_RELATIONS — exemption not applied. "
        "Set temporal_causal=False for 'tensions' in EDGE_CLASSIFICATIONS."
    )


def test_serves_in_dag_exempt_relations():
    """serves must be in DAG_EXEMPT_RELATIONS (issue #1076, issue #1076, three-tier edge taxonomy).

    serves is in the classification tier alongside instantiates (already
    exempt): a goal can be articulated after the node that serves it, so
    serves.source legitimately pre-dates serves.target.
    """
    import engram_core as core
    assert "serves" in core.DAG_EXEMPT_RELATIONS, (
        "serves is not in DAG_EXEMPT_RELATIONS — exemption not applied. "
        "Set temporal_causal=False for 'serves' in EDGE_CLASSIFICATIONS."
    )


# ── 2. Startup scan: no violations for back-reference resolves edge ────────────

def test_dag_scan_no_violation_for_back_reference_resolves():
    """A resolver older than the question must not appear as a DAG violation.

    Setup: obs created first (T1), question created after (T2 > T1).
    Edge: obs --resolves--> question (source.created_at < target.created_at).
    Expected: startup scan returns 0 violations.

    Mutation note: with temporal_causal=True for resolves, the row WOULD appear
    (verified by test 3 below). This test catches any regression that
    re-enables the check.
    """
    server = fresh_server()
    obs_id = _add_obs(server)          # resolver — created FIRST
    time.sleep(0.01)                   # ensure strictly older timestamp
    qu_id = _add_question(server)      # question — created AFTER

    # Wire the back-reference resolves edge.
    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "resolving_node_id": obs_id,
        "target_id": qu_id,
    })))
    assert "error" not in r, f"engram_resolve failed: {r}"

    # Startup scan: resolves is exempt, so no violation should be reported.
    violations = _count_dag_violations(server, include_resolves=False)
    resolves_violations = [v for v in violations if v["relation"] == "resolves"]
    assert resolves_violations == [], (
        f"Startup scan reported DAG violations for resolves edges: {resolves_violations}. "
        "This is the #1076 false-positive — check temporal_causal=False for 'resolves' "
        "in EDGE_CLASSIFICATIONS."
    )


# ── 3. Mutation guard: the edge WOULD be a violation without the exemption ─────

def test_dag_scan_would_flag_resolves_without_exemption():
    """Confirm the mutation test is meaningful: without the exemption the edge
    is detectable as a temporal-DAG violation.

    This test verifies that the setup in test 2 actually exercises the guard.
    If this test FAILS (finds 0 violations even when resolves is not exempt),
    the fixture is broken and test 2's pass is vacuous.
    """
    server = fresh_server()
    obs_id = _add_obs(server)
    time.sleep(0.01)
    qu_id = _add_question(server)

    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "resolving_node_id": obs_id,
        "target_id": qu_id,
    })))
    assert "error" not in r, f"engram_resolve failed: {r}"

    # Without the exemption: the edge appears as a violation.
    violations = _count_dag_violations(server, include_resolves=True)
    resolves_violations = [v for v in violations if v["relation"] == "resolves"]
    assert len(resolves_violations) >= 1, (
        "Expected the back-reference resolves edge to appear as a temporal-DAG "
        "candidate when resolves is NOT exempt — mutation guard is broken."
    )
