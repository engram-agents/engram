"""#3782: engram_reflect must SET `metadata.support_lost` from current state,
not only clear it.

Contract (written before the implementation; cells R1-R8; R9 is the
unmodified-detector parity run named in the PR, not a test in this file):

  R1 unflagged lesson whose only `exemplifies` source is non-current ->
     after reflect: flagged, ONE `support_lost` edit row (trigger
     reflect_recompute), listed in `support_lost_marked`.
  R2 same for a cornerstone whose only `supported_by` target is non-current.
  R3 a cornerstone/lesson that NEVER had a support edge is never marked and
     gets no edit row.
  R4 flagged lesson with a live exemplar -> cleared (existing behaviour).
  R5 flagged lesson still at zero live support -> stays flagged, listed in
     `support_lost_nodes`, NOT in `support_lost_marked`.
  R6 idempotent: a second reflect marks/clears nothing, writes no edit rows.
  R7 non-current cornerstone/lesson is never touched.
  R8 one live + one dead exemplar -> not marked.

Support is killed with direct SQL (is_current=0), which is the very case in
the issue: support went to zero WITHOUT the transform-time detector running.
"""

import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def env():
    d = tempfile.mkdtemp(prefix="engram_test_reflect_sl_3782_")
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(d)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    conn = server._get_db()
    yield server, conn
    conn.close()
    shutil.rmtree(d, ignore_errors=True)


def _call(fn, **p):
    return json.loads(fn(payload_json=json.dumps(p)))


def _axiom(server, tag):
    r = _call(server.engram_add_axiom, claim=f"Fixture premise {tag} holds.",
              basis="test fixture basis")
    assert "error" not in r, r
    return r["axiom_id"]


def _lesson(server, incident_ids, tag="a"):
    r = _call(server.engram_add_lesson,
              claim=f"Lesson pattern {tag}.",
              incident_ids=",".join(incident_ids),
              logical_chain=f"Pattern derived from incidents {tag}.",
              scaffolding_nudge="Watch for this pattern.")
    assert "error" not in r, r
    return r["lesson_id"]


def _cornerstone(server, supporting_ids, tag="cs"):
    r = _call(server.engram_add_cornerstone, tag=tag, title=f"Cornerstone {tag}",
              new_frame=f"Principle for {tag}.",
              supporting_ids=",".join(supporting_ids))
    assert "error" not in r, r
    return r["cornerstone_id"]


def _kill(conn, nid):
    conn.execute("UPDATE nodes SET is_current = 0 WHERE id = ?", (nid,))
    conn.commit()


def _flag(conn, nid):
    conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?",
                 (json.dumps({"support_lost": True}), nid))
    conn.commit()


def _flagged(conn, nid):
    row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (nid,)).fetchone()
    return bool(json.loads(row["metadata"] or "{}").get("support_lost"))


def _rows(conn, nid, action):
    return conn.execute(
        "SELECT details FROM edit_history WHERE node_id = ? AND action = ?",
        (nid, action)).fetchall()


def _reflect(server):
    r = _call(server.engram_reflect)
    assert "error" not in r, r
    return r


def _ids(r, key):
    return [x["id"] for x in r.get(key, [])]


def _make(server, kind, tag):
    """Return (node_id, premise_id) for a node with ONE support edge."""
    ax = _axiom(server, tag)
    nid = _lesson(server, [ax], tag) if kind == "lesson" else _cornerstone(server, [ax], tag)
    return nid, ax


@pytest.mark.parametrize("kind", ["lesson", "cornerstone"])
def test_R1_R2_dead_only_support_gets_marked(env, kind):
    server, conn = env
    nid, ax = _make(server, kind, "r12")
    # Premise: the support edge exists, and the node is not yet flagged.
    assert conn.execute(
        "SELECT COUNT(*) c FROM edges WHERE source_id=? OR target_id=?",
        (nid, nid)).fetchone()["c"] >= 1
    assert not _flagged(conn, nid)
    _kill(conn, ax)
    r = _reflect(server)
    assert _flagged(conn, nid)
    rows = _rows(conn, nid, "support_lost")
    assert len(rows) == 1
    assert json.loads(rows[0]["details"])["trigger"] == "reflect_recompute"
    assert _ids(r, "support_lost_marked") == [nid]
    assert r["summary"]["support_lost_marked"] == 1
    # Marked this run is support-lost now: also in support_lost_nodes, its
    # count, and the recommended-actions block the dream acts on.
    assert _ids(r, "support_lost_nodes") == [nid]
    assert r["summary"]["support_lost_nodes"] == 1
    assert any("support-lost cornerstones/lessons" in a
               for a in r["recommended_actions"]), r["recommended_actions"]
    assert nid not in _ids(r, "support_lost_cleared")


@pytest.mark.parametrize("kind", ["lesson", "cornerstone"])
def test_R3_never_had_support_not_marked(env, kind):
    server, conn = env
    nid, _ax = _make(server, kind, "r3")
    conn.execute("DELETE FROM edges WHERE source_id = ? OR target_id = ?", (nid, nid))
    conn.commit()
    r = _reflect(server)
    assert not _flagged(conn, nid)
    assert _rows(conn, nid, "support_lost") == []
    assert _ids(r, "support_lost_marked") == []
    assert r["summary"]["support_lost_marked"] == 0


def test_R4_flagged_with_live_exemplar_cleared(env):
    server, conn = env
    nid, _ax = _make(server, "lesson", "r4")
    _flag(conn, nid)
    r = _reflect(server)
    assert not _flagged(conn, nid)
    assert _ids(r, "support_lost_cleared") == [nid]
    assert _ids(r, "support_lost_marked") == []
    assert len(_rows(conn, nid, "support_lost_cleared")) == 1


def test_R5_flagged_still_zero_stays_listed_not_marked(env):
    server, conn = env
    nid, ax = _make(server, "lesson", "r5")
    _flag(conn, nid)
    _kill(conn, ax)
    r = _reflect(server)
    assert _flagged(conn, nid)
    assert _ids(r, "support_lost_nodes") == [nid]
    assert _ids(r, "support_lost_marked") == []
    assert _rows(conn, nid, "support_lost") == []


@pytest.mark.parametrize("kind", ["lesson", "cornerstone"])
def test_R6_idempotent(env, kind):
    server, conn = env
    nid, ax = _make(server, kind, "r6")
    _kill(conn, ax)
    _reflect(server)
    before = conn.execute("SELECT COUNT(*) c FROM edit_history").fetchone()["c"]
    r2 = _reflect(server)
    after = conn.execute("SELECT COUNT(*) c FROM edit_history").fetchone()["c"]
    assert after == before
    assert _ids(r2, "support_lost_marked") == []
    assert _ids(r2, "support_lost_cleared") == []
    assert _ids(r2, "support_lost_nodes") == [nid]
    assert _flagged(conn, nid)


@pytest.mark.parametrize("kind", ["lesson", "cornerstone"])
def test_R7_non_current_never_touched(env, kind):
    server, conn = env
    nid, ax = _make(server, kind, "r7")
    _kill(conn, ax)
    _kill(conn, nid)
    r = _reflect(server)
    assert not _flagged(conn, nid)
    assert _rows(conn, nid, "support_lost") == []
    assert _ids(r, "support_lost_marked") == []


@pytest.mark.parametrize("kind", ["lesson", "cornerstone"])
def test_R8_one_live_one_dead_not_marked(env, kind):
    server, conn = env
    a, b = _axiom(server, "r8a"), _axiom(server, "r8b")
    nid = _lesson(server, [a, b], "r8") if kind == "lesson" else _cornerstone(server, [a, b], "r8")
    _kill(conn, a)
    r = _reflect(server)
    assert not _flagged(conn, nid)
    assert _rows(conn, nid, "support_lost") == []
    assert _ids(r, "support_lost_marked") == []


def _set_raw_meta(conn, nid, text):
    conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (text, nid))
    conn.commit()


# Parsed-flagged but missed by reflect's SQL LIKE '"support_lost": true'
# (no space after the colon / a truthy non-boolean).
_LIKE_MISS_BLOBS = ['{"support_lost":true}', '{"support_lost": 1}']


@pytest.mark.parametrize("blob", _LIKE_MISS_BLOBS)
def test_R10_parsed_flagged_like_miss_dead_support_not_marked(env, blob):
    server, conn = env
    nid, ax = _make(server, "lesson", "r10")
    _set_raw_meta(conn, nid, blob)
    _kill(conn, ax)
    r = _reflect(server)
    assert _ids(r, "support_lost_marked") == []
    assert _rows(conn, nid, "support_lost") == []
    assert _ids(r, "support_lost_nodes") == [nid]
    assert _flagged(conn, nid)


@pytest.mark.parametrize("blob", _LIKE_MISS_BLOBS)
def test_R11_parsed_flagged_like_miss_live_support_cleared(env, blob):
    server, conn = env
    nid, _ax = _make(server, "lesson", "r11")
    _set_raw_meta(conn, nid, blob)
    r = _reflect(server)
    assert _ids(r, "support_lost_cleared") == [nid]
    assert len(_rows(conn, nid, "support_lost_cleared")) == 1
    assert not _flagged(conn, nid)


def test_R12_parsed_flagged_like_miss_never_supported_still_listed(env):
    """Zero support edges ever + a parsed-flagged blob the LIKE misses: the
    node is already support-lost, so it must be LISTED, not skipped by the
    never-had-support guard, and not re-marked."""
    server, conn = env
    nid, _ax = _make(server, "lesson", "r12b")
    conn.execute("DELETE FROM edges WHERE source_id = ? OR target_id = ?", (nid, nid))
    _set_raw_meta(conn, nid, '{"support_lost":true}')
    r = _reflect(server)
    assert _ids(r, "support_lost_nodes") == [nid]
    assert _ids(r, "support_lost_marked") == []
    assert _rows(conn, nid, "support_lost") == []
    assert _flagged(conn, nid)
