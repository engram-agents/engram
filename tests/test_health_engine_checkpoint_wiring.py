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
clearing health_engine / health_engine_metrics / health_engine_config from
sys.modules. Those three modules are the first `health_engine*` family
members to import engram_core, and the existing fresh_server() convention's
module-clearing predicate (`key == "server" or key.startswith("engram_")`)
does not sweep them — see health_engine_metrics.py's module docstring for
why a stale cross-test engram_core binding would otherwise be a real (not
hypothetical) contamination risk once this file exists in the same pytest
process as the pre-existing nap/advance_turn test files.
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


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if (
            key == "server"
            or key.startswith("engram_")
            or key.startswith("health_engine")
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
    import health_engine

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
