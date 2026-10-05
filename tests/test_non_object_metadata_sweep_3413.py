"""End-to-end sweep: a valid-JSON non-object `nodes.metadata` blob must not
crash the READ-ONLY metadata sites routed through `_metadata_dict` (#3413,
folds in #3424).

Contract (written before the routing):
  S1 for each of `[1,2]`, `"str"`, `5`, `null` stored in nodes.metadata, the
     real tool call that reaches a routed read site returns normally; it
     neither raises nor answers with a Python-exception-shaped error.
  S2 sites with no real caller (a snapshot worker, the cache rebuilds, a
     one-time backfill) are covered by a DIRECT call, and each such cell says
     so in its name or docstring ("direct").
  S3 the WRITE paths (parse, mutate, re-dump) are covered by
     tests/test_non_object_metadata_write_sweep_3413.py (a non-object blob is
     replaced by a dict, logged and warned). The `retract` of a person cell
     below was a strict xfail until `_retract_impl`'s write was routed; it is
     now a plain passing cell.

Fixture style follows test_inspect_recall_non_object_metadata_3413.py. Red on
dev, green with `_metadata_dict` routed.
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
    d = tempfile.mkdtemp(prefix="engram_test_nonobj_sweep_")
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


def _derive(server, premises):
    r = _call(
        server.engram_derive,
        claim="Coastal output shifts seasonally across both sectors.",
        supporting_ids=",".join(premises),
        logical_chain="Both series move with the season, so output is seasonal.",
        reasoning_type="inductive_generalization",
    )
    return _idof(r, "derivation_id")


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


def _cornerstone(server):
    r = _call(
        server.engram_add_cornerstone,
        tag="sweep-axis", title="Check the season before reading output",
        new_frame="Season first, quarry figure second.",
        prior_frame="Quarry figure first.",
        triggering_experience="A spring rise read as a trend.",
    )
    return _idof(r, "cornerstone_id")


def _task(server):
    r = _call(server.engram_add_task, description="Check the quarry season table")
    return _idof(r, "task_id")


def _assert_diagnose_ok(r):
    """engram_diagnose swallows some per-section failures into
    `{"<section>": {"error": ...}}` instead of raising, so a top-level
    `error` check alone is blind to them: check the two sections that read
    metadata (#3424) explicitly."""
    assert "error" not in r, r
    for section in ("calibration", "principle_coverage"):
        assert "error" not in r.get(section, {}), (section, r.get(section))


# ---- engram_stats / engram_diagnose (#3424) -------------------------------

@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_diagnose_derivation_blob(env, blob):
    """engram_stats.py `_diagnose_impl` reasoning_types (and the calibration
    corpus distribution)."""
    server, conn, setmeta = env
    a = _obs(server, "da")
    b = _obs(server, "db", "Bravo kelp harvest yields fell in winter.")
    did = _derive(server, [a, b])
    setmeta(did, blob)
    r = json.loads(server.engram_diagnose())
    _assert_diagnose_ok(r)


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_diagnose_observation_blob(env, blob):
    """`_compute_confidence_distribution` by_source_class (observation rows)."""
    server, conn, setmeta = env
    oid = _obs(server, "do")
    setmeta(oid, blob)
    r = json.loads(server.engram_diagnose())
    _assert_diagnose_ok(r)


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_diagnose_lesson_blob(env, blob):
    """`_diagnose_impl` principle coverage `meta.get("situation_pattern")`."""
    server, conn, setmeta = env
    lid, _ = _lesson(server, "dl")
    setmeta(lid, blob)
    r = json.loads(server.engram_diagnose())
    _assert_diagnose_ok(r)


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_stats_default_sections_blob(env, blob):
    """`_compute_confidence_distribution` (derivation + observation rows)
    reached through the real engram_stats call."""
    server, conn, setmeta = env
    a = _obs(server, "sa")
    b = _obs(server, "sb", "Bravo kelp harvest yields fell in winter.")
    did = _derive(server, [a, b])
    setmeta(did, blob)
    setmeta(a, blob)
    r = _call(server.engram_stats)
    assert "error" not in r, r


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_stats_reasoning_type_breakdown_blob(env, blob):
    """`_stats_impl` reasoning_type_breakdown section."""
    server, conn, setmeta = env
    a = _obs(server, "ra")
    b = _obs(server, "rb", "Bravo kelp harvest yields fell in winter.")
    did = _derive(server, [a, b])
    setmeta(did, blob)
    r = _call(server.engram_stats, sections=["reasoning_breakdown"])
    assert "error" not in r, r
    assert "reasoning_type_breakdown" in r, r


# ---- engram_reflect (lifecycle) -------------------------------------------

@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_reflect_task_blob(env, blob):
    server, conn, setmeta = env
    setmeta(_task(server), blob)
    r = _call(server.engram_reflect)
    assert "error" not in r, r


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_reflect_person_blob(env, blob):
    server, conn, setmeta = env
    setmeta(_person(server), blob)
    r = _call(server.engram_reflect)
    assert "error" not in r, r


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_reflect_lesson_blob(env, blob):
    server, conn, setmeta = env
    lid, _ = _lesson(server, "rl")
    setmeta(lid, blob)
    r = _call(server.engram_reflect)
    assert "error" not in r, r


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_reflect_retracted_node_blob(env, blob):
    server, conn, setmeta = env
    oid = _obs(server, "rr")
    rr = _call(
        server.engram_retract, node_id=oid, error_type="other",
        reason="test retraction", replacement_absent_because="NONE_BECAUSE_NO_SUCCESSOR",
    )
    assert "error" not in rr, rr
    setmeta(oid, blob)
    r = _call(server.engram_reflect)
    assert "error" not in r, r


# ---- _generate_snapshot (direct: the snapshot worker path) -----------------

def _snapshot(conn):
    import engram_core
    return engram_core._generate_snapshot(conn)


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_snapshot_direct_definition_blob(env, blob):
    """DIRECT call to _generate_snapshot (no tool reaches it; the snapshot
    worker / commit path does)."""
    server, conn, setmeta = env
    r = _call(server.engram_add_definition, term="Gneiss", definition="A banded metamorphic rock.")
    setmeta(_idof(r, "definition_id"), blob)
    assert "Definition:" in _snapshot(conn)


# NOTE: `_generate_snapshot`'s `elif ntype == "task"` branch (core.py) is dead
# code: "task" is not in the function's `type_order`, so no node ever reaches
# it, by a real call or a direct one. The branch is routed through
# `_metadata_dict` anyway (same shape as its siblings) but no cell can cover
# it; the drift guard covers it by construction.


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_snapshot_direct_person_blob(env, blob):
    """DIRECT call to _generate_snapshot, person branch."""
    server, conn, setmeta = env
    setmeta(_person(server), blob)
    assert "Person:" in _snapshot(conn)


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_snapshot_direct_lesson_blob(env, blob):
    """DIRECT call to _generate_snapshot, lesson branch."""
    server, conn, setmeta = env
    lid, _ = _lesson(server, "sn")
    setmeta(lid, blob)
    assert lid in _snapshot(conn)


# ---- confidence recompute (cascade acknowledge) ---------------------------

@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_cascade_acknowledge_recompute_blob(env, blob):
    """`_recompute_node_confidence` meta read, via engram_cascade_resolve
    (acknowledged) on a derivation whose premise was superseded."""
    server, conn, setmeta = env
    ob = _obs(server, "ca1")
    ob2 = _obs(server, "ca2", "Refined kelp harvest yields fell in winter.")
    dv = _derive(server, [ob])
    sup = _call(server.engram_supersede, old_node_id=ob, new_node_id=ob2,
                supersede_reason="refined")
    assert "error" not in sup, sup
    setmeta(dv, blob)
    r = _call(server.engram_cascade_resolve, node_id=dv,
              resolution_kind="acknowledged", note="refinement does not touch the claim")
    assert "error" not in r, r


# ---- derive: _validate_premises -------------------------------------------

@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_derive_over_retracted_premise_blob(env, blob):
    """`_validate_premises` reads the retracted premise's metadata for the
    refusal's retraction_reason_excerpt. The call must REFUSE (tainted), not
    raise."""
    server, conn, setmeta = env
    a = _obs(server, "va")
    b = _obs(server, "vb", "Bravo kelp harvest yields fell in winter.")
    rr = _call(
        server.engram_retract, node_id=a, error_type="other",
        reason="test retraction", replacement_absent_because="NONE_BECAUSE_NO_SUCCESSOR",
    )
    assert "error" not in rr, rr
    setmeta(a, blob)
    r = _call(
        server.engram_derive,
        claim="Coastal output shifts seasonally across both sectors.",
        supporting_ids=f"{a},{b}",
        logical_chain="Both series move with the season, so output is seasonal.",
        reasoning_type="inductive_generalization",
    )
    assert "error" in r, r
    assert "retract" in r["error"].lower() or "tainted" in r["error"].lower(), r


# ---- cornerstone ----------------------------------------------------------

@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_outgrow_cornerstone_blob(env, blob):
    server, conn, setmeta = env
    cs = _cornerstone(server)
    setmeta(cs, blob)
    r = _call(server.engram_outgrow_cornerstone, old_cornerstone_id=cs,
              new_new_frame="Season first, then compare years.")
    assert "error" not in r, r


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_link_about_person_blob(env, blob):
    server, conn, setmeta = env
    pid = _person(server)
    oid = _obs(server, "la")
    setmeta(pid, blob)
    r = _call(server.engram_link_about, node_id=oid, person_id=pid)
    assert "error" not in r, r


# ---- observation ----------------------------------------------------------

@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_add_observation_person_mention_blob(env, blob):
    """`_add_observation_impl` person-mention scan (observation.py ~2240).

    The scan sits in a best-effort `except Exception`, so a non-object blob
    does not fail the call; it SILENTLY ends the scan, and a well-formed
    person node scanned after it loses its mention. So the cell asserts the
    good person's `person_mentions` entry survives a bad sibling scanned
    first."""
    server, conn, setmeta = env
    bad = _person(server, "Aaron Bell")
    good = _person(server, "Zorblax Quentin")
    setmeta(bad, blob)
    r = _call(
        server.engram_add_observation,
        url="https://example.com/pm", title="Src pm",
        quoted_text="Zorblax Quentin measured the granite.",
        interpretation="Zorblax Quentin measured the granite output.",
        claim="Zorblax Quentin measured granite output in spring.",
        quote_type="hard_data",
    )
    assert "observation_id" in r, r
    assert [m["person_id"] for m in r.get("person_mentions", [])] == [good], r


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_standpoint_author_repair_hint_blob(env, blob):
    """`_find_person_repair_hint` (observation.py ~230): a display-name
    standpoint_author_id is rejected with a repair hint scan over person
    nodes. The call must REJECT with the display-name message, not raise."""
    server, conn, setmeta = env
    pid = _person(server, "Zorblax Quentin")
    setmeta(pid, blob)
    r = _call(
        server.engram_add_observation,
        url="https://example.com/sa", title="Src sa",
        quoted_text="sa verbatim quote text.",
        interpretation="sa interpretation text here.",
        claim="sa granite quarry output rose in spring.",
        quote_type="hard_data",
        standpoint_author_id="Zorblax Quentin",
    )
    assert "error" in r and "display name" in r["error"], r


# ---- trust ---------------------------------------------------------------

@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_set_trust_tier_self_blob(env, blob):
    """`_set_trust_tier_impl` tier=self gate read: refuses (no is_self), not
    raises."""
    server, conn, setmeta = env
    pid = _person(server, "Zorblax Quentin")
    setmeta(pid, blob)
    r = _call(server.engram_set_trust_tier, target_pn=pid, tier="self")
    assert "error" in r and "is_self" in r["error"], r


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_backfill_special_moment_edges_direct_blob(env, blob):
    """DIRECT call: `_backfill_special_moment_about_edges` is a one-time
    backfill (tools/engram-backfill-special-moment-edges.py), not a tool."""
    server, conn, setmeta = env
    import engram_trust
    setmeta(_person(server), blob)
    assert engram_trust._backfill_special_moment_about_edges(conn) == 0


# ---- revision ------------------------------------------------------------

@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_supersede_person_blob(env, blob):
    """`_supersede_impl` self-anchor-transfer read (revision.py ~2536)."""
    server, conn, setmeta = env
    old = _person(server, "Zorblax Quentin")
    new = _person(server, "Zorblax Quentin Jr")
    setmeta(old, blob)
    r = _call(server.engram_supersede, old_node_id=old, new_node_id=new,
              supersede_reason="renamed")
    assert "error" not in r, r


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_retract_person_blob(env, blob):
    server, conn, setmeta = env
    pid = _person(server)
    setmeta(pid, blob)
    r = _call(server.engram_retract, node_id=pid, error_type="other",
              reason="test retraction", replacement_absent_because="NONE_BECAUSE_NO_SUCCESSOR")
    assert "error" not in r, r


# ---- core cache rebuilds (direct) -----------------------------------------

@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_rebuild_incidents_cache_direct_blob(env, blob):
    """DIRECT call: `_rebuild_incidents_cache` lesson-metadata read."""
    server, conn, setmeta = env
    import engram_core
    lid, _ = _lesson(server, "ri")
    setmeta(lid, blob)
    r = engram_core._rebuild_incidents_cache()
    assert "error" not in r, r


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_rebuild_cornerstone_anchors_direct_blob(env, blob):
    """DIRECT call: `_rebuild_cornerstone_anchors_cache` anchor_line read."""
    server, conn, setmeta = env
    import engram_core
    cs = _cornerstone(server)
    ex = _obs(server, "rc")
    rr = _call(server.engram_register_exemplar, target_id=cs, exemplar_id=ex)
    assert "error" not in rr, rr
    setmeta(cs, blob)
    r = engram_core._rebuild_cornerstone_anchors_cache()
    assert "error" not in r, r


@pytest.mark.parametrize("blob", NON_OBJECT_BLOBS)
def test_rebuild_principle_triggers_direct_blob(env, blob):
    """DIRECT call: `_rebuild_principle_triggers` nudge read."""
    server, conn, setmeta = env
    import engram_core
    lid, _ = _lesson(server, "rp")
    setmeta(lid, blob)
    r = engram_core._rebuild_principle_triggers()
    assert "error" not in r, r
