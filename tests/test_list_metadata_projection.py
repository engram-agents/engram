"""engram_list `metadata.<key>` filter + projection; support_lost in inspect recall (#3402).

Contract (written before the implementation):
  F1 filter metadata.support_lost eq 1 -> exactly flagged nodes
  F2 malformed-JSON metadata row does not break the query
  F3 invalid key -> error naming the pattern, no SQL error, no rows
  F4 is_null / is_not_null / scalar + list ops work on metadata.<key>
  P1 fields ["id","metadata"] -> raw metadata string (== inspect deep)
  P2 fields ["id","metadata.support_lost"] -> extracted value; absent key -> null
  P3 invalid projected key -> same error as F3
  R0 unknown real column still errors; metadata still not a filterable column
  I1 inspect recall promotes support_lost when truthy; I2 deep keeps metadata
"""

import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KEY_PATTERN = "^[A-Za-z_][A-Za-z0-9_]{0,63}$"


@pytest.fixture
def env():
    d = tempfile.mkdtemp(prefix="engram_test_list_meta_")
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(d)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    ids = {}
    topics = {
        "flagged": "Alpha granite quarry output rose in spring.",
        "falsey": "Bravo kelp harvest yields fell in winter.",
        "absent": "Charlie lighthouse keepers rotate every month.",
        "malformed": "Delta glacier retreat accelerated near the fjord.",
    }
    for name, claim in topics.items():
        r = json.loads(server.engram_add_observation(payload_json=json.dumps({
            "url": f"https://example.com/{name}",
            "title": f"Src {name}",
            "quoted_text": f"{name} verbatim quote text.",
            "interpretation": f"{name} interpretation text here.",
            "claim": claim,
            "quote_type": "hard_data",
        })))
        assert "observation_id" in r, r
        ids[name] = r["observation_id"]
    from engram_core import _get_db, _METADATA_GUARD_TRIGGER_NAMES
    conn = _get_db()
    # #3425: the write guard now refuses a non-JSON blob. These cells test the
    # READ path against a legacy blob that slipped past the guard (a restore of
    # an old dump, a hand edit), so the fixture drops the guard to plant one.
    for _guard in _METADATA_GUARD_TRIGGER_NAMES:
        conn.execute(f"DROP TRIGGER IF EXISTS {_guard}")
    conn.commit()

    def setmeta(nid, text):
        conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (text, nid))
        conn.commit()

    setmeta(ids["flagged"], json.dumps({"support_lost": True, "tag": "red"}))
    setmeta(ids["falsey"], json.dumps({"support_lost": False, "tag": "blue"}))
    setmeta(ids["absent"], json.dumps({"other": 1}))
    setmeta(ids["malformed"], "{not json")
    yield server, ids, setmeta
    shutil.rmtree(d, ignore_errors=True)


def _list(server, filters=None, fields=None):
    p = {"unlimited": True}
    if filters is not None:
        p["filters_json"] = json.dumps(filters)
    if fields is not None:
        p["fields_json"] = json.dumps(fields)
    return json.loads(server.engram_list(payload_json=json.dumps(p)))


def _idset(r):
    return {n["id"] for n in r["nodes"]}


def test_F1_filter_support_lost_eq_1(env):
    server, ids, _ = env
    r = _list(server, {"field": "metadata.support_lost", "op": "eq", "value": 1})
    assert r["status"] == "ok", r
    assert _idset(r) == {ids["flagged"]}
    assert r["total_matched"] == len(r["nodes"]) == 1


def test_F2_malformed_metadata_does_not_break_query(env):
    server, ids, _ = env
    r = _list(server, {"field": "metadata.support_lost", "op": "eq", "value": 1})
    assert r["status"] == "ok", r
    assert ids["flagged"] in _idset(r)
    # malformed row is treated as key-absent
    r2 = _list(server, {"field": "metadata.support_lost", "op": "is_null"})
    assert r2["status"] == "ok", r2
    assert ids["malformed"] in _idset(r2)


@pytest.mark.parametrize("bad", [
    "", "a.b", 'a"b', "$.x", "x;DROP", "x" * 65, "1abc", "a b", "a'b",
])
def test_F3_invalid_key_rejected(env, bad):
    server, ids, _ = env
    r = _list(server, {"field": f"metadata.{bad}", "op": "eq", "value": 1})
    assert r["status"] == "error", r
    assert KEY_PATTERN in r["error"], r
    assert "nodes" not in r
    low = r["error"].lower()
    assert "sqlite" not in low and "syntax error" not in low and "operationalerror" not in low


def test_F3_max_length_key_ok(env):
    server, ids, _ = env
    r = _list(server, {"field": "metadata." + "x" * 64, "op": "is_null"})
    assert r["status"] == "ok", r


def test_F4_null_and_scalar_ops(env):
    server, ids, _ = env
    absent = _list(server, {"field": "metadata.support_lost", "op": "is_null"})
    assert ids["absent"] in _idset(absent) and ids["flagged"] not in _idset(absent)
    assert ids["falsey"] not in _idset(absent)
    present = _list(server, {"field": "metadata.support_lost", "op": "is_not_null"})
    assert _idset(present) == {ids["flagged"], ids["falsey"]}
    eq0 = _list(server, {"field": "metadata.support_lost", "op": "eq", "value": 0})
    assert _idset(eq0) == {ids["falsey"]}
    ne1 = _list(server, {"field": "metadata.support_lost", "op": "ne", "value": 1})
    assert ids["flagged"] not in _idset(ne1) and ids["falsey"] in _idset(ne1)
    gt0 = _list(server, {"field": "metadata.support_lost", "op": "gt", "value": 0})
    assert _idset(gt0) == {ids["flagged"]}
    con = _list(server, {"field": "metadata.tag", "op": "contains", "value": "RE"})
    assert _idset(con) == {ids["flagged"]}
    inn = _list(server, {"field": "metadata.tag", "op": "in", "value": ["red", "blue"]})
    assert _idset(inn) == {ids["flagged"], ids["falsey"]}
    # a string value compares as a string
    s = _list(server, {"field": "metadata.tag", "op": "eq", "value": "blue"})
    assert _idset(s) == {ids["falsey"]}


def test_F4_composes_with_column_filters(env):
    server, ids, _ = env
    r = _list(server, [
        {"field": "metadata.support_lost", "op": "eq", "value": 1},
        {"field": "type", "op": "eq", "value": "observation_factual"},
    ])
    assert _idset(r) == {ids["flagged"]}


def test_P1_projection_raw_metadata_equals_inspect_deep(env):
    server, ids, _ = env
    r = _list(server, fields=["id", "metadata"])
    assert r["status"] == "ok", r
    row = {n["id"]: n for n in r["nodes"]}[ids["flagged"]]
    deep = json.loads(server.engram_inspect(payload_json=json.dumps(
        {"node_id": ids["flagged"], "view": "deep"})))
    assert row["metadata"] == deep["node"]["metadata"]
    assert isinstance(row["metadata"], str)
    # malformed row: raw text returned untouched
    bad = {n["id"]: n for n in r["nodes"]}[ids["malformed"]]
    assert bad["metadata"] == "{not json"


def test_P2_projection_key_value_absent_is_null(env):
    server, ids, _ = env
    r = _list(server, fields=["id", "metadata.support_lost", "metadata.tag"])
    assert r["status"] == "ok", r
    rows = {n["id"]: n for n in r["nodes"]}
    assert rows[ids["flagged"]]["metadata.support_lost"] == 1
    assert rows[ids["falsey"]]["metadata.support_lost"] == 0
    assert rows[ids["flagged"]]["metadata.tag"] == "red"
    # absent / malformed: key present in the row, value null
    for name in ("absent", "malformed"):
        assert "metadata.support_lost" in rows[ids[name]], rows[ids[name]]
        assert rows[ids[name]]["metadata.support_lost"] is None


@pytest.mark.parametrize("bad", ["", "a.b", 'a"b', "$.x", "x;DROP", "x" * 65, "1abc"])
def test_P3_invalid_projected_key_rejected(env, bad):
    server, ids, _ = env
    r = _list(server, fields=["id", f"metadata.{bad}"])
    assert r["status"] == "error", r
    assert KEY_PATTERN in r["error"], r
    assert "nodes" not in r


def test_R0_unknown_column_still_errors_and_metadata_not_filterable(env):
    server, ids, _ = env
    r = _list(server, {"field": "nonesuch", "op": "eq", "value": 1})
    assert r["status"] == "error" and "unknown field 'nonesuch'" in r["error"]
    r = _list(server, {"field": "metadata", "op": "eq", "value": "x"})
    assert r["status"] == "error" and "unknown field 'metadata'" in r["error"]
    r = _list(server, fields=["id", "nonesuch"])
    assert r["status"] == "error" and "unknown field 'nonesuch'" in r["error"]
    from engram_filter import ALLOWED_NODE_COLUMNS
    assert "metadata" not in ALLOWED_NODE_COLUMNS


def test_F4_not_in_param_order_with_metadata_key(env):
    """not_in repeats the expression around the list; bound params must line up."""
    server, ids, _ = env
    r = _list(server, {"field": "metadata.tag", "op": "not_in", "value": ["red"]})
    assert r["status"] == "ok", r
    assert ids["flagged"] not in _idset(r)
    assert ids["falsey"] in _idset(r) and ids["absent"] in _idset(r)


def test_filter_and_projection_params_line_up(env):
    server, ids, _ = env
    r = _list(server,
              {"field": "metadata.tag", "op": "eq", "value": "blue"},
              fields=["id", "metadata.support_lost", "metadata.tag"])
    assert r["status"] == "ok", r
    assert [n["id"] for n in r["nodes"]] == [ids["falsey"]]
    assert r["nodes"][0]["metadata.tag"] == "blue"


def _inspect(server, nid, view):
    return json.loads(server.engram_inspect(payload_json=json.dumps(
        {"node_id": nid, "view": view})))


def test_I1_recall_view_promotes_support_lost(env):
    server, ids, _ = env
    r = _inspect(server, ids["flagged"], "recall")
    assert r["node"].get("support_lost") is True, r["node"]
    for name in ("absent", "falsey", "malformed"):
        assert "support_lost" not in _inspect(server, ids[name], "recall")["node"]


def test_I2_deep_view_still_includes_metadata(env):
    server, ids, _ = env
    r = _inspect(server, ids["flagged"], "deep")
    assert "support_lost" in json.loads(r["node"]["metadata"])


NON_OBJECT_BLOBS = ["[1,2]", '"str"', "5", "null"]


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_I3_recall_view_tolerates_non_object_metadata(env, blob):
    server, ids, setmeta = env
    setmeta(ids["absent"], blob)
    r = _inspect(server, ids["absent"], "recall")
    assert "error" not in r, r
    assert "support_lost" not in r["node"]
    assert "metadata" not in r["node"]


def test_F5_non_object_blobs_in_list_path(env):
    server, ids, setmeta = env
    names = ["flagged", "falsey", "absent", "malformed"]
    for name, blob in zip(names, NON_OBJECT_BLOBS):
        setmeta(ids[name], blob)
    r = _list(server, {"field": "metadata.support_lost", "op": "is_null"},
              fields=["id", "metadata.support_lost"])
    assert r["status"] == "ok", r
    mine = {ids[n] for n in names}
    assert mine <= _idset(r)
    for n in r["nodes"]:
        if n["id"] in mine:
            assert n["metadata.support_lost"] is None
    assert r["total_matched"] == len(r["nodes"])


def test_F6_filter_value_true_through_json_payload(env):
    server, ids, _ = env
    # real JSON path: Python True serializes to JSON true
    assert '"value": true' in json.dumps(
        {"field": "metadata.support_lost", "op": "eq", "value": True})
    r = _list(server, {"field": "metadata.support_lost", "op": "eq", "value": True})
    assert r["status"] == "ok", r
    assert _idset(r) == {ids["flagged"]}
    assert r["total_matched"] == len(r["nodes"]) == 1
