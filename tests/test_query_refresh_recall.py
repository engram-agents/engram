"""Contract tests for engram_query's `refresh_recall` flag (part of #3362).

Contract:
  1. Default (flag absent): surfaced nodes are re-stamped -- recall_count + 1,
     recall_turn == current turn.
  2. refresh_recall=false: recall_count / recall_turn / importance_score are
     byte-identical before/after, and result ids + order equal the default
     call's (the flag changes no ranking). The flagged call runs FIRST so the
     default call's refresh cannot mask it.
  3. Response shape: top-level `refresh_recall: false` appears only in the
     flagged response (tiered and return_debug shapes); absent by default.
  4. Unknown-field error still lists the allowed fields, now with refresh_recall.

Precedent: tests/test_inspect_view_modes.py::test_dream_mode_skips_recall_refresh.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_query_refresh_recall_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

_SENTINEL_TURN = -7  # never equals a real current_turn


def fresh_server():
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


def _add_observation(server, claim):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/test",
        "title": "Test Source",
        "quoted_text": claim[:80],
        "interpretation": "Test.",
        "claim": claim,
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r, f"add_observation failed: {r}"
    return r["observation_id"]


def _build():
    """Fresh server + three observations that all match 'zebra'; recall_turn
    reset to a sentinel so a re-stamp is unambiguous."""
    server = fresh_server()
    # Three unrelated topics, all matching "zebra" at different strengths, so
    # the ranking order is non-trivial (not three near-identical ties).
    claims = [
        "Volcanic basalt formations are mapped by geologists; one old field guide mentions a zebra once.",
        "Zebra zebra zebra herds migrate across the savanna, and zebra foals stand within minutes.",
        "Giraffe habitats overlap with a zebra population in several dry regions of Africa.",
    ]
    ids = [_add_observation(server, c) for c in claims]
    conn = server._get_db()
    try:
        for nid in ids:
            conn.execute(
                "UPDATE nodes SET recall_turn = ?, recall_count = 0 WHERE id = ?",
                (_SENTINEL_TURN, nid),
            )
        conn.commit()
    finally:
        conn.close()
    return server, ids


def _row(server, nid):
    conn = server._get_db()
    try:
        r = conn.execute(
            "SELECT importance_score, recall_turn, recall_count FROM nodes WHERE id = ?",
            (nid,),
        ).fetchone()
        return (r["importance_score"], r["recall_turn"], r["recall_count"])
    finally:
        conn.close()


def _query(server, **extra):
    payload = {"query": "zebra", **extra}
    out = json.loads(server.engram_query(payload_json=json.dumps(payload)))
    assert "error" not in out, f"unexpected error: {out}"
    return out


def _result_ids(out):
    return [r["id"] for r in out["results"]]


def test_default_refreshes_recall():
    server, ids = _build()
    out = _query(server)
    x = _result_ids(out)[0]
    assert x in ids
    turn = server._get_memory_config().get("current_turn", 0)
    _score, recall_turn, recall_count = _row(server, x)
    assert recall_count == 1
    assert recall_turn == turn


def test_false_changes_nothing_and_keeps_ranking():
    server, ids = _build()
    before = {nid: _row(server, nid) for nid in ids}

    flagged = _query(server, refresh_recall=False)  # FIRST, so default can't mask
    flagged_ids = _result_ids(flagged)
    assert set(flagged_ids) == set(ids)
    assert {nid: _row(server, nid) for nid in ids} == before

    default = _query(server)
    assert _result_ids(default) == flagged_ids  # same ids, same order
    assert all(_row(server, nid)[2] == 1 for nid in ids)  # default did refresh


def test_response_shape_key_only_when_false():
    server, _ids = _build()
    for extra in ({}, {"return_debug": True}):
        default = _query(server, **extra)
        assert "refresh_recall" not in default
        flagged = _query(server, refresh_recall=False, **extra)
        assert flagged["refresh_recall"] is False
        # explicit True is the default shape too
        explicit = _query(server, refresh_recall=True, **extra)
        assert "refresh_recall" not in explicit


def test_unknown_field_error_lists_refresh_recall():
    server, _ids = _build()
    out = json.loads(server.engram_query(
        payload_json=json.dumps({"query": "zebra", "bogus_field": 1})
    ))
    assert "error" in out
    assert "Unknown fields" in out["error"]
    assert "refresh_recall" in out["error"]


def _person_meta(server, pid):
    conn = server._get_db()
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (pid,)).fetchone()
        return row["metadata"]  # raw string: byte-identical comparison
    finally:
        conn.close()


def _ledger():
    import engram_query
    return engram_query._read_person_moment_ledger()


def _person_with_moment():
    """Person node with a special moment, matching the query 'zebra' so it
    lands in the Tier-1 top-k (summary_top_k default 3) of a tiered query."""
    server = fresh_server()
    anchor = _add_observation(server, "Anchor claim about zebra keepers.")
    pid = json.loads(server.engram_add_person(payload_json=json.dumps(
        {"name": "Zebra Keeper"}
    )))["person_id"]
    r = json.loads(server.engram_add_special_moment(payload_json=json.dumps({
        "person_id": pid, "description": "Fed a zebra by hand.", "node_id": anchor,
    })))
    assert "error" not in r, r
    return server, pid


def _person_entry(out, pid):
    return next(e for e in out["results"] if e["id"] == pid)


def test_false_skips_tiered_person_moment_write():
    server, pid = _person_with_moment()
    meta_before = _person_meta(server, pid)
    ledger_before = _ledger()

    out = _query(server, refresh_recall=False)
    assert pid in _result_ids(out)
    assert "special_moment" not in _person_entry(out, pid)
    assert _person_meta(server, pid) == meta_before  # cursor + all metadata untouched
    assert _ledger() == ledger_before  # cooldown ledger untouched


def test_default_runs_tiered_person_moment_render():
    """Positive control: the default path renders the moment and writes, so
    the test above discriminates."""
    server, pid = _person_with_moment()
    meta_before = _person_meta(server, pid)
    ledger_before = _ledger()

    out = _query(server)
    assert _person_entry(out, pid).get("special_moment") == "Fed a zebra by hand."
    assert _person_meta(server, pid) != meta_before
    assert json.loads(_person_meta(server, pid)).get("special_moment_cursor") == 1
    assert _ledger() != ledger_before
