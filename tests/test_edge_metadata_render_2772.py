"""#2772: every edge entry the read tools return renders the edge's stored
metadata under ONE key, `edge_metadata`.

Contract:
  1. The WHOLE stored metadata dict is rendered, as stored, on every edge
     entry of engram_inspect views `edges`, `deep`, `recall` and of
     engram_get_subgraph (views `edges` and `recall`).
  2. The key is ABSENT when the stored metadata is NULL, empty, invalid JSON
     or not a JSON object. Never a raw string, never an error.
  3. The STORED key is `reason` (the tool parameter is `supersede_reason`);
     a render keyed on the parameter name shows nothing.
  4. Additive: existing keys unchanged.
All fixtures live in a temporary ENGRAM home.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_edge_metadata_2772_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

REASON = "keeps the class finding; replaces the account"
NOTE = "why this incident fits"
CITE_REASON = "cited for the measurement"


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
    os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
    return server


def _call(server, tool, **payload):
    return json.loads(getattr(server, tool)(payload_json=json.dumps(payload)))


def _axiom(server, claim):
    r = _call(server, "engram_add_axiom", claim=claim, basis="Test basis.")
    assert "axiom_id" in r, r
    return r["axiom_id"]


def _seed(server):
    old = _axiom(server, "Old claim alpha.")
    new = _axiom(server, "New claim alpha.")
    r = _call(server, "engram_supersede", old_node_id=old, new_node_id=new,
              supersede_reason=REASON)
    assert "error" not in r, r

    cs_support = _axiom(server, "Support for the cornerstone.")
    cs = _call(server, "engram_add_cornerstone", tag="t", title="CS",
               new_frame="frame", supporting_ids=cs_support)
    cs_id = cs.get("cornerstone_id")
    assert cs_id, cs
    ex = _axiom(server, "A situation the principle fired in.")
    r = _call(server, "engram_register_exemplar", target_id=cs_id,
              exemplar_id=ex, note=NOTE)
    assert r.get("status") == "created", r

    # edges without usable metadata, made by SQL on the tmp store
    hub = _axiom(server, "Hub node.")
    ids = {"old": old, "new": new, "cs": cs_id, "ex": ex, "hub": hub}
    # a `cites` edge carrying a reason; no public tool writes one, so SQL
    ids["citer"] = _axiom(server, "A claim that cites a source.")
    ids["cited"] = _axiom(server, "A source that is cited.")
    cases = (("null", None), ("bad", "{not json"),
             ("list", "[1, 2]"), ("empty", "{}"))
    for name, _meta in cases:
        ids[name] = _axiom(server, f"Neighbour {name}.")
    conn = server._get_db()
    try:
        for name, meta in cases:
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at, metadata) "
                "VALUES (?, ?, 'cites', '2026-01-01T00:00:00Z', ?)",
                (hub, ids[name], meta),
            )
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at, metadata) "
            "VALUES (?, ?, 'cites', '2026-01-01T00:00:00Z', ?)",
            (ids["citer"], ids["cited"], json.dumps({"reason": CITE_REASON})),
        )
        conn.commit()
    finally:
        conn.close()
    return ids


def _inspect(server, node_id, view):
    r = _call(server, "engram_inspect", node_id=node_id, view=view,
              include_superseded=True)
    assert "error" not in r, r
    return r


def _topo_entries(resp):
    return [e for v in resp["topology"].values() for e in v]


def _recall_entries(resp):
    ln = resp["logical_neighbors"]
    return (list(ln["upstream"]) + list(ln["downstream"]) + list(ln["lateral"])
            + list(resp["contextual_neighbors"]))


def _by_id(entries, nid, idkey):
    hits = [e for e in entries if e.get(idkey) == nid]
    assert hits, (nid, entries)
    return hits[0]


# ---- supersede reason ----------------------------------------------------

def test_edges_view_supersede_reason():
    s = fresh_server(); ids = _seed(s)
    e = _by_id(_topo_entries(_inspect(s, ids["new"], "edges")), ids["old"], "dst")
    assert e["relation"] == "supersedes"
    assert e["edge_metadata"] == {"reason": REASON}
    assert "supersede_reason" not in e["edge_metadata"]


def test_deep_view_supersede_reason():
    s = fresh_server(); ids = _seed(s)
    e = _by_id(_topo_entries(_inspect(s, ids["new"], "deep")), ids["old"], "dst")
    assert e["edge_metadata"] == {"reason": REASON}


def test_recall_view_supersede_reason():
    s = fresh_server(); ids = _seed(s)
    e = _by_id(_recall_entries(_inspect(s, ids["new"], "recall")), ids["old"], "id")
    assert e["relation"] == "supersedes"
    assert e["edge_metadata"] == {"reason": REASON}


def test_get_subgraph_supersede_reason_both_views():
    s = fresh_server(); ids = _seed(s)
    for view in ("edges", "recall"):
        resp = _call(s, "engram_get_subgraph", node_id=ids["new"], depth=1,
                     direction="both", view=view)
        assert "error" not in resp, resp
        out = _by_id(resp["topology"][ids["new"]], ids["old"], "to")
        inc = _by_id(resp["topology"][ids["old"]], ids["new"], "to")
        for ent in (out, inc):
            assert ent["edge_metadata"] == {"reason": REASON}, (view, ent)


def test_get_neighbors_helper_renders_edge_metadata():
    s = fresh_server(); ids = _seed(s)
    import engram_query
    conn = s._get_db()
    try:
        nbs = engram_query._get_neighbors(conn, ids["new"])
    finally:
        conn.close()
    e = _by_id(nbs, ids["old"], "neighbor_id")
    assert e["edge_metadata"] == {"reason": REASON}


def test_recall_view_cites_reason_in_contextual_neighbors():
    # `cites` is a contextual relation: the recall view renders it through
    # _trim_contextual, a different site from the logical groups.
    s = fresh_server(); ids = _seed(s)
    resp = _inspect(s, ids["citer"], "recall")
    e = _by_id(resp["contextual_neighbors"], ids["cited"], "id")
    assert e["relation"] == "cites"
    assert e["edge_metadata"] == {"reason": CITE_REASON}


def test_engram_query_neighbors_carry_edge_metadata():
    # the public engram_query path (node["neighbors"]), not the helper alone.
    # `neighbors` is only on the return_debug=True shape (the default tiered
    # shape carries none), so that is the public surface that exposes it.
    s = fresh_server(); ids = _seed(s)
    r = _call(s, "engram_query", query="New claim alpha",
              include_superseded=True, return_debug=True)
    assert "error" not in r, r
    node = _by_id(r["results"], ids["new"], "id")
    e = _by_id(node["neighbors"], ids["old"], "neighbor_id")
    assert e["relation"] == "supersedes"
    assert e["edge_metadata"] == {"reason": REASON}


# ---- exemplifies note ----------------------------------------------------

def test_exemplifies_note_every_view():
    s = fresh_server(); ids = _seed(s)
    for view in ("edges", "deep"):
        e = _by_id(_topo_entries(_inspect(s, ids["cs"], view)), ids["ex"], "src")
        assert e["relation"] == "exemplifies"
        assert e["edge_metadata"] == {"note": NOTE}, view
    e = _by_id(_recall_entries(_inspect(s, ids["cs"], "recall")), ids["ex"], "id")
    assert e["edge_metadata"] == {"note": NOTE}
    sg = _call(s, "engram_get_subgraph", node_id=ids["cs"], depth=1,
               direction="both", view="edges")
    e = _by_id(sg["topology"][ids["cs"]], ids["ex"], "to")
    assert e["edge_metadata"] == {"note": NOTE}


# ---- absent when unusable ------------------------------------------------

def test_key_absent_for_null_empty_invalid_nonobject():
    s = fresh_server(); ids = _seed(s)
    names = ("null", "bad", "list", "empty")
    for view in ("edges", "deep"):
        entries = _topo_entries(_inspect(s, ids["hub"], view))
        for k in names:
            e = _by_id(entries, ids[k], "dst")
            assert e["relation"] == "cites"
            assert "edge_metadata" not in e, (view, k, e)
    entries = _recall_entries(_inspect(s, ids["hub"], "recall"))
    for k in names:
        assert "edge_metadata" not in _by_id(entries, ids[k], "id"), k
    for view in ("edges", "recall"):
        sg = _call(s, "engram_get_subgraph", node_id=ids["hub"], depth=1,
                   direction="both", view=view)
        assert "error" not in sg, sg
        for k in names:
            e = _by_id(sg["topology"][ids["hub"]], ids[k], "to")
            assert "edge_metadata" not in e, (view, k, e)


# ---- additive ------------------------------------------------------------

def test_existing_keys_unchanged():
    s = fresh_server(); ids = _seed(s)
    e = _by_id(_topo_entries(_inspect(s, ids["new"], "edges")), ids["old"], "dst")
    assert set(e) == {"src", "dst", "relation", "edge_metadata"}
    sg = _call(s, "engram_get_subgraph", node_id=ids["hub"], depth=1,
               direction="both", view="edges")
    e = _by_id(sg["topology"][ids["hub"]], ids["null"], "to")
    assert set(e) == {"to", "relation", "direction"}


# ---- the WHOLE dict, not a projection of it ------------------------------

TWO_KEY = {"reason": "kept the finding", "extra": "x"}
NESTED = {"reason": "r", "detail": {"a": [1, 2]}}


def _seed_rich(server):
    """_seed plus two `cites` edges (SQL on the tmp store) whose metadata is
    a two-key dict (second key not reason/note) and a nested-value dict."""
    ids = _seed(server)
    ids["rich"] = _axiom(server, "Rich metadata source.")
    ids["two"] = _axiom(server, "Target of the two-key edge.")
    ids["nest"] = _axiom(server, "Target of the nested edge.")
    conn = server._get_db()
    try:
        for tgt, meta in (("two", TWO_KEY), ("nest", NESTED)):
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at, metadata) "
                "VALUES (?, ?, 'cites', '2026-01-01T00:00:00Z', ?)",
                (ids["rich"], ids[tgt], json.dumps(meta)),
            )
        conn.commit()
    finally:
        conn.close()
    return ids


def _assert_whole(server, ids, key, expected):
    for view in ("edges", "deep"):
        e = _by_id(_topo_entries(_inspect(server, ids["rich"], view)),
                   ids[key], "dst")
        assert e["edge_metadata"] == expected, (view, e)
    for view in ("edges", "recall"):
        resp = _call(server, "engram_get_subgraph", node_id=ids["rich"],
                     depth=1, direction="both", view=view)
        assert "error" not in resp, resp
        out = _by_id(resp["topology"][ids["rich"]], ids[key], "to")
        inc = _by_id(resp["topology"][ids[key]], ids["rich"], "to")
        for ent in (out, inc):
            assert ent["edge_metadata"] == expected, (view, ent)


def test_two_key_dict_rendered_whole():
    s = fresh_server(); ids = _seed_rich(s)
    _assert_whole(s, ids, "two", TWO_KEY)


def test_nested_value_rendered_whole():
    s = fresh_server(); ids = _seed_rich(s)
    _assert_whole(s, ids, "nest", NESTED)


def test_recall_view_superseded_node_downstream_group():
    # inspecting the OLD node puts the new node in the DOWNSTREAM group,
    # a different render group from the new node's upstream view.
    s = fresh_server(); ids = _seed(s)
    resp = _inspect(s, ids["old"], "recall")
    e = _by_id(resp["logical_neighbors"]["downstream"], ids["new"], "id")
    assert e["relation"] == "supersedes"
    assert e["edge_metadata"] == {"reason": REASON}
