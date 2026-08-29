"""Tests for tests/cascade_playbox.py — the cascade playbox harness (#2356).

Coverage (T1-T9, each named for what it would catch — see the build spec):
  T1 single-instance: entering a second box while one is live raises, and
     the message names both topologies.
  T2 guard survives the purge (the C3 test): a mid-box `fresh_module()` call
     on an `engram_*` module must NOT reset the playbox's own single-instance
     guard, because the guard lives outside the `engram_*` purge predicate by
     construction (the module is named `cascade_playbox`, not
     `engram_playbox`).
  T3 exception safety: an exception inside the `with` block still clears the
     module-level `_LIVE` slot, and a subsequent box opens fine.
  T4 drive-not-fake: a resolve through `box.resolve()` produces the same
     marker state as calling `_cascade_resolve_impl` directly on the same
     fixture shape.
  T5 derived-not-literal: `box.edges()` with no argument defaults to exactly
     `engram_core.CASCADE_RELATIONS` — asserted against the constant itself,
     never a hardcoded list.
  T6 topologies: each of the five named topologies builds, and its shape is
     asserted structurally (edge count + every minted handle touched by at
     least one edge), never by id. `mutual_crossing` additionally gets its
     own shape-specific check (its exact crossing edges + which nodes land
     on the frontier) since it is the one topology the spec pins precisely.
  T7 failed-enter cleanup (round-1 review, #2373): a setup failure inside
     `__enter__` — AFTER the temp dir already exists — must not leak that
     temp dir, and a subsequent box must still open fine (the `_LIVE` half of
     this was already covered by T6's unknown-topology case; T7 adds the
     temp-dir assertion T6 never made).
  T8 `box.cycles()` coverage (round-1 review, #2373): a cycle-free graph
     returns falsy, and a graph with a cycle over the DERIVED
     `engram_core.CASCADE_RELATIONS` set is detected. The cycle fixture is
     built by inserting the closing edge directly (never through
     `box.supersede()`/`box.resolve()`) — the real cascade machine enforces
     a DAG invariant and refuses to close a cascade cycle through the public
     API (the refusal is on the `acknowledged_derives_from` edge, at
     acknowledgement-write time — asserted not here but in
     `tests/test_cascade_resolve.py`, in
     `test_acknowledgement_cannot_close_a_cascade_cycle`; see also the
     ruling above T8's second test), and whether the public API can be
     driven to one anyway is the open question `mutual_crossing`
     deliberately leaves unanswered (see its builder's docstring). T8 tests
     `cycles()` itself, as a generic detector over whatever the edges table
     holds — it does not touch `mutual_crossing` and answers nothing about
     public-API reachability.
     Per the method's own docstring it is a back-edge detector, not an
     exhaustive elementary-cycle enumerator — these tests assert detection,
     never enumeration completeness.
  T9 isolation (round-2 review, #2373): the box's configured data dir IS its
     own temp dir. Nothing else in this file binds those two together, so a
     dropped or reordered `_configure_paths()` call in `__enter__` would
     leave all fifteen other tests green — and, on any seat that EXPORTS
     `ENGRAM_HOME`, writing into the developer's real graph. That hazard is
     conditional, not universal (`conftest.py` nets an unset one); T9's own
     docstring carries why.

Import-pattern contract (#948 wave — R1): `cascade_playbox.py` is a plain
support module, not itself a `test_*.py` file, so
`tools/run_touched_tests.py`'s static selection graph only walks
`tests/test_*.py` files (see that file's own docstring). This file therefore
carries its own top-level imports of every family module the playbox touches
so a change to any of them still selects this test file, even though the
actual calls are mediated through `cascade_playbox.Playbox`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from conftest import fresh_module  # noqa: E402 — must follow sys.path setup

import engram_core  # noqa: E402
import engram_revision  # noqa: E402
import engram_observation  # noqa: E402
import engram_derive  # noqa: E402
import engram_conformance  # noqa: E402

import cascade_playbox as playbox_module  # noqa: E402
from cascade_playbox import cascade_playbox, PlayboxAlreadyLiveError  # noqa: E402


# ---------------------------------------------------------------------------
# T1 — single-instance guard
# ---------------------------------------------------------------------------


def test_t1_single_instance_guard_names_both_topologies():
    with cascade_playbox(topology="chain") as outer:
        assert outer.topology == "chain"
        with pytest.raises(PlayboxAlreadyLiveError) as exc_info:
            with cascade_playbox(topology="diamond"):
                pass  # pragma: no cover - must not be reached
        err = exc_info.value
        assert err.live_topology == "chain"
        assert err.attempted_topology == "diamond"
        assert "chain" in str(err)
        assert "diamond" in str(err)

    # The outer box's own exit must have released the guard — a fresh box
    # opens fine afterward.
    with cascade_playbox(topology="diamond") as fresh:
        assert fresh.handles


# ---------------------------------------------------------------------------
# T2 — guard survives an engram_* purge (the C3 test)
# ---------------------------------------------------------------------------


def test_t2_guard_survives_engram_star_purge():
    """The trap this harness exists to avoid: a playbox module named
    `engram_playbox` would have its own `_LIVE` slot deleted by the very
    `fresh_module()` purge it must call on entry. Prove the guard survives a
    purge fired from OUTSIDE the box too — simulating an adjacent test (or
    fixture) calling `fresh_module()` on some `engram_*` module while a
    playbox is still live."""
    with cascade_playbox(topology="chain"):
        # Simulate the exact purge every sibling test in this suite performs.
        fresh_module(engram_core)
        with pytest.raises(PlayboxAlreadyLiveError):
            with cascade_playbox(topology="diamond"):
                pass  # pragma: no cover - must not be reached


# ---------------------------------------------------------------------------
# T3 — exception safety
# ---------------------------------------------------------------------------


class _PlayboxTestBoom(Exception):
    pass


def test_t3_exception_inside_with_block_clears_live_slot():
    with pytest.raises(_PlayboxTestBoom):
        with cascade_playbox(topology="chain"):
            raise _PlayboxTestBoom("boom")

    assert playbox_module._LIVE is None, (
        "an exception inside the `with` block must still release the guard"
    )

    # A subsequent box opens fine.
    with cascade_playbox(topology="diamond") as box:
        assert box.handles


# ---------------------------------------------------------------------------
# T4 — drive, not fake
# ---------------------------------------------------------------------------


def _strip_nondeterministic_marker_fields(markers: list) -> list:
    """Drop the fields that are legitimately different across two separate
    (freshly-minted-id) databases: raw ids, timestamps, and the cascade
    event id. Keep status/resolution_kind/handle-rendered fields — the
    STATE this test is actually comparing."""
    drop = {
        "id", "node_id", "root_node", "cascade_event", "created_at",
        "resolved_at", "resolution_replacement_id",
    }
    return [{k: v for k, v in m.items() if k not in drop} for m in markers]


def test_t4_resolve_matches_direct_impl_call():
    # engram_supersede is type-preserving (old and new must share a node
    # type), so this fixture needs TWO replacements: `base_repl` (an
    # observation, same type as `base`, superseding it so `child` picks up a
    # pending marker) and `child_repl` (a derivation, same type as `child`,
    # the replacement actually named in the resolve/adjudication call).

    # Path A: through box.resolve().
    with cascade_playbox() as box_a:
        base = box_a.node("base", kind="observation")
        child = box_a.node("child", kind="derivation", supports=[base])
        base_repl = box_a.node("base_repl", kind="observation")
        child_repl = box_a.node("child_repl", kind="derivation", supports=[base_repl])
        sup = box_a.supersede(base, base_repl)
        assert "error" not in sup, sup
        resolved = box_a.resolve(child, "superseded", replacement=child_repl)
        assert "error" not in resolved, resolved
        markers_a = _strip_nondeterministic_marker_fields(box_a.markers(child))

    # Path B: identical fixture shape, resolved by calling the impl directly
    # (not through box.resolve()) — the control this test exists to run.
    with cascade_playbox() as box_b:
        base = box_b.node("base", kind="observation")
        child = box_b.node("child", kind="derivation", supports=[base])
        base_repl = box_b.node("base_repl", kind="observation")
        child_repl = box_b.node("child_repl", kind="derivation", supports=[base_repl])
        sup = box_b.supersede(base, base_repl)
        assert "error" not in sup, sup
        direct = json.loads(box_b._rev._cascade_resolve_impl(
            node_id=box_b.id_of(child),
            resolution_kind="superseded",
            replacement_id=box_b.id_of(child_repl),
        ))
        assert "error" not in direct, direct
        markers_b = _strip_nondeterministic_marker_fields(box_b.markers(child))

    assert markers_a == markers_b
    assert markers_a, "expected at least one resolved marker on `child`"
    assert markers_a[0]["status"] == "resolved"
    assert markers_a[0]["resolution_kind"] == "superseded"


# ---------------------------------------------------------------------------
# T5 — derived, not literal
# ---------------------------------------------------------------------------


def test_t5_edges_default_matches_cascade_relations_constant():
    with cascade_playbox(topology="diamond") as box:
        default_edges = box.edges()
        explicit_edges = box.edges(relations=box._core.CASCADE_RELATIONS)
        assert default_edges == explicit_edges

        # Every relation actually returned must be a member of the live
        # constant — asserted by membership in the constant object itself,
        # never against a hardcoded copy of its current contents (a stale
        # copy would silently desync from a future CASCADE_RELATIONS edit).
        assert default_edges, "expected the diamond topology to carry cascade edges"
        for e in default_edges:
            assert e["relation"] in box._core.CASCADE_RELATIONS


# ---------------------------------------------------------------------------
# T6 — named topologies build and are structurally sound
# ---------------------------------------------------------------------------


_EXPECTED_EDGE_COUNTS = {
    "chain": 3,
    "diamond": 4,
    "triple_diamond": 6,
    "nested_diamond": 7,
    "mutual_crossing": 6,
}


@pytest.mark.parametrize("name", sorted(_EXPECTED_EDGE_COUNTS))
def test_t6_topology_builds_and_is_structurally_sound(name):
    with cascade_playbox(topology=name) as box:
        edges = box.edges()
        assert len(edges) == _EXPECTED_EDGE_COUNTS[name], (name, edges)

        # Every handle the builder minted is touched by at least one edge —
        # no orphan node silently left out of the shape.
        touched = set()
        for e in edges:
            touched.add(e["source_handle"])
            touched.add(e["target_handle"])
        assert touched == set(box.handles.keys()), (
            name, touched, set(box.handles.keys())
        )

        # Reachability: at least one node in the topology has no outgoing
        # cascade edge of its own — a pure-premise root (the observation(s)
        # nothing derives_from further) — proving the shape is actually
        # rooted rather than a floating cycle of derivations. (`edges()`
        # rendering source/target back to a handle name for every id also
        # proves every id round-trips through the handle map — a dangling
        # raw id would surface as `None` here, not a KeyError, but the
        # `touched == box.handles.keys()` check above already forces every
        # handle name to be non-None.)
        sources = {e["source_handle"] for e in edges}
        roots = touched - sources  # nothing derives FROM these (pure premises)
        assert roots, (name, "expected at least one premise root", edges)
        assert roots <= {"base", "base_a", "base_b", "root"}, (name, roots)


def test_t6_mutual_crossing_exact_shape():
    with cascade_playbox(topology="mutual_crossing") as box:
        edge_pairs = {(e["source_handle"], e["target_handle"]) for e in box.edges()}
        assert edge_pairs == {
            ("prem_a", "root"),
            ("prem_b", "root"),
            ("x1", "prem_a"),
            ("x2", "prem_b"),
            ("a_prime", "x2"),
            ("b_prime", "x1"),
        }, edge_pairs

        # x1 and x2 each carry a pending marker from their respective
        # supersede and sit on the frontier — the setup this topology exists
        # to produce, WITHOUT this test adjudicating them (that question is
        # explicitly out of scope — see the topology builder's docstring).
        frontier = box.frontier()
        frontier_ids = {n["id"] for n in frontier["frontier"]}
        assert box.id_of(box["x1"]) in frontier_ids
        assert box.id_of(box["x2"]) in frontier_ids
        # a_prime / b_prime are the REPLACEMENTS, not pending nodes — they
        # must not themselves appear on the frontier.
        assert box.id_of(box["a_prime"]) not in frontier_ids
        assert box.id_of(box["b_prime"]) not in frontier_ids


def test_t6_unknown_topology_raises():
    with pytest.raises(ValueError):
        with cascade_playbox(topology="not-a-real-topology"):
            pass  # pragma: no cover - must not be reached

    # A failed __enter__() must still release the guard.
    assert playbox_module._LIVE is None
    with cascade_playbox(topology="chain") as box:
        assert box.handles


# ---------------------------------------------------------------------------
# T7 — a failed __enter__() must not leak its temp dir
# ---------------------------------------------------------------------------


def test_t7_failed_enter_removes_temp_dir():
    """`_build_topology("not-a-real-topology")` — the same failure T6 already
    exercises — raises AFTER `tempfile.mkdtemp()` has already run, so
    `__exit__` (which would normally remove the temp dir) never fires: the
    `with` statement's context was never entered. `box.__enter__()` is called
    directly (not via `with`) so the box reference survives the raise and its
    `_tmpdir` path can be inspected afterward."""
    box = cascade_playbox(topology="not-a-real-topology")
    with pytest.raises(ValueError):
        box.__enter__()

    assert box._tmpdir is not None, (
        "expected __enter__ to have created a temp dir before its "
        "_build_topology() call failed"
    )
    assert not os.path.exists(box._tmpdir), (
        f"a failed __enter__() must not leak its temp dir: {box._tmpdir!r} "
        "still exists"
    )
    assert playbox_module._LIVE is None

    # A subsequent box still opens fine.
    with cascade_playbox(topology="chain") as fresh:
        assert fresh.handles


# ---------------------------------------------------------------------------
# T8 — box.cycles()
# ---------------------------------------------------------------------------


def test_t8_cycles_empty_on_acyclic_topology():
    with cascade_playbox(topology="diamond") as box:
        assert not box.cycles()


# Colleague ruling on the direct-SQL insert below (ariadne-calvino,
# colleague review on #2373):
# the machine's refusal to build a cycle is the feature, not friction this
# fixture routes around. TWO distinct guards refuse it, and naming the
# right one matters: §12.3 refuses at SUPERSEDE time
# (`supersede_replacement_is_downstream`, in `_supersede_impl`) — a
# replacement may not already derive from the node it replaces; §12.7
# refuses at ACKNOWLEDGEMENT-WRITE time — the refusal DECISION is raised
# inside `_cascade_record_acknowledgement_edges` (`raise
# _CascadeCycleError`), while the guard string a caller actually reads,
# `acknowledgement_would_close_cascade_cycle`, is emitted by
# `_cascade_resolve_impl`'s `except` handler. §12.7 is the one that fires on
# the shape this harness can reach: driven at `mutual_crossing`, BOTH
# supersedes are ACCEPTED and the SECOND acknowledgement is refused, on the
# `acknowledged_derives_from` edge. §12.7 exists precisely because §12.3 is
# a point-in-time check — evaluated over the edge table as it stood at
# supersede time — while every acknowledgement written afterwards adds an
# edge to the very relation §12.3 was evaluated over, so two individually
# legal supersedes plus two acknowledgements can still close a cycle. (The
# full argument, and why the supersede-guard-only proof does not carry, is
# in `_cascade_record_acknowledgement_edges`'s docstring under "§12.4's
# ORIGINAL PROOF, AND WHY IT WAS INCOMPLETE".)
#
# So a cycle is a state the public API is DESIGNED not to produce — design
# intent, not a general reachability result, and therefore not in tension
# with the module docstring's open question of whether the API can be driven
# to one anyway. That question asks what has been SHOWN, and what has been
# shown is ONE topology refused at its second acknowledgement: evidence the
# guards hold where they have been exercised, not proof that no path
# reaches a cycle. The open question stays open.
#
# Where that refusal is ASSERTED — since it is not asserted HERE, and the
# sentence above would otherwise rest on an ad-hoc run that leaves no
# trace: nothing in THIS file drives `mutual_crossing` through
# `resolve()`. The standing assertion lives in
# `tests/test_cascade_resolve.py`, in
# `test_acknowledgement_cannot_close_a_cascade_cycle` — a property test
# over §12.7's write-time guard on a structurally equivalent shape,
# deriving its relation set from CASCADE_RELATIONS rather than hardcoding
# the one cycle we thought of. Read the guarantee there; do not take it
# from this paragraph.
#
# That leaves exactly two options: drive the detector only through states
# the constructor permits (its positive path never runs, and it ships as a
# detector nobody has watched detect anything) or build the forbidden state
# directly. Hence the SQL, deliberately — to give the detector a positive
# control.
#
# The discriminator, load-bearing: direct SQL in a test is legitimate
# exactly when it builds a state the public API FORBIDS, and illegitimate
# when it builds a state the API PERMITS but is merely tedious to reach.
# The first is the only way to test a detector against what it detects;
# the second bypasses the verbs under test and turns a topology fixture
# into an assertion about the fixture rather than about the machine. The
# rule is about the STATE, not the SQL — the two look identical in a diff.
#
# `box.id_of()` below is licensed by the same discriminator, and needs
# saying because the Handle docstring rules it out for fixture
# construction. That rule governs fixtures built through the playbox's
# verbs, where an id is an invitation to hand-build what the verbs would
# have built properly. This INSERT is the other case: no verb will build
# the forbidden state, so the ids have to come from somewhere. Same
# licence, equally narrow — an `id_of()` call reaching for a state the API
# PERMITS is the breach the docstring means.
#
# Do not let this idiom migrate into a topology fixture: the playbox
# exists to make unreachable states inexpressible through its own API,
# and a topology built this way would defeat that.
def test_t8_cycles_detects_cycle_over_cascade_relations_constant():
    # Three plain nodes, minted through the real canonical tools, chained
    # a -> b -> c (b, c derive from their predecessor) — acyclic so far.
    with cascade_playbox() as box:
        a = box.node("a", kind="observation")
        b = box.node("b", kind="derivation", supports=[a])
        c = box.node("c", kind="derivation", supports=[b])
        assert not box.cycles()

        # Close the loop by inserting the edge directly — NOT through
        # box.supersede()/box.resolve(), which drive the real cascade
        # machine and enforce a DAG invariant that would refuse this edge.
        # cycles() is a generic DFS back-edge detector over whatever the
        # edges table holds; this constructs a minimal fixture for that
        # detector alone, never through (or about) the public API. The
        # existing edges are b->a (b derives_from a) and c->b (c derives_from
        # b); adding a->c closes the loop a->c->b->a.
        # `[0]` here is `acknowledged_derives_from`, not the `derives_from`
        # the edges above carry — and either works, because `cycles()` walks
        # CASCADE_RELATIONS as one relation-agnostic edge set. Kept derived
        # from the constant rather than hardcoded, per T5's discipline.
        rel = sorted(box._core.CASCADE_RELATIONS)[0]
        conn = box._core._get_db()
        try:
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at) "
                "VALUES (?, ?, ?, ?)",
                (box.id_of(a), box.id_of(c), rel, box._core._now()),
            )
            conn.commit()
        finally:
            conn.close()

        cycles = box.cycles()
        assert cycles, "expected at least one cycle to be detected"
        for cyc in cycles:
            # Detection, not enumeration completeness (the method's own
            # docstring disclaims exhaustiveness) — assert every reported
            # cycle is confined to the fixture's own three handles and
            # closes on itself, nothing stronger.
            assert set(cyc) <= {"a", "b", "c"}, (cyc, cycles)
            assert cyc[0] == cyc[-1], (cyc, cycles)


# ---------------------------------------------------------------------------
# T9 — isolation: the box's data dir is its own temp dir
# ---------------------------------------------------------------------------


def test_t9_box_data_dir_is_inside_its_tempdir():
    """The axis no other test in this file binds. `__enter__` calls
    `engram_core._configure_paths(self._tmpdir)` exactly once; without it
    `DATA_DIR` keeps its module-load default — `$ENGRAM_HOME` or
    `~/.engram`. Whether that default is harmless or catastrophic is
    CONDITIONAL, so state the condition rather than assume a seat: a
    process that reaches this suite with `ENGRAM_HOME` UNSET is given a
    throwaway home by `conftest.py`, and a dropped `_configure_paths()`
    lands in a scratch tempdir. But that net is a set-if-absent — a seat
    that EXPORTS `ENGRAM_HOME` (the self-hosting clone `conftest.py`'s own
    comment describes) still wins, and THERE the module-load default is the
    developer's REAL graph. Drop or reorder that single line and every
    other test here still passes — nothing else in this file binds the two
    together — while, on those seats, writing into production;
    `_configure_paths`'s own docstring memorialises exactly that incident.
    Hence asserted as a property and deliberately never mutation-tested by
    removing the call: on the seats where it matters, the probe writes to
    the real graph precisely when it "works"."""
    with cascade_playbox(topology="chain") as box:
        tmpdir = Path(box._tmpdir).resolve()
        data_dir = Path(box._core.DATA_DIR).resolve()
        db_path = Path(box._core.DB_PATH).resolve()

        assert data_dir == tmpdir, (
            f"the box's DATA_DIR must be its own temp dir: "
            f"{data_dir} != {tmpdir}"
        )
        assert db_path.parent == tmpdir, (
            f"the box's DB_PATH must sit inside its temp dir: {db_path} "
            f"is not under {tmpdir}"
        )
        assert db_path.exists(), f"expected a live database at {db_path}"

        # Named explicitly, since it is the whole point: not the default the
        # box overrode.
        default_db = Path(
            os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram")
        ) / "knowledge.db"
        assert db_path != default_db.resolve(), (
            f"the box is writing to the default ENGRAM home ({db_path}) — "
            "_configure_paths() did not take effect"
        )


if __name__ == "__main__":
    import traceback

    _tests = [
        (name, obj) for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    passed = 0
    for name, fn in _tests:
        try:
            if name == "test_t6_topology_builds_and_is_structurally_sound":
                for topo in sorted(_EXPECTED_EDGE_COUNTS):
                    fn(topo)
            else:
                fn()
            print(f"PASS  {name}")
            passed += 1
        except Exception:
            print(f"FAIL  {name}")
            traceback.print_exc()
    print(f"\n{passed}/{len(_tests)} passed")
