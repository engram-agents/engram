"""Metadata promotion tolerates valid-JSON-but-non-object blobs (#3413).

Contract (written before the implementation):
  C1 a person node whose nodes.metadata is `[1,2]`, `"str"`, `5` or `null`
     -> engram_inspect(view=recall) returns normally (no error); the recall
     node carries no pronouns / relation / special_moment / support_lost /
     metadata keys.
  C2 dict metadata is unchanged: person pronouns/relation, observation
     quote_verified and derivation warrant still promote in recall view.
  C3 class sweep: every sibling site that does `.get` on a parsed
     nodes.metadata blob tolerates a non-object blob end-to-end through the
     real MCP-level call (engram_query, engram_query_pattern, engram_inspect).
     "5" and "null" were already swallowed before #3413 at some sites; they
     stay in the matrices as pins.
"""

import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NON_OBJECT_BLOBS = ["[1,2]", '"str"', "5", "null"]


@pytest.fixture
def env():
    d = tempfile.mkdtemp(prefix="engram_test_nonobj_meta_")
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(d)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    from engram_core import _get_db
    conn = _get_db()

    def setmeta(nid, text):
        conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (text, nid))
        conn.commit()

    yield server, conn, setmeta
    shutil.rmtree(d, ignore_errors=True)


def _call(fn, **p):
    return json.loads(fn(payload_json=json.dumps(p)))


def _person(server, name="Zorblax Quentin"):
    r = _call(server.engram_add_person, name=name)
    nid = r.get("person_id") or r.get("node_id") or r.get("id")
    assert nid, r
    return nid


def _obs(server, tag, claim=None):
    r = _call(
        server.engram_add_observation,
        url=f"https://example.com/{tag}", title=f"Src {tag}",
        quoted_text=f"{tag} verbatim quote text.",
        interpretation=f"{tag} interpretation text here.",
        claim=claim or f"{tag} granite quarry output rose in spring.",
        quote_type="hard_data",
    )
    assert "observation_id" in r, r
    return r["observation_id"]


def _derive(server, premises, **extra):
    r = _call(
        server.engram_derive,
        claim="Coastal output shifts seasonally across both sectors.",
        supporting_ids=",".join(premises),
        logical_chain="Both series move with the season, so output is seasonal.",
        reasoning_type="inductive_generalization",
        **extra,
    )
    did = r.get("derivation_id") or r.get("node_id") or r.get("id")
    assert did, r
    return did


def _recall(server, nid):
    return _call(server.engram_inspect, node_id=nid, view="recall")


# ---- C1 -------------------------------------------------------------------

@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_C1_person_recall_view_non_object_metadata(env, blob):
    server, conn, setmeta = env
    pid = _person(server)
    setmeta(pid, blob)
    r = _recall(server, pid)
    assert "error" not in r, r
    node = r["node"]
    for k in ("pronouns", "relation", "special_moment", "support_lost", "metadata"):
        assert k not in node, (k, node)


# ---- C2 pins (green before and after) -------------------------------------

def test_C2_person_dict_metadata_still_promotes(env):
    server, conn, setmeta = env
    pid = _person(server)
    setmeta(pid, json.dumps({"pronouns": "she/her", "relation": "colleague"}))
    node = _recall(server, pid)["node"]
    assert node["pronouns"] == "she/her"
    assert node["relation"] == "colleague"


def test_C2_observation_quote_verified_still_promotes(env):
    server, conn, setmeta = env
    oid = _obs(server, "qv")
    setmeta(oid, json.dumps({"quote_verified": True}))
    assert _recall(server, oid)["node"]["quote_verified"] is True


def test_C2_derivation_warrant_still_promotes(env):
    server, conn, setmeta = env
    a = _obs(server, "pa")
    b = _obs(server, "pb", "Bravo kelp harvest yields fell in winter.")
    did = _derive(server, [a, b], warrant="seasonal cycles dominate both series")
    assert _recall(server, did)["node"]["warrant"] == "seasonal cycles dominate both series"


# ---- C3 sweep cells: end-to-end through the real MCP calls ----------------

@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_C3_query_result_path_person_non_object_metadata(env, blob):
    """_query_impl person branch (the `_pmeta.get` pair, return_debug off)."""
    server, conn, setmeta = env
    pid = _person(server, "Zorblax Quentin")
    setmeta(pid, blob)
    r = _call(server.engram_query, query="Zorblax Quentin")
    assert "error" not in r, r
    ids = [n.get("id") for n in r.get("results", [])]
    assert pid in ids, r
    row = [n for n in r["results"] if n.get("id") == pid][0]
    assert "pronouns" not in row and "relation" not in row, row


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_C3_surface_path_person_non_object_metadata(env, blob):
    """_surface_impl special-node person branch."""
    server, conn, setmeta = env
    pid = _person(server, "Zorblax Quentin")
    setmeta(pid, blob)
    r = _call(server.engram_surface, query="Zorblax Quentin")
    assert "error" not in r, r


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_C3_tainted_premise_non_object_metadata(env, blob):
    """_extract_warnings `rmeta.get` on a retracted premise's metadata."""
    server, conn, setmeta = env
    a = _obs(server, "ta")
    b = _obs(server, "tb", "Bravo kelp harvest yields fell in winter.")
    did = _derive(server, [a, b])
    rr = _call(
        server.engram_retract, node_id=a, error_type="other",
        reason="test retraction", replacement_absent_because="NONE_BECAUSE_NO_SUCCESSOR",
    )
    assert "error" not in rr, rr
    setmeta(a, blob)
    r = _recall(server, did)
    assert "error" not in r, r
    assert r["node"]["id"] == did


def _high_importance(conn, nid):
    conn.execute(
        "UPDATE nodes SET importance_base = 0.95, importance_score = 0.1 WHERE id = ?",
        (nid,),
    )
    conn.commit()


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_C3_stale_load_bearing_own_non_object_metadata(env, blob):
    """_visible_protection_channels `meta.get` on the candidate's own blob."""
    server, conn, setmeta = env
    oid = _obs(server, "sl")
    _high_importance(conn, oid)
    setmeta(oid, blob)
    r = _call(server.engram_query_pattern, pattern_name="stale_load_bearing")
    assert "error" not in r, r


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_C3_stale_load_bearing_owning_lesson_non_object_metadata(env, blob):
    """_visible_protection_channels `lmeta.get` on an exemplified lesson."""
    server, conn, setmeta = env
    oid = _obs(server, "sx")
    _high_importance(conn, oid)
    lr = _call(
        server.engram_add_lesson,
        claim="When a quarry report arrives, check the season first.",
        incident_ids=oid,
        scaffolding_nudge="Check the season before reading the quarry figure.",
        logical_chain="The single quarry incident shows seasonality confounds output.",
        situation_pattern="quarry output report received",
    )
    lid = lr.get("lesson_id") or lr.get("node_id") or lr.get("id")
    assert lid, lr
    setmeta(lid, blob)
    r = _call(server.engram_query_pattern, pattern_name="stale_load_bearing")
    assert "error" not in r, r


# ---- C3 pins: sites a non-object blob cannot crash (green before and after) ----

@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
@pytest.mark.parametrize("kind", ["person", "observation"])
def test_C3_pin_deep_view_and_observation_recall_tolerate_non_object(env, blob, kind):
    """`parsed_metadata` is stored, never `.get`-ed, and stripped from output;
    the observation recall branch is protected by the same gate."""
    server, conn, setmeta = env
    nid = _person(server) if kind == "person" else _obs(server, "pin")
    setmeta(nid, blob)
    for view in ("deep", "recall"):
        r = _call(server.engram_inspect, node_id=nid, view=view)
        assert "error" not in r, (view, r)
        assert "quote_verified" not in r["node"]
