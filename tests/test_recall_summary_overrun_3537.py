"""Write-response overrun line for recall_summary (#3537).

Contract (stated before the code):
  * A recall_summary whose .strip()'d length is 141..279 is soft-accepted
    (needs_tightening=1, unchanged). The success response of every write
    tool that takes recall_summary ALSO carries the key
    ``recall_summary_overrun`` whose value is exactly
    ``recall_summary <N> chars > <TARGET> target (soft-accepted, flagged
    needs_tightening)``; N is the stripped length, TARGET is read from
    RECALL_SUMMARY_TARGET (never hard-coded).
  * Absent/blank, 1..140 and exactly 140: the key is ABSENT (not null, not "").
  * >= 280 stays the existing hard reject: error, no node, no new key.
  * Nothing blocks; thresholds are unchanged.

Fixture pattern: same fresh_server() shape as test_recall_summary_write_time.py.
"""

from __future__ import annotations

import atexit
import errno
import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Every fresh_server() call allocates a NEW dir (#4292): nothing is removed
# before use, so a failed removal of an old dir cannot fail a cell, and a
# leftover file in an old dir cannot reach the next store.
TEST_DIR = None
_DIRS: list = []


def _cleanup_dirs():
    for p in list(_DIRS):
        shutil.rmtree(p, ignore_errors=True)


atexit.register(_cleanup_dirs)

KEY = "recall_summary_overrun"


def fresh_server():
    global TEST_DIR
    TEST_DIR = tempfile.mkdtemp(prefix="engram_test_recall_summary_overrun_3537_")
    _DIRS.append(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    return server


def _expected(n: int, target: int = 140) -> str:
    return (
        f"recall_summary {n} chars > {target} target "
        f"(soft-accepted, flagged needs_tightening)"
    )


def _node_count(srv) -> int:
    conn = srv._get_db()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
    finally:
        conn.close()


def _row(srv, node_id: str):
    conn = srv._get_db()
    try:
        return conn.execute(
            "SELECT recall_summary, needs_tightening, summary_missing "
            "FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
    finally:
        conn.close()


def _obs_payload(claim: str, **extra) -> dict:
    payload = {
        "quoted_text": "test quote",
        "interpretation": "test interpretation",
        "claim": claim,
        "quote_type": "personal_communication",
        "url": "https://example.com/test",
        "title": "Test",
    }
    payload.update(extra)
    return payload


def _add_obs(srv, claim: str, **extra) -> dict:
    return json.loads(
        srv.engram_add_observation(payload_json=json.dumps(_obs_payload(claim, **extra)))
    )


# --- one writer per tool: (srv, summary) -> (result_dict, id_key) -------------

def _w_observation(srv, summary):
    return _add_obs(srv, "overrun observation", recall_summary=summary), "observation_id"


def _w_definition(srv, summary):
    r = json.loads(srv.engram_add_definition(payload_json=json.dumps({
        "term": "overrun-term",
        "definition": "A definition used by the overrun test.",
        "recall_summary": summary,
    })))
    return r, "definition_id"


def _w_conjecture(srv, summary):
    r = json.loads(srv.engram_add_conjecture(payload_json=json.dumps({
        "claim": "overrun conjecture",
        "basis": "testing the overrun line",
        "recall_summary": summary,
    })))
    return r, "conjecture_id"


def _w_lesson(srv, summary):
    inc = _add_obs(srv, "overrun lesson incident")
    assert "error" not in inc, inc
    r = json.loads(srv.engram_add_lesson(payload_json=json.dumps({
        "claim": "overrun lesson",
        "incident_ids": inc["observation_id"],
        "scaffolding_nudge": "Do the thing.",
        "logical_chain": "One incident generalizes to this pattern.",
        "recall_summary": summary,
    })))
    return r, "lesson_id"


def _w_derive(srv, summary):
    prem = _add_obs(srv, "overrun derive premise")
    assert "error" not in prem, prem
    r = json.loads(srv.engram_derive(payload_json=json.dumps({
        "claim": "overrun derivation",
        "supporting_ids": prem["observation_id"],
        "logical_chain": "Premise directly supports this conclusion.",
        "reasoning_type": "authority_expert",
        "recall_summary": summary,
    })))
    return r, "derivation_id"


WRITERS = {
    "observation": _w_observation,
    "definition": _w_definition,
    "conjecture": _w_conjecture,
    "lesson": _w_lesson,
    "derive": _w_derive,
}


# --- 1. length boundaries (observation entry point) ---------------------------

@pytest.mark.parametrize("n", [1, 139, 140])
def test_no_key_at_or_below_target(n):
    srv = fresh_server()
    r, _ = _w_observation(srv, "s" * n)
    assert r.get("status") == "created", r
    assert KEY not in r, r


@pytest.mark.parametrize("n", [141, 200, 279])
def test_key_present_over_target(n):
    srv = fresh_server()
    r, _ = _w_observation(srv, "s" * n)
    assert r.get("status") == "created", r
    assert r[KEY] == _expected(n)


def test_omitted_summary_has_no_key():
    srv = fresh_server()
    r = _add_obs(srv, "overrun omitted")
    assert r.get("status") == "created", r
    assert KEY not in r


def test_blank_summary_has_no_key():
    srv = fresh_server()
    r, _ = _w_observation(srv, "   ")
    assert r.get("status") == "created", r
    assert KEY not in r


def test_hard_reject_at_280_no_key_no_node():
    srv = fresh_server()
    before = _node_count(srv)
    r, _ = _w_observation(srv, "s" * 280)
    assert "error" in r, r
    assert KEY not in r
    assert _node_count(srv) == before


# --- 2. every tool ------------------------------------------------------------

@pytest.mark.parametrize("tool", sorted(WRITERS))
def test_each_tool_150_chars_carries_key(tool):
    srv = fresh_server()
    r, _ = WRITERS[tool](srv, "t" * 150)
    assert "error" not in r, r
    assert r[KEY] == _expected(150)


@pytest.mark.parametrize("tool", sorted(WRITERS))
def test_each_tool_100_chars_has_no_key(tool):
    srv = fresh_server()
    r, _ = WRITERS[tool](srv, "t" * 100)
    assert "error" not in r, r
    assert KEY not in r


# --- 3. whitespace: stripped length decides -----------------------------------

def test_padding_does_not_trip_key():
    srv = fresh_server()
    r, _ = _w_observation(srv, "p" * 138 + " " * 10)
    assert r.get("status") == "created", r
    assert KEY not in r


def test_trailing_spaces_report_stripped_length():
    srv = fresh_server()
    r, _ = _w_observation(srv, "p" * 150 + " " * 7)
    assert r[KEY] == _expected(150)


# --- 4. wording reads RECALL_SUMMARY_TARGET -----------------------------------

def test_target_constant_is_read_not_hardcoded():
    srv = fresh_server()
    core = sys.modules["engram_core"]
    old = core.RECALL_SUMMARY_TARGET
    core.RECALL_SUMMARY_TARGET = 100
    try:
        r, _ = _w_observation(srv, "q" * 120)
    finally:
        core.RECALL_SUMMARY_TARGET = old
    assert "> 100 target" in r[KEY], r
    assert r[KEY] == _expected(120, target=100)


# --- 5. existing behaviour pinned ---------------------------------------------

def test_over_target_still_flags_needs_tightening_and_creates_node():
    srv = fresh_server()
    r, id_key = _w_observation(srv, "z" * 150)
    assert r.get("status") == "created", r
    row = _row(srv, r[id_key])
    assert row["needs_tightening"] == 1
    assert row["summary_missing"] == 0
    assert row["recall_summary"] == "z" * 150


# --- 6. batch: key arrives per item -------------------------------------------

def test_batch_items_carry_key_per_item():
    srv = fresh_server()
    observations = [
        {"quoted_text": "bq1 in the cited source", "interpretation": "bi1", "claim": "batch over",
         "quote_type": "personal_communication", "recall_summary": "b" * 150},
        {"quoted_text": "bq2 in the cited source", "interpretation": "bi2", "claim": "batch under",
         "quote_type": "personal_communication", "recall_summary": "b" * 100},
    ]
    r = json.loads(srv.engram_add_observation_batch(payload_json=json.dumps({
        "observations_json": json.dumps(observations),
        "url": "https://example.com/batch-overrun",
        "title": "Batch Overrun",
    })))
    assert r["created"] == 2, r
    res = sorted(r["results"], key=lambda x: x["index"])
    assert res[0][KEY] == _expected(150)
    assert KEY not in res[1]


# --- 7. fixture: fresh_server allocates a new temp dir per call (#4292) -------

def _data_dir(srv) -> str:
    return str(srv.core.DATA_DIR)


def _fake_rmtree_factory(real, fail_for=None):
    """rmtree stand-in: raises ENOTEMPTY (for every path, or only fail_for)
    unless the caller passed ignore_errors=True, in which case it swallows."""
    def fake(path, ignore_errors=False, *a, **kw):
        if fail_for is None or str(path) == str(fail_for):
            if ignore_errors:
                return None
            raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))
        return real(path, ignore_errors=ignore_errors, *a, **kw)
    return fake


def test_fixture_two_calls_use_two_distinct_dirs():
    srv1 = fresh_server()
    d1 = TEST_DIR
    srv2 = fresh_server()
    d2 = TEST_DIR
    assert d1 != d2
    assert os.path.isdir(d1) and os.path.isdir(d2)
    assert _data_dir(srv2) == d2


def test_fixture_survives_unremovable_predecessor(monkeypatch):
    monkeypatch.setattr(shutil, "rmtree", _fake_rmtree_factory(shutil.rmtree))
    srv = fresh_server()
    r, _ = _w_observation(srv, "x")
    assert r.get("status") == "created", r


def test_fixture_new_dir_has_no_stale_files():
    srv1 = fresh_server()
    baseline = _node_count(srv1)
    _w_observation(srv1, "x")  # dirty the first store too
    open(os.path.join(TEST_DIR, "stale-marker"), "w").close()
    srv2 = fresh_server()
    assert not os.path.exists(os.path.join(TEST_DIR, "stale-marker"))
    assert _node_count(srv2) == baseline


def test_fixture_cleanup_removes_every_dir_and_tolerates_failure(monkeypatch):
    fresh_server()
    fresh_server()
    mine = list(_DIRS)
    try:
        assert TEST_DIR in mine and len(mine) >= 2
        assert all(os.path.isdir(p) for p in mine)
        # The FIRST removal fails: a cleanup that gives up at the first
        # failure would leave every later dir behind.
        survivor = mine[0]
        monkeypatch.setattr(
            shutil, "rmtree", _fake_rmtree_factory(shutil.rmtree, fail_for=survivor)
        )
        _cleanup_dirs()  # must not raise although one removal fails
        assert os.path.isdir(survivor)  # proves the failure path was exercised
        assert not any(os.path.exists(p) for p in mine[1:])
        monkeypatch.undo()
        _cleanup_dirs()
        assert not any(os.path.exists(p) for p in mine)
    finally:
        monkeypatch.undo()
        for p in mine:
            shutil.rmtree(p, ignore_errors=True)


def test_fixture_cleanup_is_registered_with_atexit(monkeypatch):
    import importlib.util
    registered = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a, **k: registered.append(fn))
    spec = importlib.util.spec_from_file_location("_probe_3537", __file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._cleanup_dirs in registered
