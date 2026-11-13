"""#3864 -- person readers fall back from `relation` to the deprecated `role`.

Person nodes carry their relation in `metadata.relation`; legacy nodes carry
`metadata.role`. Every reader must show both cohorts. Contract:

  A. `engram_core.person_relation(meta)`: relation if a non-empty string after
     strip, else role under the same test, else ""; non-dict / non-string
     values -> "" and never raises.
  B/C. Each reader site renders a role-only AND a relation-only person:
     snapshot (`Role:` line), reflect `known_people[].role`, recall view
     (`engram_inspect`), surface person entry, tiered `engram_query`.
     Output keys and labels are unchanged.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_person_relation_3864_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_POLARITY"] = "1"
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    return server


def _add_person(server, name, relation):
    r = json.loads(server.engram_add_person(payload_json=json.dumps(
        {"name": name, "relation": relation})))
    assert "error" not in r, r
    return r["person_id"]


def _make_role_only(server, pid):
    """Rewrite a person's metadata to the legacy shape: `role`, no `relation`."""
    import engram_core as core
    conn = core._get_db()
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (pid,)).fetchone()
        meta = json.loads(row["metadata"])
        meta["role"] = meta.pop("relation")
        conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (json.dumps(meta), pid))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def people():
    """(server, role_only_pid, relation_only_pid)."""
    server = fresh_server()
    legacy = _add_person(server, "Legacyperson", "oldrel")
    _make_role_only(server, legacy)
    modern = _add_person(server, "Modernperson", "newrel")
    return server, legacy, modern


# --- A. the helper ----------------------------------------------------------

def _helper():
    fresh_server()
    import engram_core as core
    return core.person_relation


@pytest.mark.parametrize("meta,expected", [
    ({"relation": "friend"}, "friend"),
    ({"role": "mentor"}, "mentor"),
    ({"relation": "friend", "role": "mentor"}, "friend"),
    ({"relation": "", "role": ""}, ""),
    ({"relation": "  ", "role": "\t"}, ""),
    ({"relation": "  ", "role": "mentor"}, "mentor"),
    ({"relation": "", "role": "mentor"}, "mentor"),
    ({"relation": 5, "role": "mentor"}, "mentor"),
    ({"relation": ["x"], "role": None}, ""),
    ({"relation": None, "role": 3}, ""),
    ({}, ""),
    (None, ""),
    ("friend", ""),
    (["relation"], ""),
    (5, ""),
])
def test_person_relation_helper(meta, expected):
    assert _helper()(meta) == expected


# --- B/C. reader sites ------------------------------------------------------

def test_snapshot_renders_both_cohorts(people):
    server, legacy, modern = people
    import engram_core as core
    conn = core._get_db()
    try:
        snap = core._generate_snapshot(conn)
    finally:
        conn.close()
    lines = snap.splitlines()
    for pid, rel in ((legacy, "oldrel"), (modern, "newrel")):
        i = lines.index(next(l for l in lines if l.startswith(f"### {pid} ")))
        assert f"Role: {rel}" in lines[i:i + 4], (pid, lines[i:i + 4])


def test_reflect_known_people_both_cohorts(people, monkeypatch):
    server, legacy, modern = people
    # `known_people` is built by _reflect_impl but is not in
    # _REFLECT_LIST_ORDER, so _slim_reflect_briefing drops it from the
    # engram_reflect output today. Read the raw report to test the reader.
    import engram_lifecycle
    monkeypatch.setattr(engram_lifecycle, "_slim_reflect_briefing", lambda report: report)
    r = json.loads(engram_lifecycle._reflect_impl())
    by_id = {p["id"]: p for p in r["known_people"]}
    assert by_id[legacy]["role"] == "oldrel"
    assert by_id[modern]["role"] == "newrel"
    assert set(by_id[legacy]) == {"id", "name", "role", "since"}


def test_inspect_recall_view_both_cohorts(people):
    server, legacy, modern = people
    for pid, rel in ((legacy, "oldrel"), (modern, "newrel")):
        r = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": pid})))
        assert r["node"].get("relation") == rel, r["node"]


def _entries_for(obj, pid, found=None):
    found = [] if found is None else found
    if isinstance(obj, dict):
        if obj.get("id") == pid:
            found.append(obj)
        for v in obj.values():
            _entries_for(v, pid, found)
    elif isinstance(obj, list):
        for v in obj:
            _entries_for(v, pid, found)
    return found


def test_surface_person_entry_both_cohorts(people):
    server, legacy, modern = people
    for pid, name, rel in ((legacy, "Legacyperson", "oldrel"),
                           (modern, "Modernperson", "newrel")):
        r = json.loads(server.engram_surface(payload_json=json.dumps(
            {"query": name, "top_k": 10, "semantic": False})))
        found = _entries_for(r, pid)
        assert any(e.get("relation") == rel for e in found), (pid, found)


def test_query_tiered_both_cohorts(people):
    server, legacy, modern = people
    for pid, name, rel in ((legacy, "Legacyperson", "oldrel"),
                           (modern, "Modernperson", "newrel")):
        r = json.loads(server.engram_query(payload_json=json.dumps({"query": name})))
        entry = next(e for e in r["results"] if e["id"] == pid)
        assert entry.get("relation") == rel, entry


def test_query_omits_relation_when_neither_key_set():
    server = fresh_server()
    r = json.loads(server.engram_add_person(payload_json=json.dumps({"name": "Barepersonxyz"})))
    pid = r["person_id"]
    q = json.loads(server.engram_query(payload_json=json.dumps({"query": "Barepersonxyz"})))
    entry = next(e for e in q["results"] if e["id"] == pid)
    assert "relation" not in entry
