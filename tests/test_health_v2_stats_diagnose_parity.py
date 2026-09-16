"""Stats <-> diagnose health-score parity, re-pointed at health-v2
(engram-alpha#2940).

Replaces ``test_stats_and_diagnose_health_score_parity`` from the now-deleted
``tests/test_health_score_pinning.py``. That file pinned the single-source
property against the OLD pre-v2 penalty formula
(``engram_stats.py``'s now-retired hand-rolled penalty function), which
engram-alpha#2940 retired
entirely in favor of health-v2's own composed score
(``health/engine.py``) as the single source of truth for
``engram_stats``/``engram_diagnose``/the legacy viz ``/api/health`` endpoint.

The single-source property itself is still a real invariant worth guarding:
both surfaces must report the identical number, and now the identical
SNAPSHOT (not just a coincidentally-equal number) -- "both read
``health.engine.read_latest_composed_summary()``" replaces "both call
the old formula function" as the mechanism the parity pin is protecting.

There is NO live-compute path for health-v2 -- it is per-checkpoint only --
so this test writes a real checkpoint via ``health.engine.write_snapshot``
before calling either surface, mirroring how a real nap/sleep checkpoint
populates the snapshot both tools then read back.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_health_v2_parity_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Return a freshly initialised server module with an empty test DB."""
    td = tempfile.mkdtemp(dir=TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(td)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _get_db_path(server) -> str:
    import engram_core
    return str(engram_core.DB_PATH)


def _seed_observation(server, claim: str, quote_type: str = "hard_data") -> str:
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/test",
        "title": "Test Article",
        "quoted_text": f"Supporting quote for: {claim}",
        "interpretation": f"Interpretation of: {claim}",
        "claim": claim,
        "quote_type": quote_type,
        "source_class": "external",
    })))
    assert r.get("status") == "created", f"obs creation failed: {r}"
    return r["observation_id"]


def _seed_derivation(server, claim: str, supporting_ids: list[str]) -> str:
    r = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": claim,
        "supporting_ids": ",".join(supporting_ids),
        "logical_chain": f"Chain for: {claim}",
        "reasoning_type": "deductive_modus_ponens",
    })))
    assert "derivation_id" in r, f"derivation creation failed: {r}"
    return r["derivation_id"]


def test_stats_and_diagnose_health_v2_parity():
    """engram_stats and engram_diagnose must report the identical
    health_score AND read the identical snapshot, once a real health-v2
    checkpoint exists.

    The single-source property is now "both read
    health.engine.read_latest_composed_summary()" -- pinning
    health_score_detail's snapshot_id equality (not just the numeric score)
    guards against a future regression where the two surfaces happen to
    agree on a number while actually reading two different snapshot files.
    """
    server = fresh_server()
    ob1 = _seed_observation(server, "Parity-graph observation A")
    ob2 = _seed_observation(server, "Parity-graph observation B")
    _seed_derivation(server, "Parity-graph derivation from A and B", [ob1, ob2])

    from health import engine as health_engine
    import engram_core

    conn = engram_core._get_db()
    try:
        health_engine.write_snapshot(conn, "nap")
    finally:
        conn.close()

    stats_result = json.loads(
        server.engram_stats(json.dumps({"sections": ["health_score"]}))
    )
    diag_result = json.loads(server.engram_diagnose())

    diag_health_score = (
        diag_result["health_score"]
        if "health_score" in diag_result
        else diag_result["metrics"]["health_score"]
    )
    diag_health_score_detail = (
        diag_result["health_score_detail"]
        if "health_score_detail" in diag_result
        else diag_result["metrics"]["health_score_detail"]
    )

    assert stats_result["health_score"] is not None, (
        f"expected a real health_score after write_snapshot: {stats_result}"
    )
    assert stats_result["health_score"] == diag_health_score, (
        f"stats ({stats_result['health_score']}) and diagnose "
        f"({diag_health_score}) must report the same health_score -- "
        "single source of truth violated"
    )
    assert (
        stats_result["health_score_detail"]["snapshot_id"]
        == diag_health_score_detail["snapshot_id"]
    ), (
        "stats and diagnose must read the SAME snapshot file, not just "
        "coincidentally agree on a number: "
        f"stats snapshot_id={stats_result['health_score_detail']['snapshot_id']!r} "
        f"diagnose snapshot_id={diag_health_score_detail['snapshot_id']!r}"
    )


def test_stats_and_diagnose_health_score_none_not_string_on_empty_graph():
    """A genuinely empty graph (no claim-bearing nodes at all) checkpoints
    with EVERY health-v2 aspect held out -- health/engine.py's
    compose_aspect_scores then returns the sentinel string "UNAVAILABLE"
    for scores["composed"] (points_in_play <= 0). read_latest_composed_summary()
    must translate that into score: None, never leak the literal string
    "UNAVAILABLE" as health_score -- reviewer-fairy round 1 blocker on
    engram-alpha#2940, reproduced directly: an empty-graph checkpoint fed
    unguarded straight into engram_stats/engram_diagnose's health_score.

    has_snapshot stays True here (the checkpoint file genuinely exists) --
    this is a DIFFERENT case from test_get_health_data_health_score_no_snapshot_yet_reports_null_not_zero-
    style "no checkpoint ever ran" -- so it needs its own coverage.
    """
    server = fresh_server()
    # Deliberately NO observations/derivations seeded -- the empty-graph case.

    from health import engine as health_engine
    import engram_core

    conn = engram_core._get_db()
    try:
        snap = health_engine.build_snapshot(conn, "nap")
        assert snap["scores"]["composed"] == "UNAVAILABLE", (
            "test precondition failed -- an empty graph no longer composes "
            f"to the UNAVAILABLE sentinel (got {snap['scores']['composed']!r}); "
            "this test's whole premise needs re-checking against the current "
            "health/engine.py aspect-composition rules before trusting the "
            "assertions below"
        )
        health_engine.write_snapshot(conn, "nap")
    finally:
        conn.close()

    stats_result = json.loads(
        server.engram_stats(json.dumps({"sections": ["health_score"]}))
    )
    diag_result = json.loads(server.engram_diagnose())

    for label, result in [("stats", stats_result), ("diagnose", diag_result)]:
        assert result["health_score"] is None, (
            f"{label}: health_score must be None (never the literal string "
            f"'UNAVAILABLE') on an empty-graph checkpoint, got "
            f"{result['health_score']!r}"
        )
        assert isinstance(result["health_score_detail"]["unscored_aspects"], list), (
            f"{label}: unscored_aspects must still be a real list explaining "
            f"the None, got {result['health_score_detail']}"
        )
        assert len(result["health_score_detail"]["unscored_aspects"]) > 0, (
            f"{label}: unscored_aspects must name at least one held-out "
            f"aspect when score is None, got {result['health_score_detail']}"
        )


def test_latest_snapshot_filename_breaks_same_turn_ties_by_timestamp():
    """engram-alpha#2940 round 3 (reviewer-fairy blocker on round 3's OWN
    fix, round 3.5): direct unit test of
    health.engine._latest_snapshot_filename's tie-break, the sibling of the
    identical fix in viz/data/health_v2.py (see that module's own test for
    the twin). Turn only advances at sleep, so multiple same-turn
    checkpoints (naps) in one day is the normal case -- comparing turn
    alone left ties resolved by whichever file os.listdir() happened to
    return first, not the genuinely latest one.

    The FIRST version of this test (round 3) wrote the correct-answer file
    to disk first and the wrong-answer file second, intending real
    filesystem write order to stand in for "processed order" -- but a
    round-3 reviewer-fairy round PROVED that test non-discriminating by
    reverting the production fix and re-running it unmodified: it still
    passed, because on macOS/APFS os.listdir() returns entries in creation
    order, which coincidentally matches the OLD buggy "first-encountered-
    wins" tie-break for THIS specific write order (write correct-answer
    first -> old code also keeps it, for the wrong reason). Fixed by
    mocking os.listdir() directly to return an EXPLICIT, adversarial
    order -- decoupled from any real filesystem's enumeration behavior --
    so the test can only pass if the comparison genuinely reads the
    timestamp, not by accident of write/iteration order."""
    from unittest import mock

    from health import engine as health_engine

    # The wrong-answer name is listed FIRST (the position the old
    # first-wins tie-break would keep); the correct-answer name is listed
    # SECOND. Old code: sees wrong-answer first (best_name set), sees
    # correct-answer second, turn is EQUAL so `turn > best_turn` is False
    # -- does NOT update -- returns the wrong answer. New code: compares
    # (turn, timestamp) tuples regardless of iteration order -- returns
    # the correct answer.
    fake_names = [
        "7-nap-20260904T090000Z.json",          # wrong answer, listed first
        "7-pre_sleep-20260904T235900Z.json",    # correct answer, listed second
    ]
    with mock.patch("health.engine.os.listdir", return_value=fake_names):
        picked = health_engine._latest_snapshot_filename("/fake/snapshots/dir")
    assert picked == "7-pre_sleep-20260904T235900Z.json", (
        f"expected the later-timestamped same-turn file to win, got {picked!r}"
    )


def test_latest_snapshot_filename_breaks_equal_second_ties_by_mtime():
    """engram-alpha#2940 round 3.6 (reviewer-fairy round on Mira's OWN
    colleague review, her exact-head repro reproduced in miniature here):
    write_snapshot() stamps the filename's timestamp token at SECOND
    resolution only, and checkpoint_mode is a separate filename component
    -- so two DIFFERENT-mode checkpoints (e.g. nap and pre_sleep) genuinely
    written within the same wall-clock second share an identical
    (turn, ts_token) pair. Round 3.5's fix stopped one level too early:
    that tie fell through to whichever file os.listdir() happened to
    return first, exactly the same underlying defect one level deeper.
    Fixed by adding the file's own mtime_ns (nanosecond resolution) as a
    further tiebreaker.

    Mocks BOTH os.listdir (adversarial order: the WRONG, earlier-mtime
    file listed first) AND os.stat (explicit mtimes per filename) -- the
    same discipline as the sibling test above, so this test cannot pass by
    accident of real filesystem timing or enumeration order."""
    from unittest import mock

    from health import engine as health_engine

    # Same turn AND same ts_token -- the (turn, ts_token) tuple alone
    # cannot discriminate these; only mtime_ns can.
    older_name = "7-nap-20260904T145100Z.json"
    newer_name = "7-pre_sleep-20260904T145100Z.json"
    fake_names = [older_name, newer_name]  # correct (newer) answer listed SECOND

    mtimes = {older_name: 1_000_000_000, newer_name: 2_000_000_000}

    def fake_stat(path):
        import os as _os

        name = _os.path.basename(str(path))
        return mock.Mock(st_mtime_ns=mtimes[name])

    with mock.patch("health.engine.os.listdir", return_value=fake_names), mock.patch(
        "health.engine.os.stat", side_effect=fake_stat
    ):
        picked = health_engine._latest_snapshot_filename("/fake/snapshots/dir")
    assert picked == newer_name, (
        f"expected the later-mtime same-(turn,timestamp) file to win, got {picked!r}"
    )


def test_stats_read_does_not_create_health_snapshots_directory():
    """engram-alpha#2940 round 3 (reviewer-fairy blocker): engram_stats and
    engram_diagnose are declared side-effect-free / readOnlyHint=True, but
    read_latest_composed_summary()'s default path previously called
    _snapshot_dir() (the WRITE-path helper, which unconditionally
    mkdir+chmods) -- so a plain health_score READ on a fresh graph with no
    checkpoint ever run silently CREATED health-snapshots/ on disk, and
    made read_latest_composed_summary's own "no directory yet" branch
    unreachable (the directory had just been created by the very call
    checking for its absence). Confirms the fix: no checkpoint written,
    call stats/diagnose, assert the directory still does not exist."""
    server = fresh_server()
    import engram_core

    snapshots_dir = os.path.join(os.path.dirname(str(engram_core.DB_PATH)), "health-snapshots")
    assert not os.path.isdir(snapshots_dir), (
        "test precondition failed -- health-snapshots/ already exists before any read"
    )

    stats_result = json.loads(
        server.engram_stats(json.dumps({"sections": ["health_score"]}))
    )
    assert stats_result["health_score"] is None, stats_result
    assert not os.path.isdir(snapshots_dir), (
        "engram_stats (declared side-effect-free) must not create "
        "health-snapshots/ as a side effect of reading health_score"
    )

    diag_result = json.loads(server.engram_diagnose())
    assert diag_result["health_score"] is None, diag_result
    assert not os.path.isdir(snapshots_dir), (
        "engram_diagnose (declared side-effect-free) must not create "
        "health-snapshots/ as a side effect of reading health_score"
    )
