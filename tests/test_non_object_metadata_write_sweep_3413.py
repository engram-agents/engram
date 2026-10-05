"""End-to-end sweep of the WRITE paths (parse metadata, mutate, re-dump) for a
valid-JSON non-object `nodes.metadata` blob (#3413 write half, tracked #3425).

Contract (written before the routing; one section per module, one commit per
module):
  Q1 for each of `[1,2]`, `"str"`, `5`, `null` stored in nodes.metadata, the
     real tool call that reaches the write site SUCCEEDS (no AttributeError /
     TypeError, no exception-shaped error).
  Q2 the stored metadata afterwards is a DICT holding the site's intended new
     keys (a non-object blob is treated as {}).
  Q3 exactly one stderr line names the node id and the blob's type and says it
     was replaced with a dict (`engram: node <id> metadata was of type <t>,
     not an object; replaced with a dict on write (tool=<name>)`).
  Q4 where the tool's response already has a `warnings` field, it carries a
     matching entry; no new response key is invented where there is none.
  Q5 for a DICT blob the call behaves as before and logs nothing.
Sites with no real caller are covered by a DIRECT call, labelled as such.
"""

import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NON_OBJECT_BLOBS = [
    pytest.param("[1,2]", "list", id="list"),
    pytest.param('"str"', "str", id="str"),
    pytest.param("5", "int", id="int"),
    pytest.param("null", "null", id="null"),
]


@pytest.fixture
def env():
    d = tempfile.mkdtemp(prefix="engram_test_nonobj_write_")
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


def _idof(r, *keys):
    for k in keys + ("node_id", "id"):
        if r.get(k):
            return r[k]
    raise AssertionError(r)


def _person(server, name="Zorblax Quentin"):
    return _idof(_call(server.engram_add_person, name=name), "person_id")


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


def _stored(conn, nid):
    row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (nid,)).fetchone()
    return json.loads(row["metadata"])


def _replaced_lines(capsys, nid):
    err = capsys.readouterr().err
    return [x for x in err.splitlines()
            if nid in x and "replaced with a dict on write" in x]


def assert_logged_once(capsys, nid, tname, tool=None):
    """Q3: exactly one stderr line for `nid`, naming the type (and tool)."""
    lines = _replaced_lines(capsys, nid)
    assert len(lines) == 1, lines
    assert f"of type {tname}" in lines[0], lines[0]
    if tool:
        assert f"tool={tool}" in lines[0], lines[0]


def assert_not_logged(capsys, nid):
    """Q5: a dict blob writes exactly as before and says nothing."""
    assert _replaced_lines(capsys, nid) == []


def assert_warned(r, nid):
    """Q4: the response's `warnings` carries an entry for the node."""
    w = r.get("warnings")
    assert isinstance(w, list) and any(
        nid in str(x) and "replaced with a dict" in str(x) for x in w), r


# ---- core: _backfill_edit_history (DB-init migration; direct) --------------

@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_core_backfill_edit_history_direct_blob(env, blob, tname, capsys):
    """DIRECT call: `_backfill_edit_history` is a one-time DB-init migration,
    not a tool. It only READS the retracted node's metadata (edit_history rows
    are what it writes), so nothing on disk is replaced and it must neither
    crash DB init nor log a replacement."""
    server, conn, setmeta = env
    import engram_core
    oid = _obs(server, "bf")
    rr = _call(server.engram_retract, node_id=oid, error_type="other",
               reason="test retraction",
               replacement_absent_because="NONE_BECAUSE_NO_SUCCESSOR")
    assert "error" not in rr, rr
    setmeta(oid, blob)
    conn.execute("DELETE FROM edit_history")
    conn.commit()
    capsys.readouterr()
    engram_core._backfill_edit_history(conn)
    row = conn.execute(
        "SELECT timestamp, details FROM edit_history "
        "WHERE node_id = ? AND action = 'retracted'", (oid,)).fetchone()
    assert row is not None
    assert row["timestamp"] == "1970-01-01T00:00:00"
    assert json.loads(row["details"])["error_type"] is None
    assert _replaced_lines(capsys, oid) == []


def test_core_backfill_edit_history_dict_blob_unchanged(env):
    server, conn, setmeta = env
    import engram_core
    oid = _obs(server, "bd")
    _call(server.engram_retract, node_id=oid, error_type="other",
          reason="test retraction",
          replacement_absent_because="NONE_BECAUSE_NO_SUCCESSOR")
    conn.execute("DELETE FROM edit_history")
    conn.commit()
    engram_core._backfill_edit_history(conn)
    row = conn.execute(
        "SELECT timestamp, details FROM edit_history "
        "WHERE node_id = ? AND action = 'retracted'", (oid,)).fetchone()
    assert row["timestamp"] != "1970-01-01T00:00:00"
    assert json.loads(row["details"])["error_type"] == "other"


# ---- epistemic ------------------------------------------------------------

def _lesson(server, tag="ls"):
    oid = _obs(server, tag)
    r = _call(
        server.engram_add_lesson,
        claim="When a quarry report arrives, check the season first.",
        incident_ids=oid,
        scaffolding_nudge="Check the season before reading the quarry figure.",
        logical_chain="The single quarry incident shows seasonality confounds output.",
        situation_pattern="quarry output report received",
    )
    return _idof(r, "lesson_id"), oid


@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_epistemic_update_lesson_blob(env, blob, tname, capsys):
    """`_update_lesson_impl`: parse, add field_traces, set field, re-dump.
    The response has no `warnings` field, so the stderr line is the signal."""
    server, conn, setmeta = env
    lid, _ = _lesson(server, "ul")
    setmeta(lid, blob)
    capsys.readouterr()
    r = _call(server.engram_update_lesson, node_id=lid,
              updates={"scaffolding_nudge": "Season first, then the figure."})
    assert r.get("status") == "updated", r
    assert "warnings" not in r, r
    m = _stored(conn, lid)
    assert isinstance(m, dict)
    assert m["scaffolding_nudge"] == "Season first, then the figure."
    assert m["field_traces"]["scaffolding_nudge"][0]["prior_value"] is None
    assert_logged_once(capsys, lid, tname, "engram_update_lesson")


def test_epistemic_update_lesson_dict_blob_unchanged(env, capsys):
    server, conn, setmeta = env
    lid, _ = _lesson(server, "ud")
    before = _stored(conn, lid)
    capsys.readouterr()
    r = _call(server.engram_update_lesson, node_id=lid,
              updates={"scaffolding_nudge": "Season first, then the figure."})
    assert r.get("status") == "updated", r
    m = _stored(conn, lid)
    assert m["situation_pattern"] == before["situation_pattern"]
    assert m["field_traces"]["scaffolding_nudge"][0]["prior_value"] == before["scaffolding_nudge"]
    assert_not_logged(capsys, lid)


@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_epistemic_register_exemplar_blob_replaces_nothing(env, blob, tname, capsys):
    """`_register_exemplar_impl` support_lost clear is conditional on the blob
    carrying `support_lost`; a non-object blob reads as {} so NOTHING is
    written back. The call succeeds, the blob is left exactly as stored, and
    no replacement is logged (none happened)."""
    server, conn, setmeta = env
    lid, _ = _lesson(server, "re")
    ex = _obs(server, "rx", "Bravo kelp harvest yields fell in winter.")
    setmeta(lid, blob)
    capsys.readouterr()
    r = _call(server.engram_register_exemplar, target_id=lid, exemplar_id=ex)
    assert r.get("status") == "created", r
    assert conn.execute("SELECT metadata FROM nodes WHERE id = ?",
                        (lid,)).fetchone()["metadata"] == blob
    assert_not_logged(capsys, lid)


def test_epistemic_register_exemplar_clears_support_lost_in_dict_blob(env, capsys):
    server, conn, setmeta = env
    lid, _ = _lesson(server, "rs")
    ex = _obs(server, "ry", "Bravo kelp harvest yields fell in winter.")
    m = _stored(conn, lid)
    m["support_lost"] = True
    setmeta(lid, json.dumps(m))
    capsys.readouterr()
    r = _call(server.engram_register_exemplar, target_id=lid, exemplar_id=ex)
    assert r.get("status") == "created", r
    assert "support_lost" not in _stored(conn, lid)
    assert_not_logged(capsys, lid)


# ---- lifecycle ------------------------------------------------------------

def test_lifecycle_reflect_support_lost_clear_list_blob(env, capsys):
    """`_reflect_impl` support_lost discharge (parse, pop, re-dump). Its SQL
    pre-filter is `metadata LIKE '%"support_lost": true%'`, so the only
    non-object blob that reaches the write is one that CONTAINS that text: a
    list holding an object. The re-dump then replaces the list with a dict
    (and the lesson, having a live exemplar, is discharged). The report has no
    `warnings` field, so stderr is the signal."""
    server, conn, setmeta = env
    lid, _ = _lesson(server, "rf")
    setmeta(lid, '[{"support_lost": true}]')
    capsys.readouterr()
    r = _call(server.engram_reflect)
    assert "error" not in r, r
    assert "warnings" not in r, r
    assert [x["id"] for x in r["support_lost_cleared"]] == [lid], r
    assert _stored(conn, lid) == {}
    assert_logged_once(capsys, lid, "list", "engram_reflect")


@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_lifecycle_reflect_other_non_object_blobs_never_reach_the_write(
        env, blob, tname, capsys):
    """`[1,2]`, `"str"`, `5`, `null` carry no `"support_lost": true` text, so
    the LIKE pre-filter never selects them: reflect succeeds, the blob is left
    exactly as stored, nothing is replaced and nothing is logged."""
    server, conn, setmeta = env
    lid, _ = _lesson(server, "rg")
    setmeta(lid, blob)
    capsys.readouterr()
    r = _call(server.engram_reflect)
    assert "error" not in r, r
    assert conn.execute("SELECT metadata FROM nodes WHERE id = ?",
                        (lid,)).fetchone()["metadata"] == blob
    assert_not_logged(capsys, lid)


def test_lifecycle_reflect_support_lost_clear_dict_blob_unchanged(env, capsys):
    server, conn, setmeta = env
    lid, _ = _lesson(server, "rd")
    m = _stored(conn, lid)
    m["support_lost"] = True
    setmeta(lid, json.dumps(m))
    capsys.readouterr()
    r = _call(server.engram_reflect)
    assert [x["id"] for x in r["support_lost_cleared"]] == [lid], r
    after = _stored(conn, lid)
    assert "support_lost" not in after
    assert after == {k: v for k, v in m.items() if k != "support_lost"}
    assert_not_logged(capsys, lid)


# ---- observation ----------------------------------------------------------

def _evidence_of(conn, oid):
    return conn.execute("SELECT evidence_id FROM nodes WHERE id = ?",
                        (oid,)).fetchone()["evidence_id"]


def _second_obs_on(server, url_tag, **extra):
    return _call(
        server.engram_add_observation,
        url=f"https://example.com/{url_tag}", title=f"Src {url_tag}",
        quoted_text=f"{url_tag} second verbatim quote text.",
        interpretation=f"{url_tag} second interpretation text here.",
        claim="Bravo kelp harvest yields fell in winter along this coast.",
        quote_type="hard_data", **extra,
    )


@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_observation_evidence_metadata_write_blob(env, blob, tname, capsys):
    """`_add_observation_impl` evidence-node metadata write (evidence_type /
    original_url passthrough): a reused evidence node whose blob is a
    non-object is replaced by a dict carrying the new keys. The tool HAS a
    `warnings` list, so the replacement is also warned there."""
    server, conn, setmeta = env
    first = _obs(server, "ev")
    ev_id = _evidence_of(conn, first)
    setmeta(ev_id, blob)
    capsys.readouterr()
    r = _second_obs_on(server, "ev", evidence_type="web_page",
                       original_url="https://example.com/orig")
    assert "observation_id" in r, r
    m = _stored(conn, ev_id)
    assert isinstance(m, dict)
    assert m["evidence_type"] == "web_page"
    assert m["original_url"] == "https://example.com/orig"
    assert_warned(r, ev_id)
    assert_logged_once(capsys, ev_id, tname, "engram_add_observation")


@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_observation_without_passthrough_args_replaces_nothing(
        env, blob, tname, capsys):
    """No evidence_type / original_url: nothing is written to the evidence
    node, so the non-object blob stays as stored and nothing is logged."""
    server, conn, setmeta = env
    first = _obs(server, "en")
    ev_id = _evidence_of(conn, first)
    setmeta(ev_id, blob)
    capsys.readouterr()
    r = _second_obs_on(server, "en")
    assert "observation_id" in r, r
    assert conn.execute("SELECT metadata FROM nodes WHERE id = ?",
                        (ev_id,)).fetchone()["metadata"] == blob
    assert not any("replaced with a dict" in str(w) for w in r.get("warnings", []))
    assert_not_logged(capsys, ev_id)


def test_observation_evidence_metadata_dict_blob_unchanged(env, capsys):
    server, conn, setmeta = env
    first = _obs(server, "ed")
    ev_id = _evidence_of(conn, first)
    before = _stored(conn, ev_id)
    capsys.readouterr()
    r = _second_obs_on(server, "ed", evidence_type="web_page",
                       original_url="https://example.com/orig")
    assert "observation_id" in r, r
    after = _stored(conn, ev_id)
    assert after == {**before, "evidence_type": "web_page",
                     "original_url": "https://example.com/orig"}
    assert not any("replaced with a dict" in str(w) for w in r.get("warnings", []))
    assert_not_logged(capsys, ev_id)


# ---- revision -------------------------------------------------------------

_RETRACT = dict(error_type="other", reason="test retraction",
                replacement_absent_because="NONE_BECAUSE_NO_SUCCESSOR")


@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_revision_retract_node_blob(env, blob, tname, capsys):
    """`_retract_impl` marks the retracted node (parse, set retracted keys,
    re-dump). The response's existing singular `warning` field carries the
    replacement (no new key is invented)."""
    server, conn, setmeta = env
    oid = _obs(server, "rn")
    setmeta(oid, blob)
    capsys.readouterr()
    r = _call(server.engram_retract, node_id=oid, **_RETRACT)
    assert "error" not in r, r
    m = _stored(conn, oid)
    assert isinstance(m, dict)
    assert m["retracted"] is True and m["error_type"] == "other"
    assert m["retraction_reason"] == "test retraction"
    assert "warnings" not in r, r
    assert oid in r["warning"] and "replaced with a dict" in r["warning"], r
    assert_logged_once(capsys, oid, tname, "engram_retract")


def test_revision_retract_node_dict_blob_unchanged(env, capsys):
    server, conn, setmeta = env
    oid = _obs(server, "rd")
    before = _stored(conn, oid)
    capsys.readouterr()
    r = _call(server.engram_retract, node_id=oid, **_RETRACT)
    assert "error" not in r, r
    after = _stored(conn, oid)
    assert {k: v for k, v in after.items()
            if k not in ("retracted", "error_type", "retraction_reason",
                         "retracted_at")} == before
    assert "warning" not in r and "warnings" not in r, r
    assert_not_logged(capsys, oid)


@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_revision_retract_person_blob(env, blob, tname, capsys):
    """The read-half cell for a person node (was a strict xfail pending this
    write path): retract of a person with a non-object blob succeeds."""
    server, conn, setmeta = env
    pid = _person(server)
    setmeta(pid, blob)
    capsys.readouterr()
    r = _call(server.engram_retract, node_id=pid, **_RETRACT)
    assert "error" not in r, r
    assert _stored(conn, pid)["retracted"] is True
    assert pid in r["warning"]
    assert_logged_once(capsys, pid, tname, "engram_retract")


@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_revision_retract_basis_prediction_blob(env, blob, tname, capsys):
    """`_retract_impl` basis-retraction branch: the paired prediction is
    retracted too (parse, set keys, re-dump); its non-object blob is replaced
    by a dict and warned on the retract response's `warning`."""
    server, conn, setmeta = env
    r0 = _call(
        server.engram_add_observation,
        url="https://example.com/pred", title="Src pred",
        quoted_text="Model signals an upcoming event.",
        interpretation="Signal predicts event.", claim="Event will occur.",
        quote_type="attributed_analysis", is_predictive=True,
        predicted_event="Event occurs within 60 days.",
    )
    assert "error" not in r0, r0
    oid, pid = r0["observation_id"], r0["prediction_id"]
    setmeta(pid, blob)
    capsys.readouterr()
    r = _call(server.engram_retract, node_id=oid, **_RETRACT)
    assert "error" not in r, r
    assert r["retracted_predictions"][0]["id"] == pid, r
    m = _stored(conn, pid)
    assert isinstance(m, dict) and m["retracted_via_basis_retraction"] == oid
    assert pid in r["warning"] and "replaced with a dict" in r["warning"], r
    assert_logged_once(capsys, pid, tname, "engram_retract")


def _lesson_over(server, conn, tag):
    lid, oid = _lesson(server, tag)
    return lid, oid


@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_revision_retract_marks_support_lost_over_blob(env, blob, tname, capsys):
    """`_detect_zero_support` (via engram_retract): the lesson's only
    exemplar is retracted, so the lesson is marked support_lost (parse, set,
    re-dump); a non-object blob is replaced by that dict and warned on the
    retract response's `warning`."""
    server, conn, setmeta = env
    lid, oid = _lesson_over(server, conn, "sl")
    setmeta(lid, blob)
    capsys.readouterr()
    r = _call(server.engram_retract, node_id=oid, **_RETRACT)
    assert "error" not in r, r
    assert [x["id"] for x in r["support_lost"]] == [lid], r
    assert _stored(conn, lid) == {"support_lost": True}
    assert lid in r["warning"] and "replaced with a dict" in r["warning"], r
    assert_logged_once(capsys, lid, tname, "engram_retract")


@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_revision_supersede_marks_support_lost_over_blob(env, blob, tname, capsys):
    """`_detect_zero_support` (via engram_supersede). Supersede's response has
    no warning field, so stderr is the signal."""
    server, conn, setmeta = env
    lid, oid = _lesson_over(server, conn, "ss")
    new = _obs(server, "ss2", "Refined kelp harvest yields fell in winter.")
    setmeta(lid, blob)
    capsys.readouterr()
    r = _call(server.engram_supersede, old_node_id=oid, new_node_id=new,
              supersede_reason="refined")
    assert "error" not in r, r
    assert [x["id"] for x in r["support_lost"]] == [lid], r
    assert "warning" not in r and "warnings" not in r, r
    assert _stored(conn, lid) == {"support_lost": True}
    assert_logged_once(capsys, lid, tname, "engram_supersede")


def test_revision_support_lost_dict_blob_unchanged(env, capsys):
    server, conn, setmeta = env
    lid, oid = _lesson_over(server, conn, "sd")
    before = _stored(conn, lid)
    capsys.readouterr()
    r = _call(server.engram_retract, node_id=oid, **_RETRACT)
    assert [x["id"] for x in r["support_lost"]] == [lid], r
    assert _stored(conn, lid) == {**before, "support_lost": True}
    assert "warning" not in r, r
    assert_not_logged(capsys, lid)


@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_revision_supersede_self_anchor_transfer_over_blob(env, blob, tname, capsys):
    """`_supersede_impl` self-anchor transfer: the NEW person's blob is
    replaced by a dict carrying is_self (supersede has no warning field)."""
    server, conn, setmeta = env
    old = _person(server, "Zorblax Quentin")
    new = _person(server, "Zorblax Quentin Jr")
    setmeta(old, json.dumps({"name": "Zorblax Quentin", "is_self": True}))
    setmeta(new, blob)
    capsys.readouterr()
    r = _call(server.engram_supersede, old_node_id=old, new_node_id=new,
              supersede_reason="renamed")
    assert "error" not in r, r
    assert _stored(conn, new) == {"is_self": True}
    assert "warning" not in r and "warnings" not in r, r
    assert_logged_once(capsys, new, tname, "engram_supersede")


def test_revision_supersede_self_anchor_transfer_dict_blob_unchanged(env, capsys):
    server, conn, setmeta = env
    old = _person(server, "Zorblax Quentin")
    new = _person(server, "Zorblax Quentin Jr")
    setmeta(old, json.dumps({"name": "Zorblax Quentin", "is_self": True}))
    before = _stored(conn, new)
    capsys.readouterr()
    r = _call(server.engram_supersede, old_node_id=old, new_node_id=new,
              supersede_reason="renamed")
    assert "error" not in r, r
    assert _stored(conn, new) == {**before, "is_self": True}
    assert_not_logged(capsys, new)


# ---- tasks ----------------------------------------------------------------

def _task(server, description="Check the quarry season table", **kw):
    r = _call(server.engram_add_task, description=description, **kw)
    assert "error" not in r, r
    return _idof(r, "task_id")


def _goal(server):
    r = _call(server.engram_add_goal,
              claim="Memory value is measured by reasoning change, not accumulation.",
              motivation="Wire implementations to this goal as they land.")
    assert "error" not in r, r
    return r.get("goal_id") or r.get("node_id")


@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_tasks_add_task_parent_promotion_blob(env, blob, tname, capsys):
    """`_add_task_impl` parent-task scope promotion (parse, set scope=
    milestone, re-dump). The response has no `warnings` field."""
    server, conn, setmeta = env
    parent = _task(server, "Parent task")
    setmeta(parent, blob)
    capsys.readouterr()
    r = _call(server.engram_add_task, description="Child task",
              parent_task_id=parent)
    assert r.get("status") == "created", r
    assert "warnings" not in r, r
    assert _stored(conn, parent) == {"scope": "milestone"}
    assert_logged_once(capsys, parent, tname, "engram_add_task")


def test_tasks_add_task_parent_promotion_dict_blob_unchanged(env, capsys):
    server, conn, setmeta = env
    parent = _task(server, "Parent task")
    before = _stored(conn, parent)
    capsys.readouterr()
    r = _call(server.engram_add_task, description="Child task",
              parent_task_id=parent)
    assert r.get("status") == "created", r
    assert _stored(conn, parent) == {**before, "scope": "milestone"}
    assert_not_logged(capsys, parent)


@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_tasks_update_task_blob(env, blob, tname, capsys):
    """`_update_task_impl` always re-dumps its metadata (status_history on a
    note). A non-object blob is replaced by a dict holding the history entry;
    no `warnings` field on the response."""
    server, conn, setmeta = env
    tid = _task(server)
    setmeta(tid, blob)
    capsys.readouterr()
    r = _call(server.engram_update_task, task_id=tid, new_status="active",
              note="starting")
    assert r.get("status") == "updated", r
    assert r["scope"] == "routine"
    assert "warnings" not in r, r
    m = _stored(conn, tid)
    assert isinstance(m, dict)
    assert [h["note"] for h in m["status_history"]] == ["starting"]
    assert_logged_once(capsys, tid, tname, "engram_update_task")


def test_tasks_update_task_dict_blob_unchanged(env, capsys):
    server, conn, setmeta = env
    tid = _task(server, scope="milestone")
    before = _stored(conn, tid)
    capsys.readouterr()
    r = _call(server.engram_update_task, task_id=tid, new_status="active",
              note="starting")
    assert r["scope"] == "milestone", r
    m = _stored(conn, tid)
    assert {k: v for k, v in m.items() if k != "status_history"} == before
    assert [h["note"] for h in m["status_history"]] == ["starting"]
    assert_not_logged(capsys, tid)


@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_tasks_deactivate_goal_blob(env, blob, tname, capsys):
    """`_deactivate_goal_impl` always writes lifecycle_state=paused back; a
    non-object blob is replaced by a dict. No `warnings` field."""
    server, conn, setmeta = env
    gid = _goal(server)
    setmeta(gid, blob)
    capsys.readouterr()
    r = _call(server.engram_deactivate_goal, goal_id=gid, reason="parked")
    assert r.get("status") == "deactivated", r
    assert "warnings" not in r, r
    m = _stored(conn, gid)
    assert isinstance(m, dict)
    assert m["lifecycle_state"] == "paused" and m["deactivation_reason"] == "parked"
    assert_logged_once(capsys, gid, tname, "engram_deactivate_goal")


def test_tasks_deactivate_goal_dict_blob_unchanged(env, capsys):
    server, conn, setmeta = env
    gid = _goal(server)
    before = _stored(conn, gid)
    capsys.readouterr()
    r = _call(server.engram_deactivate_goal, goal_id=gid, reason="parked")
    assert r.get("status") == "deactivated", r
    m = _stored(conn, gid)
    assert {k: v for k, v in m.items()
            if k not in ("lifecycle_state", "deactivated_at",
                         "deactivation_reason")} == before
    assert_not_logged(capsys, gid)


@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_tasks_activate_goal_blob_refuses_and_replaces_nothing(
        env, blob, tname, capsys):
    """`_activate_goal_impl` only writes when the goal is already paused. A
    non-object blob reads as {} (not paused), so the call refuses with the
    same error a never-deactivated goal gets, the blob is left as stored, and
    nothing is logged (nothing is replaced)."""
    server, conn, setmeta = env
    gid = _goal(server)
    setmeta(gid, blob)
    capsys.readouterr()
    r = _call(server.engram_activate_goal, goal_id=gid, reason="resume")
    assert r == {"error": "goal is not deactivated"}, r
    assert conn.execute("SELECT metadata FROM nodes WHERE id = ?",
                        (gid,)).fetchone()["metadata"] == blob
    assert_not_logged(capsys, gid)


def test_tasks_activate_goal_dict_blob_unchanged(env, capsys):
    server, conn, setmeta = env
    gid = _goal(server)
    assert _call(server.engram_deactivate_goal, goal_id=gid)["status"] == "deactivated"
    capsys.readouterr()
    r = _call(server.engram_activate_goal, goal_id=gid, reason="resume")
    assert r.get("status") == "activated", r
    m = _stored(conn, gid)
    assert m["lifecycle_state"] == "active" and m["reactivation_reason"] == "resume"
    assert_not_logged(capsys, gid)


# ---- trust ----------------------------------------------------------------

@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_trust_update_person_blob(env, blob, tname, capsys):
    """`_update_person_impl` person write (field_traces + the field). The
    response has no `warnings` field, so stderr is the signal."""
    server, conn, setmeta = env
    pid = _person(server)
    setmeta(pid, blob)
    capsys.readouterr()
    r = _call(server.engram_update_person, node_id=pid,
              updates={"pronouns": "they/them"})
    assert r.get("status") == "updated", r
    assert "warnings" not in r, r
    m = _stored(conn, pid)
    assert isinstance(m, dict) and m["pronouns"] == "they/them"
    assert m["field_traces"]["pronouns"][0]["prior_value"] is None
    assert_logged_once(capsys, pid, tname, "engram_update_person")


@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_trust_update_person_citing_node_blob(env, blob, tname, capsys):
    """`_update_person_impl` field_changed_flags write on each CITING node
    (here an observation linked `about` the person): a non-object blob on the
    citing node is replaced by a dict holding the flag, and logged naming the
    CITING node."""
    server, conn, setmeta = env
    pid = _person(server)
    oid = _obs(server, "cn")
    lr = _call(server.engram_link_about, node_id=oid, person_id=pid)
    assert "error" not in lr, lr
    setmeta(oid, blob)
    capsys.readouterr()
    r = _call(server.engram_update_person, node_id=pid,
              updates={"pronouns": "they/them"})
    assert r.get("status") == "updated", r
    assert oid in r["cascade_flagged_nodes"], r
    m = _stored(conn, oid)
    assert isinstance(m, dict)
    assert [f["field"] for f in m["field_changed_flags"]] == ["pronouns"]
    assert_logged_once(capsys, oid, tname, "engram_update_person")


def test_trust_update_person_dict_blobs_unchanged(env, capsys):
    server, conn, setmeta = env
    pid = _person(server)
    oid = _obs(server, "cd")
    _call(server.engram_link_about, node_id=oid, person_id=pid)
    pbefore, obefore = _stored(conn, pid), _stored(conn, oid)
    capsys.readouterr()
    r = _call(server.engram_update_person, node_id=pid,
              updates={"pronouns": "they/them"})
    assert r.get("status") == "updated" and oid in r["cascade_flagged_nodes"], r
    pm, om = _stored(conn, pid), _stored(conn, oid)
    skip = ("pronouns", "field_traces")
    assert {k: v for k, v in pm.items() if k not in skip} == {
        k: v for k, v in pbefore.items() if k not in skip}
    assert {k: v for k, v in om.items() if k != "field_changed_flags"} == obefore
    assert_not_logged(capsys, pid)
    assert_not_logged(capsys, oid)


@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_trust_add_special_moment_blob(env, blob, tname, capsys):
    """`_add_special_moment_impl`: the moment is appended and the metadata
    re-dumped; a non-object blob is replaced by a dict holding it. No
    `warnings` field on the response."""
    server, conn, setmeta = env
    pid = _person(server)
    oid = _obs(server, "sm")
    setmeta(pid, blob)
    capsys.readouterr()
    r = _call(server.engram_add_special_moment, person_id=pid,
              description="Shared a long debugging night.", node_id=oid)
    assert r.get("status") == "added", r
    assert "warnings" not in r, r
    m = _stored(conn, pid)
    assert isinstance(m, dict)
    assert [x["node_id"] for x in m["special_moments"]] == [oid]
    assert_logged_once(capsys, pid, tname, "engram_add_special_moment")


def test_trust_add_special_moment_dict_blob_unchanged(env, capsys):
    server, conn, setmeta = env
    pid = _person(server)
    oid = _obs(server, "sd")
    before = _stored(conn, pid)
    capsys.readouterr()
    r = _call(server.engram_add_special_moment, person_id=pid,
              description="Shared a long debugging night.", node_id=oid)
    assert r.get("status") == "added", r
    m = _stored(conn, pid)
    assert {k: v for k, v in m.items() if k != "special_moments"} == {
        k: v for k, v in before.items() if k != "special_moments"}
    assert [x["node_id"] for x in m["special_moments"]] == [oid]
    assert_not_logged(capsys, pid)


@pytest.mark.parametrize("blob,tname", NON_OBJECT_BLOBS)
def test_trust_set_person_lineage_blob(env, blob, tname, capsys):
    """`_set_person_lineage_impl`: model_* fields are written and the metadata
    re-dumped; a non-object blob is replaced by a dict. No `warnings` field."""
    server, conn, setmeta = env
    pid = _person(server)
    setmeta(pid, blob)
    capsys.readouterr()
    r = _call(server.engram_set_person_lineage, target_pn=pid,
              model_id="claude-sonnet-4-6")
    assert r.get("status") == "ok", r
    assert "warnings" not in r, r
    m = _stored(conn, pid)
    assert isinstance(m, dict)
    assert (m["model_provider"], m["model_family"]) == (
        r["model_provider"], r["model_family"])
    assert_logged_once(capsys, pid, tname, "engram_set_person_lineage")


def test_trust_set_person_lineage_dict_blob_unchanged(env, capsys):
    server, conn, setmeta = env
    pid = _person(server)
    before = _stored(conn, pid)
    capsys.readouterr()
    r = _call(server.engram_set_person_lineage, target_pn=pid,
              model_id="claude-sonnet-4-6")
    assert r.get("status") == "ok", r
    m = _stored(conn, pid)
    assert {k: v for k, v in m.items()
            if not k.startswith("model_")} == before
    assert_not_logged(capsys, pid)
