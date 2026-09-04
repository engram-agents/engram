"""Tests for the health-score engine's checkpoint wiring into
engram_lifecycle._checkpoint_internal (engram-alpha#1991, PR2 of 4).

Covers the two CONFIRMED, concretely-wired checkpoints (design doc §4.4):
  - "nap"        -> engram_nap / _nap_impl (mode == "nap")
  - "post_dream" -> engram_advance_turn / _advance_turn_impl (mode == "session")

The THIRD checkpoint ("pre_sleep") is deliberately NOT wired in this PR — see
the PR2 report's checkpoint-hook investigation findings. No test claims
pre_sleep wiring; its absence is intentional, not an oversight to catch here.

Fixture note: this file's fresh_server() extends the established
tests/test_nap_payload.py / test_advance_turn_payload.py pattern by ALSO
clearing the whole `health` package (`health` itself plus every
`health.*` submodule — engine, metrics, config, measurements, model,
curves, version) from sys.modules. These are the first modules to import
engram_core outside the `health_engine*` flat-file family this predicate
originally targeted (engram-alpha#2537 amendment 3 folded the flat
`health_engine*.py` files into a `health/` package), and the existing
fresh_server() convention's module-clearing predicate
(`key == "server" or key.startswith("engram_")`) does not sweep them —
see health/metrics.py's module docstring for why a stale cross-test
engram_core binding would otherwise be a real (not hypothetical)
contamination risk once this file exists in the same pytest process as
the pre-existing nap/advance_turn test files. The package-prefix sweep
(`key == "health" or key.startswith("health.")`) is broader than the old
`startswith("health_engine")` predicate — it now also covers
health.measurements/health.model/health.curves/health.version, which the
original predicate never named.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_health_engine_checkpoint_wiring_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _stop_leaked_snapshot_worker():
    """Stop + reset any currently-loaded engram_snapshot_worker singleton before
    tearing down TEST_DIR (alpha#2507).

    _nap_impl / _advance_turn_impl can engage the real #1673 async snapshot
    worker (a background daemon thread that regenerates knowledge.sql /
    graph_snapshot.md and git-commits into engram_core.DATA_DIR -- read fresh
    from the module global on every job, not captured once at thread start).
    conftest.py's `_stop_snapshot_worker` autouse fixture stops it too, but only
    ONCE per test FUNCTION, at the very end -- too late for a test that calls
    fresh_server() more than once (e.g. test_checkpoint_internal_never_emits_
    pre_sleep_mode, which calls it for the nap half and then again for the
    advance_turn half). Without this, the worker thread started by the FIRST
    fresh_server() call can still be mid-write into TEST_DIR (or into a
    directory it lazily mkdir's, e.g. db-backup/.pending/) when the SECOND
    fresh_server() call's shutil.rmtree(TEST_DIR) below is walking the same
    directory -- a genuine TOCTOU race, not a flake: rmtree lists a directory's
    entries then removes them, and a new entry created mid-walk raises
    OSError: [Errno 39] Directory not empty (Linux; ENOTEMPTY, errno 66 on
    macOS -- same symbolic error).

    Evidence, precisely, so the claim doesn't overreach: (1) the worker
    genuinely engages during this test -- a live run of this file logs
    "[engram snapshot-worker] job turn=0,seq=1 raised, leaving pending: no
    such table: nodes" from the background thread, confirming it is real and
    unsynchronized with the main thread, not hypothetical; (2) the general
    mechanism -- a background thread creating a new file in a directory while
    shutil.rmtree() is walking it -- was reproduced in isolation (a threaded
    writer racing shutil.rmtree() on a populated tempdir), reliably raising
    the identical "Directory not empty" error, and a join()-before-rmtree
    fix (exactly what stop_worker() below does) eliminated it across 5/5
    trials. What was NOT reproduced locally is the exact ENOTEMPTY on THIS
    test on this machine -- on macOS, with no git objects yet committed (the
    worker's own job fails before reaching the commit step), rmtree's walk is
    fast enough that a naive artificial delay in the worker overshoots the
    real race window; CI's Linux runner, mid a 707s/7666-test full-suite run,
    is evidently loaded enough to hit it. The fix closes the mechanism in the
    common case: stop_worker() joins the thread (up to 2s) before TEST_DIR is
    touched, so an in-flight write ordinarily finishes (or errors out) before
    rmtree runs. Caveat, flagged by reviewer-fairy round 1 rather than
    self-caught: join(timeout=2.0) can return with the thread still alive
    (e.g. a slow git commit inside _process_job), and the reset below doesn't
    check is_alive() afterward -- if that happens, the race isn't actually
    closed for that call. This is inherited verbatim from conftest.py's own
    _stop_snapshot_worker fixture (this helper mirrors it exactly), not a gap
    introduced here; hardening both call sites together (e.g. logging/
    asserting on a post-join is_alive()) is tracked as a follow-up rather than
    done in this test-file-only PR.
    """
    _sw = sys.modules.get("engram_snapshot_worker")
    if _sw is None:
        return
    try:
        _sw.stop_worker(timeout=2.0)
        _sw._started = False
        _sw._worker_thread = None
        _sw._seq_counter = 0
        _sw._stop.clear()
        _sw._work_event.clear()
    except Exception:
        pass


def fresh_server():
    _stop_leaked_snapshot_worker()
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if (
            key == "server"
            or key.startswith("engram_")
            or key == "health"
            or key.startswith("health.")
        ):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


# ---------------------------------------------------------------------------
# nap -> checkpoint_mode "nap"
# ---------------------------------------------------------------------------


def test_nap_result_carries_health_snapshot_with_nap_mode():
    server = fresh_server()
    r = json.loads(server._nap_impl(message="checkpoint-wiring test nap"))
    assert "error" not in r, f"_nap_impl returned error: {r}"
    assert "health_snapshot" in r, f"_nap_impl result missing health_snapshot: {r}"
    hs = r["health_snapshot"]
    assert hs.get("checkpoint_mode") == "nap", f"expected nap mode, got: {hs}"
    assert "error" not in hs, f"health_snapshot reported an error: {hs}"
    assert "composed_score" in hs
    assert "aspect_scores" in hs


def test_nap_writes_a_real_snapshot_file_to_disk():
    server = fresh_server()
    r = json.loads(server._nap_impl(message="checkpoint-wiring test nap file"))
    hs = r["health_snapshot"]
    path = hs.get("path")
    assert path, f"no snapshot path recorded: {hs}"
    assert os.path.exists(path), f"snapshot file does not exist on disk: {path}"
    with open(path, encoding="utf-8") as f:
        snapshot = json.load(f)
    assert snapshot["checkpoint_mode"] == "nap"
    assert "health-snapshots" in path


# ---------------------------------------------------------------------------
# advance_turn -> checkpoint_mode "post_dream"
# ---------------------------------------------------------------------------


def test_advance_turn_result_carries_health_snapshot_with_post_dream_mode():
    """engram_advance_turn (mode='session' internally) maps to checkpoint_mode
    'post_dream' — see engram_lifecycle._checkpoint_internal's wiring comment
    for why (engram_advance_turn is [Sleep/first-session only]; the codebase
    states outright that turn advance is reserved for post-dream checkpoints).

    NOTE: this test advances the turn on the test DB (irreversible within the
    test process), matching test_advance_turn_payload.py's own precedent —
    runs against a fresh isolated TEST_DIR, safe to call once.
    """
    server = fresh_server()
    r = json.loads(server._advance_turn_impl(message="checkpoint-wiring test advance_turn"))
    assert "error" not in r, f"_advance_turn_impl returned error: {r}"
    assert "health_snapshot" in r, f"_advance_turn_impl result missing health_snapshot: {r}"
    hs = r["health_snapshot"]
    assert hs.get("checkpoint_mode") == "post_dream", f"expected post_dream mode, got: {hs}"
    assert "error" not in hs, f"health_snapshot reported an error: {hs}"


# ---------------------------------------------------------------------------
# Best-effort posture — a health-engine failure must never break nap/advance_turn
# ---------------------------------------------------------------------------


def test_health_engine_failure_does_not_break_nap(monkeypatch):
    """If health_engine.write_snapshot raises for any reason, _nap_impl must
    still return its normal success result — mirrors core._commit_snapshot's
    own best-effort posture that this wiring is piggybacked onto."""
    server = fresh_server()
    from health import engine as health_engine

    def _boom(conn, checkpoint_mode, config=health_engine.CONFIG):
        raise RuntimeError("simulated health-engine failure")

    monkeypatch.setattr(health_engine, "write_snapshot", _boom)

    r = json.loads(server._nap_impl(message="checkpoint-wiring test failure isolation"))
    assert "error" not in r, f"_nap_impl should still succeed despite health-engine failure: {r}"
    assert r["status"] == "checkpoint_saved"
    hs = r.get("health_snapshot")
    assert hs is not None
    assert "error" in hs, f"expected the failure to be surfaced in health_snapshot: {hs}"
    assert "simulated health-engine failure" in hs["error"]


# ---------------------------------------------------------------------------
# pre_sleep is deliberately NOT wired — documented absence, not silently
# assumed. See PR2 report's checkpoint-hook investigation.
# ---------------------------------------------------------------------------


def test_checkpoint_internal_never_emits_pre_sleep_mode():
    """Neither nap nor advance_turn should ever produce checkpoint_mode
    'pre_sleep' — that checkpoint has no wired call site in this PR. This is
    a documentation-as-test guard: if a future change wires pre_sleep into
    this same call site without updating this test, that's a deliberate
    signal to review the new wiring's correctness, not silent drift."""
    server = fresh_server()
    nap_result = json.loads(server._nap_impl(message="pre_sleep absence check (nap)"))
    assert nap_result["health_snapshot"]["checkpoint_mode"] != "pre_sleep"

    server = fresh_server()
    adv_result = json.loads(server._advance_turn_impl(message="pre_sleep absence check (advance_turn)"))
    assert adv_result["health_snapshot"]["checkpoint_mode"] != "pre_sleep"
