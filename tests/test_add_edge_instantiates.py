"""Tests for the `instantiates` relation (#530, PR 3 of the #510 cites-overload epic).

`instantiates` is the achievement-shaped relevance marker: source (claim-bearing
node) realizes / implements / is-an-instance-of target (goal, cornerstone,
definition, axiom). Designed in forum thread #44 (three-graph convergence,
2026-06-07); Lei confirmed c1 on the issue. First artifact of the
cycle/epistemic-foundations-v1 branch.

The three-way boundary under test (mechanical gate, not documentation):
  exemplifies  — incident → lesson ONLY (engine-coupled to the tripwire cache)
  serves       — intent-shaped contribution toward a goal
  instantiates — achievement-shaped realization of a principle-family node

Coverage:
  1.  Happy path — observation instantiates goal → created.
  2.  Happy path — observation instantiates axiom (the axiom-grounding case,
      Luria's use case 3 / Ariadne n=2) → created.
  3.  Lesson target REJECTED with a pointer to `exemplifies` (the boundary gate).
      Mutation note: if the gate were removed, this add would succeed and the
      assertion on the error text would fail — the gate is load-bearing.
  4.  Non-whitelist target (observation) rejected, error names the allowed
      target types and redirects serves/exemplifies.
  5.  Non-claim-bearing source (person) rejected.
  6.  Idempotency — second identical add returns no_op_already_exists.
  7.  DAG-exemption — source OLDER than target succeeds (a goal articulated
      AFTER the work that realizes it is the gl-0008-shaped first customer).
      Mutation note: if instantiates had temporal_causal=True, this test would fail
      with the DAG-time invariant error — the exemption is deliberate.
  8.  Removability — engram_remove_edge removes an instantiates edge
      (EDGE_CLASSIFICATIONS removable=True drives the remove whitelist).
"""

import atexit
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_instantiates_")
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


def _add_obs(server, claim_suffix="x"):
    o = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/instantiates-test",
        "title": "Instantiates Test Obs",
        "quoted_text": "Practice produced a measurable outcome.",
        "interpretation": "The practice run is an instance of the principle holding.",
        "claim": f"Practice outcome instance {claim_suffix}.",
        "quote_type": "hard_data",
    })))
    assert "error" not in o, o
    return o["observation_id"]


def _add_goal(server):
    g = json.loads(server.engram_add_goal(payload_json=json.dumps({
        "claim": "Memory value is measured by reasoning change, not accumulation.",
        "motivation": "Wire implementations to this goal as they land.",
    })))
    assert "error" not in g, g
    return g.get("goal_id") or g.get("node_id")


def _add_axiom(server):
    a = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": "Practice produces true knowledge; practice is the test of truth.",
        "basis": "Adopted foundational commitment for instantiates tests.",
    })))
    assert "error" not in a, a
    return a.get("axiom_id") or a.get("node_id")


def _add_lesson(server, incident_id):
    ls = json.loads(server.engram_add_lesson(payload_json=json.dumps({
        "claim": "Test lesson: verify the axis the check exercised.",
        "incident_ids": incident_id,
        "scaffolding_nudge": "Name the axis; run a differently-oriented check.",
        "logical_chain": "Single incident grounds the proposed lesson for boundary testing.",
    })))
    assert "error" not in ls, ls
    return ls["lesson_id"]


def _add_person(server):
    p = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "Boundary Probe",
        "role": "non-claim-bearing source for gate test",
    })))
    assert "error" not in p, p
    return p.get("person_id") or p.get("node_id")


# ── 1. Happy path: observation instantiates goal ──────────────────────────


def test_instantiates_goal_created():
    """A claim-bearing source realizing a goal → status='created'."""
    server = fresh_server()
    goal_id = _add_goal(server)
    obs_id = _add_obs(server)
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": goal_id,
        "relation": "instantiates",
    })))
    assert "error" not in r, r
    assert r["status"] == "created", r
    assert r["relation"] == "instantiates"

    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT id FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
            (obs_id, goal_id, "instantiates"),
        ).fetchone()
        assert row is not None, "instantiates edge must be persisted"
    finally:
        conn.close()


# ── 2. Happy path: axiom grounding from a practice observation ────────────


def test_instantiates_axiom_grounding():
    """ob_X instantiates ax_Y — the practice observation IS an instance of the
    axiom holding (not serves: wrong direction; not derives: axioms are terminal)."""
    server = fresh_server()
    ax_id = _add_axiom(server)
    obs_id = _add_obs(server, "axiom-grounding")
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": ax_id,
        "relation": "instantiates",
    })))
    assert "error" not in r, r
    assert r["status"] == "created", r


# ── 3. Lesson target rejected with exemplifies pointer (the boundary gate) ─


def test_instantiates_lesson_target_rejected_with_exemplifies_pointer():
    """Lesson targets are mechanically rejected — incidents go via exemplifies.

    Mutation note: removing the lesson gate in _add_edge_impl would make this
    add SUCCEED, failing both assertions below — the gate is load-bearing, the
    test is not vacuously green.
    """
    server = fresh_server()
    incident_id = _add_obs(server, "incident")
    lesson_id = _add_lesson(server, incident_id)
    other_obs = _add_obs(server, "would-be-instance")
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": other_obs,
        "target_id": lesson_id,
        "relation": "instantiates",
    })))
    assert "error" in r, f"lesson target must be rejected, got: {r}"
    assert "exemplifies" in r["error"], (
        f"rejection must point to the exemplifies channel: {r['error']}"
    )


# ── 4. Non-whitelist target rejected, error names allowed types ───────────


def test_instantiates_observation_target_rejected():
    """An observation is not a principle-family target — error enumerates the
    whitelist and redirects serves/exemplifies."""
    server = fresh_server()
    obs_a = _add_obs(server, "a")
    obs_b = _add_obs(server, "b")
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_b,
        "target_id": obs_a,
        "relation": "instantiates",
    })))
    assert "error" in r, f"observation target must be rejected, got: {r}"
    for expected in ("goal", "cornerstone", "definition", "axiom"):
        assert expected in r["error"], (
            f"error must enumerate allowed target types (missing {expected}): "
            f"{r['error']}"
        )


# ── 5. Non-claim-bearing source rejected ───────────────────────────────────


def test_instantiates_person_source_rejected():
    """Person nodes are not claim-bearing — they cannot realize a principle."""
    server = fresh_server()
    goal_id = _add_goal(server)
    person_id = _add_person(server)
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": person_id,
        "target_id": goal_id,
        "relation": "instantiates",
    })))
    assert "error" in r, f"person source must be rejected, got: {r}"
    assert "claim-bearing" in r["error"], r["error"]


# ── 6. Idempotency ─────────────────────────────────────────────────────────


def test_instantiates_idempotent_no_op():
    """Second identical add returns no_op_already_exists; single edge in DB."""
    server = fresh_server()
    goal_id = _add_goal(server)
    obs_id = _add_obs(server)
    payload = json.dumps({
        "source_id": obs_id,
        "target_id": goal_id,
        "relation": "instantiates",
    })
    r1 = json.loads(server.engram_add_edge(payload_json=payload))
    assert r1["status"] == "created", r1
    r2 = json.loads(server.engram_add_edge(payload_json=payload))
    assert r2["status"] == "no_op_already_exists", r2

    conn = server._get_db()
    try:
        n = conn.execute(
            "SELECT count(*) FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
            (obs_id, goal_id, "instantiates"),
        ).fetchone()[0]
        assert n == 1, f"exactly one edge expected, got {n}"
    finally:
        conn.close()


# ── 7. DAG-exemption: source older than target succeeds ───────────────────


def test_instantiates_dag_exempt_source_older_than_target():
    """A goal articulated AFTER the work that realizes it is wirable — the
    gl-0008-shaped first customer.

    Mutation note: if instantiates had temporal_causal=True in EDGE_CLASSIFICATIONS,
    this add would fail with the DAG-time invariant error — the exemption is a
    deliberate design property (realization is not a temporal dependency), and
    this test pins it.
    """
    server = fresh_server()
    obs_id = _add_obs(server, "older-work")  # source created FIRST
    time.sleep(0.01)  # ensure strictly increasing created_at
    goal_id = _add_goal(server)              # target created AFTER source
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": goal_id,
        "relation": "instantiates",
    })))
    assert "error" not in r, (
        f"older-source instantiates must succeed (DAG-exempt): {r}"
    )
    assert r["status"] == "created", r


# ── 8. Removability via engram_remove_edge ─────────────────────────────────


def test_instantiates_removable():
    """instantiates is removable (EDGE_CLASSIFICATIONS removable=True drives
    the remove whitelist) — correction tool works for over-applied wiring."""
    server = fresh_server()
    goal_id = _add_goal(server)
    obs_id = _add_obs(server)
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": goal_id,
        "relation": "instantiates",
    })))
    assert r["status"] == "created", r

    rm = json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": obs_id,
        "target_id": goal_id,
        "relation": "instantiates",
    })))
    assert "error" not in rm, rm

    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT id FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
            (obs_id, goal_id, "instantiates"),
        ).fetchone()
        assert row is None, "edge must be gone after remove"
    finally:
        conn.close()
