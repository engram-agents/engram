"""Tests for the health-series lifecycle hook wiring into
engram_lifecycle._checkpoint_internal (engram-alpha#2575 PR C1).

Style follows tests/test_health_engine_checkpoint_wiring.py's fresh_server()
pattern exactly (this file reuses the SAME module-clearing predicate --
`health` + every `health.*` submodule, plus `server`/`engram_*`), but is a
SEPARATE file/TEST_DIR so this PR's own dispatch-scoped tests never share
process state with that file's.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_health_series_hook_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _stop_leaked_snapshot_worker():
    """Mirrors tests/test_health_engine_checkpoint_wiring.py's own helper of
    the same name exactly -- see that file's docstring for the full TOCTOU
    race this guards against."""
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
# post_dream (advance_turn) carries health_series with ingest/backfill/derive
# ---------------------------------------------------------------------------


def test_advance_turn_result_carries_health_series_with_all_three_keys():
    server = fresh_server()
    r = json.loads(server._advance_turn_impl(message="health-series hook test advance_turn"))
    assert "error" not in r, f"_advance_turn_impl returned error: {r}"
    assert "health_series" in r, f"result missing health_series: {r}"
    hs = r["health_series"]
    assert set(hs.keys()) >= {"ingest", "backfill", "derive", "elapsed_seconds"}
    # TEST_DIR is NOT a git repo -- ingest/backfill/derive must degrade to
    # error/empty dicts, never raise out of the checkpoint.
    assert r["status"] == "checkpoint_saved"


# ---------------------------------------------------------------------------
# nap does NOT run the series store
# ---------------------------------------------------------------------------


def test_nap_result_carries_health_series_skipped():
    server = fresh_server()
    r = json.loads(server._nap_impl(message="health-series hook test nap"))
    assert "error" not in r, f"_nap_impl returned error: {r}"
    assert "health_series" in r
    assert "skipped" in r["health_series"]
    assert r["status"] == "checkpoint_saved"


# ---------------------------------------------------------------------------
# best-effort posture -- a health_series failure must never break the
# checkpoint
# ---------------------------------------------------------------------------


def test_health_series_checkpoint_hook_failure_does_not_break_advance_turn(monkeypatch):
    server = fresh_server()
    from health import series as health_series

    def _boom(*_a, **_kw):
        raise RuntimeError("simulated health-series failure")

    monkeypatch.setattr(health_series, "checkpoint_hook", _boom)

    r = json.loads(server._advance_turn_impl(message="health-series hook failure isolation"))
    assert "error" not in r, f"_advance_turn_impl should still succeed: {r}"
    assert r["status"] == "checkpoint_saved"
    hs = r.get("health_series")
    assert hs is not None
    assert "error" in hs
    assert "simulated health-series failure" in hs["error"]
