"""engram_update_task must refuse a task that is not current (engram-alpha #4186).

The probe in the issue: a retracted task updated to `done` ended `('done', 0)`:
the tool never read `is_current`, so a retracted or superseded row was written
through. The fix has two parts:

  1. an up-front refusal (`refusal: not_current`) right after the not-found /
     not-a-task checks, on `is_current` falsy OR `status == 'retracted'` (two
     separate conditions: a retracted row has both, a superseded row only
     `is_current = 0`, and a `retracted`-but-current row must still refuse);
  2. the guarded UPDATE gains `AND is_current = 1`, so a supersede that lands
     between the read and the write refuses as `changed_concurrently`.

C1-C5 exercise the tool; C6 pins the dream-master instruction text. The dream
master is an LLM agent, so C6 pins the TEXT of the instruction (each
`engram_update_task(` call site carries the error-branch clause); there is no
runtime walk to execute.
"""

import json
import os
import re
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER_MD = os.path.join(
    REPO, "src", "engram", "agents", "claude", "engram-dream-master.md")


@pytest.fixture
def env():
    d = tempfile.mkdtemp(prefix="engram_test_update_task_not_current_4186_")
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(d)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    yield server
    shutil.rmtree(d, ignore_errors=True)


def _call(fn, **p):
    return json.loads(fn(payload_json=json.dumps(p)))


def _task(server, desc="Sweep the granite quarry floor."):
    r = _call(server.engram_add_task, description=desc)
    assert "task_id" in r, r
    return r["task_id"]


def _row(server, nid):
    conn = server.core._get_db()
    try:
        return dict(conn.execute(
            "SELECT * FROM nodes WHERE id = ?", (nid,)).fetchone())
    finally:
        conn.close()


def _set(server, nid, **cols):
    conn = server.core._get_db()
    try:
        for col, val in cols.items():
            conn.execute(f"UPDATE nodes SET {col} = ? WHERE id = ?", (val, nid))
        conn.commit()
    finally:
        conn.close()


def _edits(server, nid):
    conn = server.core._get_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM edit_history WHERE node_id = ?",
            (nid,)).fetchone()["n"]
    finally:
        conn.close()


_FIELDS = ("status", "is_current", "importance_base", "importance_score",
           "metadata")


def _assert_refused_unchanged(server, tid, before, edits_before, r):
    assert r.get("refusal") == "not_current", r
    assert "not current" in r["error"] and "nothing was changed" in r["error"], r
    assert tid in r["error"], r
    after = _row(server, tid)
    for f in _FIELDS:
        assert after[f] == before[f], (f, before[f], after[f])
    assert _edits(server, tid) == edits_before


# C1: retracted task (status='retracted', is_current=0, set directly, as the
# issue's probe did)
def test_C1_retracted_task_refused(env):
    server = env
    tid = _task(server)
    _set(server, tid, status="retracted", is_current=0)
    before, edits = _row(server, tid), _edits(server, tid)
    r = _call(server.engram_update_task, task_id=tid, new_status="done",
              note="should not land")
    _assert_refused_unchanged(server, tid, before, edits, r)


# C2: superseded task, via the PUBLIC engram_supersede path (it accepts task
# nodes in the fixture DB; it leaves status 'planned' and flips is_current=0).
def test_C2_superseded_task_refused(env):
    server = env
    tid = _task(server, "Old plan for the quarry floor.")
    new = _task(server, "New plan for the quarry floor.")
    s = json.loads(server.engram_supersede(payload_json=json.dumps(
        {"old_node_id": tid, "new_node_id": new, "supersede_reason": "replanned"})))
    assert s.get("status") == "superseded", s
    before, edits = _row(server, tid), _edits(server, tid)
    assert before["is_current"] == 0 and before["status"] != "retracted"
    r = _call(server.engram_update_task, task_id=tid, new_status="done")
    _assert_refused_unchanged(server, tid, before, edits, r)


# C3: status='retracted' but is_current=1 must still refuse (the two
# conditions are separate; the check must not depend on the pair co-occurring)
def test_C3_retracted_status_but_current_refused(env):
    server = env
    tid = _task(server)
    _set(server, tid, status="retracted", is_current=1)
    before, edits = _row(server, tid), _edits(server, tid)
    r = _call(server.engram_update_task, task_id=tid, new_status="done")
    _assert_refused_unchanged(server, tid, before, edits, r)


# C4 control: a current planned task still updates normally
def test_C4_current_task_updates(env):
    server = env
    tid = _task(server)
    before, edits = _row(server, tid), _edits(server, tid)
    r = _call(server.engram_update_task, task_id=tid, new_status="active")
    assert r.get("status") == "updated" and "refusal" not in r, r
    after = _row(server, tid)
    assert after["status"] == "active"
    assert after["importance_base"] != before["importance_base"]
    assert _edits(server, tid) == edits + 1


# C5: a second writer flips ONLY is_current between the read and the write.
# Interference point: core._compute_importance, called after the task row is
# read and before the guarded UPDATE, with no DML run on the call's connection.
def test_C5_concurrent_supersede_refused(env, monkeypatch):
    server = env
    tid = _task(server)
    fired = {"n": 0}
    real = server.core._compute_importance

    def interfering(*a, **k):
        if not fired["n"]:
            fired["n"] += 1
            _set(server, tid, is_current=0)
        return real(*a, **k)

    monkeypatch.setattr(server.core, "_compute_importance", interfering)
    edits = _edits(server, tid)
    r = _call(server.engram_update_task, task_id=tid, new_status="active")
    assert fired["n"] == 1
    assert r.get("refusal") == "changed_concurrently", r
    row = _row(server, tid)
    assert row["is_current"] == 0  # the second writer's flip stands
    assert row["status"] == "planned"  # original status kept
    assert _edits(server, tid) == edits


# C6: dream-master instruction text
_CLAUSE_PARTS = ("do not retry", "update_task refused:", "continue the walk",
                 "not_current", "changed_concurrently")


def _passages(text):
    """One passage per `engram_update_task(` occurrence: from the call to the
    next list item (a line starting with optional indent then `- ` or `N. `)
    or heading (`#`), or end of file."""
    boundary = re.compile(r"^[ \t]*(?:- |\d+\. |#)", re.M)
    out = []
    for m in re.finditer(r"engram_update_task\(", text):
        nl = text.find("\n", m.end())
        start_of_search = nl + 1 if nl != -1 else len(text)
        b = boundary.search(text, start_of_search)
        out.append(text[m.start():b.start() if b else len(text)])
    return out


def test_C6_dream_master_call_sites_carry_error_branch():
    with open(MASTER_MD, encoding="utf-8") as fh:
        text = fh.read()
    passages = _passages(text)
    assert len(passages) == 2, (
        f"expected exactly 2 engram_update_task( call sites, found "
        f"{len(passages)}; a new call site needs the error-branch clause "
        f"and a bump here")
    for p in passages:
        assert p.strip(), "passage boundary not found"
        for part in _CLAUSE_PARTS:
            assert part in p, (part, p)
