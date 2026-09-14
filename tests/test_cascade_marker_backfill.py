"""Migration rung 5 — backfill cascade_markers from the retired
metadata.stale_by / metadata.tainted_by fields (#2324), plus the guard
semantics that ruling brought with it.

WHY THE BACKFILL EXISTS AT ALL. #2009 moves every cascade reader off
metadata onto the cascade_markers table. Ship that without a backfill and
any graph upgraded from before #2009 has a guard consulting a table with no
rows for its legacy taint — so a derivation that is REFUSED today would go
through, silently, in the dangerous direction. Measured on the author's live
graph when the spec was written: 30 current nodes carrying stale_by (35
markers, five of them two-root), every one of them blocked today.

Two things this file deliberately does NOT do:
  - It does not assert a count taken from the code's own bookkeeping. Every
    expectation here is written out by hand from the fixture, so the test
    disagrees with the implementation rather than echoing it.
  - It does not test the anomaly path only through a positive assertion. The
    author's real graph has zero dangling roots, so a fixture is the ONLY
    place that branch can run; it gets an explicit case.
"""

import atexit
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile

# #2565: append (not insert(0)) -- putting src/engram at position 0
# outranks the repo-root tools/ package for the rest of the process,
# a live instance of the tools/-package-name-clash class found while
# building #2565's durable guard (tests/conftest.py).
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "engram"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from conftest import fresh_module  # noqa: E402

import engram_core  # noqa: E402
import engram_derive  # noqa: E402
import engram_observation  # noqa: E402
import engram_revision  # noqa: E402

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_marker_backfill_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _fresh(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    core = fresh_module(engram_core)
    core._configure_paths(d)
    core._ensure_data_dir()
    return core, d


def _mods():
    """Reload the family modules against one shared TEST_DIR."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    rev = fresh_module(engram_revision)
    obs = importlib.import_module("engram_observation")
    derive = importlib.import_module("engram_derive")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return rev, obs, derive, core


def _mk_obs(obs, claim):
    r = json.loads(obs._add_observation_impl(
        url="https://example.com/t", title="T", claim=claim,
        quoted_text=claim, interpretation=f"obs: {claim}",
        quote_type="hard_data",
    ))
    assert "observation_id" in r, r
    return r["observation_id"]


# ---------------------------------------------------------------------------
# A. The migration itself
# ---------------------------------------------------------------------------

_NODE_SQL = (
    "INSERT INTO nodes (id, type, created_at, claim, status, is_current, metadata) "
    "VALUES (?, 'observation', '2026-01-01T00:00:00+00:00', ?, ?, ?, ?)"
)


def _build_pre_migration_graph(core):
    """A graph in the exact shape rung 5 must handle, rewound to v4.

    Every row here is a case the spec calls out by name, so a reader can map
    fixture -> clause without re-deriving the population rule.
    """
    conn = core._get_db()
    rows = [
        # roots (targets of the legacy references) — must EXIST so the
        # dangling case below is the only dangling one.
        ("ob_root_s", "superseded root", "active", 0, "{}"),
        ("ob_root_t", "retracted root", "retracted", 0, "{}"),
        ("ob_root_s2", "second superseded root", "active", 0, "{}"),
        # §2 population — current nodes carrying legacy keys.
        ("ob_stale_one", "current, one stale root", "active", 1,
         json.dumps({"stale_by": ["ob_root_s"]})),
        ("ob_taint_one", "current, one taint root", "active", 1,
         json.dumps({"tainted_by": ["ob_root_t"]})),
        # invariant 9 — per-EVENT rows, so two roots means two markers.
        ("ob_stale_two", "current, two stale roots", "active", 1,
         json.dumps({"stale_by": ["ob_root_s", "ob_root_s2"]})),
        # §4 — dangling root. Still gets a BLOCKING marker, flagged.
        ("ob_dangling", "current, root absent from graph", "active", 1,
         json.dumps({"stale_by": ["zz_never_existed"]})),
        # §2 — non-current nodes are excluded (already uncitable).
        ("ob_noncurrent", "superseded, carries a legacy key", "active", 0,
         json.dumps({"stale_by": ["ob_root_s"]})),
        # The empty-list trap: has the KEY, has no roots. Not a population
        # member — a substring test on the metadata blob would wrongly count
        # it, and the §5 gate would then fail on a node needing no marker.
        ("ob_empty_list", "current, empty stale_by", "active", 1,
         json.dumps({"stale_by": []})),
        # A current node with unrelated metadata — must be untouched.
        ("ob_clean", "current, no legacy keys", "active", 1,
         json.dumps({"warrant": "unrelated"})),
    ]
    for r in rows:
        conn.execute(_NODE_SQL, r)
    # Originating events for two of them, so the 'recovered' timestamp path
    # runs alongside the 'migration_time' fallback in the same run.
    conn.execute(
        "INSERT INTO edit_history (timestamp, turn, action, node_id, node_type, details) "
        "VALUES ('2026-02-02T03:04:05+00:00', 1, 'stale_flagged', 'ob_stale_one', "
        "'observation', ?)",
        (json.dumps({"stale_premise": "ob_root_s", "replacement": "ob_root_s2"}),),
    )
    conn.execute(
        "INSERT INTO edit_history (timestamp, turn, action, node_id, node_type, details) "
        "VALUES ('2026-03-03T04:05:06+00:00', 1, 'tainted', 'ob_taint_one', "
        "'observation', ?)",
        (json.dumps({"tainted_by": "ob_root_t"}),),
    )
    conn.execute("DELETE FROM cascade_markers")
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()
    core._db_setup_done_paths.clear()


def _markers(core):
    conn = core._get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT node_id, cascade_event, root_node, root_op, status, created_at, "
        "backfill_provenance, backfill_anomaly FROM cascade_markers "
        "ORDER BY node_id, cascade_event"
    )]
    conn.close()
    return rows


def test_backfill_marks_exactly_the_current_legacy_population():
    core, d = _fresh("engram_test_bf_pop_")
    try:
        _build_pre_migration_graph(core)
        core._get_db().close()  # triggers the ladder

        got = {(m["node_id"], m["root_node"], m["root_op"]) for m in _markers(core)}
        # Written out by hand from the fixture, NOT read back from the code.
        assert got == {
            ("ob_stale_one", "ob_root_s", "supersede"),
            ("ob_taint_one", "ob_root_t", "retract"),
            ("ob_stale_two", "ob_root_s", "supersede"),
            ("ob_stale_two", "ob_root_s2", "supersede"),
            ("ob_dangling", "zz_never_existed", "supersede"),
        }, got

        by_node = {}
        for m in _markers(core):
            by_node.setdefault(m["node_id"], []).append(m)

        # Non-current excluded; empty-list and clean nodes untouched.
        for absent in ("ob_noncurrent", "ob_empty_list", "ob_clean"):
            assert absent not in by_node, (absent, by_node.keys())

        # invariant 9 — two roots, two DISTINCT events on one node.
        two = by_node["ob_stale_two"]
        assert len(two) == 2 and len({m["cascade_event"] for m in two}) == 2, two

        # §4 — the dangling entry blocks anyway, and says so.
        dang = by_node["ob_dangling"][0]
        assert dang["backfill_anomaly"] == 1, dang
        assert dang["status"] == "pending", dang

        # §3 — a recovered timestamp is the ORIGINAL, not the migration clock.
        rec = by_node["ob_stale_one"][0]
        assert rec["backfill_provenance"] == "recovered", rec
        assert rec["created_at"] == "2026-02-02T03:04:05+00:00", rec
        tai = by_node["ob_taint_one"][0]
        assert tai["backfill_provenance"] == "recovered", tai
        assert tai["created_at"] == "2026-03-03T04:05:06+00:00", tai

        # ...and one with no recoverable event is stamped honestly rather
        # than wearing an original's face.
        unre = by_node["ob_dangling"][0]
        assert unre["backfill_provenance"] == "migration_time", unre

        # Every backfilled row is distinguishable from a live-cascade one.
        assert all(m["backfill_provenance"] is not None for m in _markers(core))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_backfill_is_idempotent():
    """§6 — re-running the rung is a no-op, not a duplicate-row generator."""
    core, d = _fresh("engram_test_bf_idem_")
    try:
        _build_pre_migration_graph(core)
        core._get_db().close()
        first = _markers(core)

        # Rewind ONLY the version gate — the table keeps its rows, which is
        # exactly the state a crash-then-retry leaves behind.
        conn = core._get_db()
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        conn.close()
        core._db_setup_done_paths.clear()
        core._get_db().close()

        assert _markers(core) == first
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_count_gate_rolls_back_and_leaves_the_version_for_retry():
    """§5 — when the table does not end up holding what the legacy side says
    it should, the migration FAILS LOUDLY and retries, never half-applies.

    The injection is synthetic and says so: a pre-existing row on the exact
    (node_id, cascade_event) key with backfill_provenance NULL. `INSERT OR
    IGNORE` then drops the real backfill row, and the gate — which counts
    only rows it can prove it wrote — sees the node missing. That is the
    same shape as any real cause of an insert not landing, exercised through
    the gate's genuine code path rather than by patching the gate.
    """
    core, d = _fresh("engram_test_bf_gate_")
    try:
        _build_pre_migration_graph(core)
        # RAW connection on purpose: core._get_db() runs the ladder, which
        # would fire rung 5 before the injection landed and leave nothing to
        # catch. This is the same trap the fixture builder already works
        # around, and it bit this test first.
        raw = sqlite3.connect(str(core.DB_PATH))
        raw.execute(
            "INSERT INTO cascade_markers (node_id, cascade_event, root_node, "
            "root_op, status, created_at) VALUES "
            "('ob_stale_one', 'backfill:supersede:ob_root_s', 'ob_root_s', "
            "'supersede', 'pending', '2026-01-01T00:00:00+00:00')"
        )
        raw.execute("PRAGMA user_version = 4")
        raw.commit()
        raw.close()
        core._db_setup_done_paths.clear()

        core._get_db().close()

        conn = core._get_db()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == 4, (
            f"a failed count gate must leave the version un-bumped so the "
            f"rung retries on the next startup, got {version}"
        )
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_a_failed_rung_is_not_leapfrogged_by_its_successor():
    """The ladder must be a LADDER. Rungs are independent `if` blocks gated
    on a version read once at the top, so before #2324 a rung that failed
    (deliberately leaving its version un-bumped, to retry) did not stop the
    rungs after it — and the NEXT rung's bump made the failed one's retry
    gate permanently false. Silent, and permanent.

    Adding rung 5 is what made this reachable; the guard covers rungs 1-5.
    """
    core, d = _fresh("engram_test_bf_ladder_")
    try:
        conn = core._get_db()
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()
        core._db_setup_done_paths.clear()

        # Break rung 3 only.
        import engram_backup  # noqa: F401
        real = core._rung_ready

        def fake(c, target, ok):
            if target == 3:
                return False   # rung 3 does not run -> version stays at 2
            return real(c, target, ok)

        core._rung_ready = fake
        try:
            core._get_db().close()
        finally:
            core._rung_ready = real

        conn = core._get_db()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == 2, (
            f"rungs 4 and 5 must NOT run over a rung 3 that did not land — "
            f"bumping past it would make its own retry gate unreachable "
            f"forever. got user_version={version}"
        )
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# B. What the backfilled markers DO — the ruling of 2026-08-28
# ---------------------------------------------------------------------------


def test_backfilled_markers_of_both_kinds_hard_block_a_derivation():
    """The ruling: backfilled markers hard-block IMMEDIATELY, both root_ops,
    with no use_stale compatibility. Clause 7 of the original spec (preserve
    the soft/overridable behaviour for supersede-rooted markers) was
    overruled.
    """
    rev, obs, derive, core = _mods()
    prem_s = _mk_obs(obs, "Premise reached by a supersede cascade.")
    prem_t = _mk_obs(obs, "Premise reached by a retract cascade.")
    other = _mk_obs(obs, "An unrelated clean premise.")
    root = _mk_obs(obs, "The cascade root.")

    conn = core._get_db()
    for node, op in ((prem_s, "supersede"), (prem_t, "retract")):
        conn.execute(
            "INSERT INTO cascade_markers (node_id, cascade_event, root_node, "
            "root_op, status, created_at, backfill_provenance) VALUES "
            f"(?, 'backfill:{op}:' || ?, ?, ?, 'pending', ?, 'migration_time')",
            (node, root, root, op, core._now()),
        )
    conn.commit()
    conn.close()

    # supersede-rooted: hard, and use_stale=True changes nothing.
    r = json.loads(derive._derive_impl(
        claim="Deriving over a supersede-rooted backfilled marker.",
        supporting_ids=f"{prem_s},{other}",
        logical_chain="Should not be permitted.",
        reasoning_type="deductive_modus_ponens",
        use_stale=True,
    ))
    assert r.get("status") == "blocked_stale", r
    assert "derivation_id" not in r, r
    assert "use_stale_retired" in r and "RETIRED" in r["use_stale_retired"], r

    # retract-rooted: hard, as it always was.
    r2 = json.loads(derive._derive_impl(
        claim="Deriving over a retract-rooted backfilled marker.",
        supporting_ids=f"{prem_t},{other}",
        logical_chain="Should not be permitted.",
        reasoning_type="deductive_modus_ponens",
        use_stale=True,
    ))
    assert r2.get("status") == "blocked_tainted", r2
    assert "derivation_id" not in r2, r2


def test_refusal_names_the_route_for_each_case():
    """Stage-1.3 rider (a): the refusal names the RESOLUTION ROUTE, and the
    two stale cases have different ones. Case 1 (carries a marker) resolves
    by superseding/retracting the premise; case 2 (is itself superseded)
    resolves by citing the successor — which needs no machine at all.
    """
    rev, obs, derive, core = _mods()
    root = _mk_obs(obs, "Root of an earlier cascade.")
    marked = _mk_obs(obs, "Premise carrying a marker.")
    other = _mk_obs(obs, "Clean premise.")
    conn = core._get_db()
    conn.execute(
        "INSERT INTO cascade_markers (node_id, cascade_event, root_node, "
        "root_op, status, created_at, backfill_provenance) VALUES "
        "(?, 'backfill:supersede:x', ?, 'supersede', 'pending', ?, 'migration_time')",
        (marked, root, core._now()),
    )
    conn.commit()
    conn.close()

    r1 = json.loads(derive._derive_impl(
        claim="Case 1.", supporting_ids=f"{marked},{other}",
        logical_chain="c", reasoning_type="deductive_modus_ponens",
    ))
    assert r1.get("status") == "blocked_stale", r1
    cases = {e["case"]: e for p in r1["stale_premises"] for e in p["stale_because"]}
    assert "cascade_marker" in cases, r1
    route1 = cases["cascade_marker"]["route"]
    assert marked in route1 and "frontier" in route1, route1

    # Case 2 — the premise IS the superseded node. No marker is written for
    # it (the walker seeds it into `visited`), so this path is reached only
    # by the is_current/superseded_by check, not by the marker read.
    old = _mk_obs(obs, "A claim that will be refined.")
    new = _mk_obs(obs, "The refined claim.")
    sup = json.loads(rev._supersede_impl(
        old_node_id=old, new_node_id=new, supersede_reason="refined",
    ))
    assert sup.get("status") == "superseded", sup

    conn = core._get_db()
    n = conn.execute(
        "SELECT COUNT(*) c FROM cascade_markers WHERE node_id = ?", (old,)
    ).fetchone()["c"]
    conn.close()
    assert n == 0, "a superseded node must not carry a marker for its own supersede"

    r2 = json.loads(derive._derive_impl(
        claim="Case 2.", supporting_ids=f"{old},{other}",
        logical_chain="c", reasoning_type="deductive_modus_ponens",
    ))
    assert r2.get("status") == "blocked_stale", r2
    cases2 = {e["case"]: e for p in r2["stale_premises"] for e in p["stale_because"]}
    assert "superseded_premise" in cases2, r2
    route2 = cases2["superseded_premise"]["route"]
    assert new in route2 and "cite the successor" in route2, route2


def test_refusal_cascade_marker_case_carries_machine_readable_route():
    """#2394 [part of #2013]: the MECH-5 refusal's cascade_marker entry must
    carry the SAME machine-readable route fields as the plain
    supersede/retract refusal (#2343, engram_revision.py) — next_call names
    engram_cascade_frontier, then names engram_cascade_resolve. A plain
    supersede/retract on the marked premise is refused while its markers are
    pending, so the route prose must not tell the caller to do that.
    """
    rev, obs, derive, core = _mods()
    root = _mk_obs(obs, "Root of an earlier cascade.")
    marked = _mk_obs(obs, "Premise carrying a marker.")
    other = _mk_obs(obs, "Clean premise.")
    conn = core._get_db()
    conn.execute(
        "INSERT INTO cascade_markers (node_id, cascade_event, root_node, "
        "root_op, status, created_at, backfill_provenance) VALUES "
        "(?, 'backfill:supersede:x', ?, 'supersede', 'pending', ?, 'migration_time')",
        (marked, root, core._now()),
    )
    conn.commit()
    conn.close()

    r1 = json.loads(derive._derive_impl(
        claim="Case 1, machine-readable route.", supporting_ids=f"{marked},{other}",
        logical_chain="c", reasoning_type="deductive_modus_ponens",
    ))
    assert r1.get("status") == "blocked_stale", r1
    cases = {e["case"]: e for p in r1["stale_premises"] for e in p["stale_because"]}
    entry = cases["cascade_marker"]
    assert entry["next_call"] == f"engram_cascade_frontier(node_id={marked!r})", entry
    assert "engram_cascade_resolve" in entry["then"], entry
    assert "supersede or retract" not in entry["route"], entry


def test_superseding_a_marked_node_is_refused_and_resolves_via_the_verb():
    """Design invariant 7, both sentences (#2343, Lei 2026-08-29). The PLAIN
    supersede of a marked node is REFUSED with a pointer at the frontier /
    resolve verb; the route out is engram_cascade_resolve, which resolves the
    node's own marker as 'superseded' with the replacement recorded. (Before
    the frontier verb shipped, the plain verb resolved the marker itself — a
    deliberate accommodation, retired here.)
    """
    rev, obs, derive, core = _mods()
    root = _mk_obs(obs, "Root.")
    marked = _mk_obs(obs, "Marked node.")
    replacement = _mk_obs(obs, "Its replacement.")
    other = _mk_obs(obs, "Clean premise.")
    conn = core._get_db()
    conn.execute(
        "INSERT INTO cascade_markers (node_id, cascade_event, root_node, "
        "root_op, status, created_at, backfill_provenance) VALUES "
        "(?, 'backfill:supersede:y', ?, 'supersede', 'pending', ?, 'migration_time')",
        (marked, root, core._now()),
    )
    conn.commit()
    conn.close()

    refused = json.loads(rev._supersede_impl(
        old_node_id=marked, new_node_id=replacement,
        supersede_reason="re-derived on live premises",
    ))
    assert "error" in refused and "engram_cascade_resolve" in refused["error"], refused
    assert refused["next_call"].startswith("engram_cascade_frontier("), refused
    assert marked in refused["then"] and replacement in refused["then"], refused

    sup = json.loads(rev._cascade_resolve_impl(
        node_id=marked, resolution_kind="superseded",
        replacement_id=replacement, note="re-derived on live premises",
    ))
    assert "error" not in sup, sup
    assert sup.get("resolved_marker_count") == 1, sup

    conn = core._get_db()
    row = conn.execute(
        "SELECT status, resolution_kind, resolution_replacement_id, resolved_at "
        "FROM cascade_markers WHERE node_id = ?", (marked,)
    ).fetchone()
    conn.close()
    assert row["status"] == "resolved", dict(row)
    assert row["resolution_kind"] == "superseded", dict(row)
    assert row["resolution_replacement_id"] == replacement, dict(row)
    assert row["resolved_at"] is not None, dict(row)

    # The REPLACEMENT is clean — citing it works, which is the whole point
    # of resolution existing.
    ok = json.loads(derive._derive_impl(
        claim="Now derivable from the replacement.",
        supporting_ids=f"{replacement},{other}",
        logical_chain="c", reasoning_type="deductive_modus_ponens",
    ))
    assert "derivation_id" in ok, ok


def test_supersede_resolves_markers_from_ALL_cascades_not_just_one():
    """The blast radius of _resolve_own_markers is deliberately the whole
    node, not one cascade_event — locking that in because it is the
    reasonable-looking wrong change a future maintainer might make.

    THE TEMPTING WRONG READING: "this supersede addressed cascade A, so it
    should resolve only A's marker and leave B's pending." That treats a
    marker as a record of an unaddressed CONCERN. It is not — it is a record
    that THIS NODE has not moved since the cascade reached it, and the node
    has now moved. Superseding it is not a partial answer to one upstream
    change; it replaces the claim outright, so every pending marker on it is
    answered at once by the same fact.

    Scoping resolution per-event would also strand the node permanently:
    there is no verb, before slice 4, that resolves marker B alone, so a node
    marked by two cascades could never be fully cleared no matter what its
    author did.

    Belt and braces, and worth saying because it means the choice is safe
    either way: once superseded the node has is_current=0, so any later
    citation of it is refused by the case-2 (superseded_premise) branch
    regardless of its marker state. The marker rows are the audit trail here,
    not the live gate.
    """
    rev, obs, derive, core = _mods()
    root_a = _mk_obs(obs, "Root of cascade A.")
    root_b = _mk_obs(obs, "Root of cascade B, entirely unrelated to A.")
    marked = _mk_obs(obs, "A node that two separate cascades both reached.")
    replacement = _mk_obs(obs, "Its replacement.")

    conn = core._get_db()
    for ev, root in (("backfill:supersede:A", root_a), ("backfill:supersede:B", root_b)):
        conn.execute(
            "INSERT INTO cascade_markers (node_id, cascade_event, root_node, "
            "root_op, status, created_at, backfill_provenance) VALUES "
            "(?, ?, ?, 'supersede', 'pending', ?, 'migration_time')",
            (marked, ev, root, core._now()),
        )
    conn.commit()
    pending_before = conn.execute(
        "SELECT COUNT(*) c FROM cascade_markers WHERE node_id = ? AND status = 'pending'",
        (marked,),
    ).fetchone()["c"]
    conn.close()
    assert pending_before == 2, pending_before

    refused = json.loads(rev._supersede_impl(
        old_node_id=marked, new_node_id=replacement,
        supersede_reason="re-derived; both upstream changes folded in",
    ))
    assert "error" in refused, refused   # #2343: plain verb refuses a marked node
    assert set(refused["pending_events"]) == {
        "backfill:supersede:A", "backfill:supersede:B"
    }, refused

    sup = json.loads(rev._cascade_resolve_impl(
        node_id=marked, resolution_kind="superseded",
        replacement_id=replacement, note="re-derived; both upstream changes folded in",
    ))
    assert "error" not in sup, sup
    assert sup.get("resolved_marker_count") == 2, sup
    assert set(sup["resolved_events"]) == {
        "backfill:supersede:A", "backfill:supersede:B"
    }, sup

    conn = core._get_db()
    rows = {
        r["cascade_event"]: dict(r)
        for r in conn.execute(
            "SELECT cascade_event, status, resolution_kind, "
            "resolution_replacement_id FROM cascade_markers WHERE node_id = ?",
            (marked,),
        )
    }
    conn.close()
    assert len(rows) == 2, rows
    for ev, row in rows.items():
        assert row["status"] == "resolved", (ev, row)
        assert row["resolution_kind"] == "superseded", (ev, row)
        assert row["resolution_replacement_id"] == replacement, (ev, row)


def test_retracting_a_marked_node_is_refused_and_resolves_via_the_verb():
    rev, obs, derive, core = _mods()
    root = _mk_obs(obs, "Root.")
    marked = _mk_obs(obs, "Marked node about to be withdrawn.")
    conn = core._get_db()
    conn.execute(
        "INSERT INTO cascade_markers (node_id, cascade_event, root_node, "
        "root_op, status, created_at, backfill_provenance) VALUES "
        "(?, 'backfill:retract:z', ?, 'retract', 'pending', ?, 'migration_time')",
        (marked, root, core._now()),
    )
    conn.commit()
    conn.close()

    refused = json.loads(rev._retract_impl(
        node_id=marked, error_type="wrong_evidence",
        reason="withdrawn rather than fixed",
    ))
    assert "error" in refused and "engram_cascade_resolve" in refused["error"], refused

    r = json.loads(rev._cascade_resolve_impl(
        node_id=marked, resolution_kind="retracted",
        error_type="wrong_evidence", note="withdrawn rather than fixed",
    ))
    assert "error" not in r, r
    assert r.get("resolved_marker_count") == 1, r

    conn = core._get_db()
    row = conn.execute(
        "SELECT status, resolution_kind, resolution_replacement_id "
        "FROM cascade_markers WHERE node_id = ?", (marked,)
    ).fetchone()
    conn.close()
    assert row["status"] == "resolved", dict(row)
    assert row["resolution_kind"] == "retracted", dict(row)
    # No replacement: a retracted node was withdrawn, not corrected.
    assert row["resolution_replacement_id"] is None, dict(row)


def test_dead_premises_was_vacuous_before_a_resolution_writer_existed():
    """_dead_premises filters on status='resolved' AND resolution_kind IN
    ('superseded','retracted'). Until #2324 NOTHING wrote status='resolved',
    so that predicate could never be true and the function always returned
    [] — a production read that looked like it worked and was structurally
    incapable of returning anything.

    This test is the positive control that it now CAN return something. It
    would have passed vacuously against the old code only if it asserted
    emptiness; it asserts a non-empty result, so it fails against it.
    """
    rev, obs, derive, core = _mods()
    root = _mk_obs(obs, "Root.")
    premise = _mk_obs(obs, "A premise that will be marked, then superseded.")
    other = _mk_obs(obs, "Clean premise.")
    dv = json.loads(derive._derive_impl(
        claim="Depends on the premise.", supporting_ids=f"{premise},{other}",
        logical_chain="c", reasoning_type="deductive_modus_ponens",
    ))
    assert "derivation_id" in dv, dv
    dv_id = dv["derivation_id"]

    event = "backfill:supersede:w"
    conn = core._get_db()
    for node in (premise, dv_id):
        conn.execute(
            "INSERT INTO cascade_markers (node_id, cascade_event, root_node, "
            "root_op, status, created_at, backfill_provenance) VALUES "
            "(?, ?, ?, 'supersede', 'pending', ?, 'migration_time')",
            (node, event, root, core._now()),
        )
    conn.commit()
    conn.close()

    conn = core._get_db()
    assert rev._dead_premises(conn, dv_id, event) == [], (
        "nothing is resolved yet, so nothing is dead"
    )
    conn.close()

    replacement = _mk_obs(obs, "The premise, corrected.")
    # #2343: the plain verb refuses a marked node; this test's subject is
    # _dead_premises, so use the internal resolution route to write the
    # resolution it reads.
    sup = json.loads(rev._supersede_impl(
        old_node_id=premise, new_node_id=replacement, supersede_reason="corrected",
        cascade_triggering=False,
    ))
    assert sup.get("status") == "superseded", sup

    conn = core._get_db()
    dead = rev._dead_premises(conn, dv_id, event)
    conn.close()
    assert dead == [premise], (
        f"the premise resolved as 'superseded' under {event}, so it is dead "
        f"for that event — this is the read that could never fire before a "
        f"resolution writer existed. got {dead}"
    )
