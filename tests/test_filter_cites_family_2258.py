"""#2258 -- the `cites` / `cited_by` virtual filter fields must match the whole
claim-citation relation family, not a relation that does not exist.

The fields' relation tuple once named `supports`, which is not in
VALID_RELATIONS, so a `cites`/`cited_by` filter silently missed every
`supported_by` and `acknowledged_derives_from` edge. The family's canonical
definition is `engram_core.CITATION_SUPPORT_RELATIONS` plus `cites`.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile

import pytest

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_filter_cites_2258_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.path[0] != repo_root:
        sys.path.insert(0, repo_root)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server

    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _add_observation(server, claim: str) -> str:
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/test",
        "title": "Test source",
        "quoted_text": claim + " in the cited source",
        "interpretation": f"Interpretation of: {claim}",
        "claim": claim,
        "quote_type": "hard_data",
    })))
    return r["observation_id"]


def _list_ids(server, field: str, value: str) -> set:
    r = json.loads(server.engram_list(payload_json=json.dumps({
        "filters_json": json.dumps({"field": field, "op": "eq", "value": value}),
    })))
    assert "error" not in r, r
    return {n["id"] for n in r["nodes"]}


def _insert_edge(server, src, tgt, relation):
    conn = server._get_db()
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, ?, 't')",
        (src, tgt, relation),
    )
    conn.commit()


@pytest.mark.parametrize("field", ["cites", "cited_by"])
def test_virtual_field_relations_are_the_citation_family(field):
    """Binding cell: the tuple equals {cites} | CITATION_SUPPORT_RELATIONS and
    every member is a real relation."""
    _fresh_server()
    import engram_core
    from engram_filter import VIRTUAL_FIELDS

    rels = set(VIRTUAL_FIELDS[field]["relations"])
    assert rels == {"cites"} | set(engram_core.CITATION_SUPPORT_RELATIONS)
    assert rels <= set(engram_core.VALID_RELATIONS)


def test_cites_and_cited_by_match_a_supported_by_edge():
    """Behaviour cell: a real supported_by edge (cornerstone A -> observation B,
    written by engram_add_cornerstone) is matched in both directions."""
    server = _fresh_server()
    b = _add_observation(server, "Premise observation for 2258.")
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps({
        "tag": "test-2258",
        "title": "Test cornerstone",
        "new_frame": "Operate according to the test principle.",
        "supporting_ids": b,
    })))
    assert "error" not in r, r
    a = r["cornerstone_id"]
    conn = server._get_db()
    assert conn.execute(
        "SELECT 1 FROM edges WHERE source_id=? AND target_id=? AND relation='supported_by'",
        (a, b),
    ).fetchone(), "fixture must contain a supported_by edge"
    assert a in _list_ids(server, "cites", b)
    assert b in _list_ids(server, "cited_by", a)


def test_cites_and_cited_by_match_an_acknowledged_derives_from_edge():
    """Edge row inserted directly in the TEST db: the only writer is
    engram_cascade_resolve, which needs a full supersession scenario."""
    server = _fresh_server()
    a = _add_observation(server, "Acknowledging node 2258.")
    b = _add_observation(server, "Replacement premise 2258.")
    _insert_edge(server, a, b, "acknowledged_derives_from")
    assert a in _list_ids(server, "cites", b)
    assert b in _list_ids(server, "cited_by", a)


def test_control_cites_edge_still_matches():
    server = _fresh_server()
    a = _add_observation(server, "Citing node 2258.")
    b = _add_observation(server, "Cited node 2258.")
    _insert_edge(server, a, b, "cites")
    assert a in _list_ids(server, "cites", b)
    assert b in _list_ids(server, "cited_by", a)
