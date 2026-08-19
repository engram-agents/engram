"""Tests for #2000 — engram_update_lesson refreshing error_incidents.json.

The gap: engram_update_lesson writes a corrected scaffolding_nudge (or
situation_pattern) to the lesson node's metadata, but never refreshed
~/.engram/error_incidents.json (the per-incident tripwire cache the
surface hook scans every prompt). A corrected lesson kept serving its OLD
nudge through the file-backed surface indefinitely.

Fix: _update_lesson_impl now calls core._rebuild_incidents_cache() when the
update touches scaffolding_nudge or situation_pattern (the two fields the
cache carries), and surfaces the result as `cache_rebuild` in the response —
same shape _register_exemplar_impl already returns for lesson targets.

Coverage:
  1. scaffolding_nudge update -> cache file on disk carries the NEW text,
     response includes cache_rebuild.
  2. situation_pattern update -> cache_rebuild fires (situation_pattern
     itself isn't stored in the file, but the rebuild call still happens
     and is surfaced).
  3. An update that touches neither cache field -> no rebuild, response
     omits cache_rebuild, cache file untouched.
     Note: as of #1826, _LESSON_MUTABLE_FIELDS == {situation_pattern,
     scaffolding_nudge} — i.e. every currently-mutable field IS a cache
     field, so there is no real-world "update an unrelated mutable field"
     case to exercise through the public API today. Two proxies are used
     instead: (3a) a validation-rejected update (unknown field) via the
     public API — touches nothing, so no rebuild should fire; (3b) a
     direct unit test on the field-intersection guard itself, via a
     monkeypatched mutable-field set carrying a synthetic non-cache field,
     to prove the guard's *logic* (not just today's field-set coincidence)
     skips the rebuild when a touched field isn't cache-relevant.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_update_lesson_cache_refresh_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _seed_incident(server, claim="I shipped without reading the error.") -> str:
    """Create a claim-bearing observation node usable as an incident."""
    payload = {
        "quoted_text": claim,
        "interpretation": "Missed contextual cue from stderr.",
        "claim": claim,
        "quote_type": "personal_communication",
        "url": "https://example.com/incident",
        "title": "Test incident",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["observation_id"]


def _make_lesson(server, inc_id, **overrides) -> str:
    payload = {
        "claim": "When diagnosing errors, read the output before hypothesizing.",
        "incident_ids": inc_id,
        "scaffolding_nudge": "Stop. Read the error output verbatim before forming a hypothesis.",
        "logical_chain": "Single incident -> action-focused tripwire pattern.",
    }
    payload.update(overrides)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["lesson_id"]


def _read_incidents_cache(server) -> dict:
    with open(server.core.ERROR_INCIDENTS_PATH, "r") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 1. scaffolding_nudge update rebuilds the cache with the NEW text
# ---------------------------------------------------------------------------

def test_scaffolding_nudge_update_refreshes_incidents_cache():
    server = fresh_server()
    inc_id = _seed_incident(server)
    lesson_id = _make_lesson(server, inc_id)

    # Creation-time write is already in the cache with the OLD nudge.
    before = _read_incidents_cache(server)
    assert before[inc_id]["scaffolding_nudge"] == (
        "Stop. Read the error output verbatim before forming a hypothesis."
    )

    new_nudge = "Stop. Read stderr in full, verbatim, before naming a root cause."
    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"scaffolding_nudge": new_nudge},
        "reason": "correcting stale nudge per #2000",
    })))
    assert "error" not in upd, upd
    assert upd["status"] == "updated"
    assert "cache_rebuild" in upd, upd
    assert upd["cache_rebuild"].get("status") == "rebuilt", upd["cache_rebuild"]

    after = _read_incidents_cache(server)
    assert after[inc_id]["scaffolding_nudge"] == new_nudge, after


# ---------------------------------------------------------------------------
# 2. situation_pattern update also fires the rebuild
# ---------------------------------------------------------------------------

def test_situation_pattern_update_fires_rebuild():
    server = fresh_server()
    inc_id = _seed_incident(server)
    lesson_id = _make_lesson(server, inc_id)

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"situation_pattern": r"gh\s+pr\s+create"},
    })))
    assert "error" not in upd, upd
    assert "cache_rebuild" in upd, upd
    assert upd["cache_rebuild"].get("status") == "rebuilt", upd["cache_rebuild"]


# ---------------------------------------------------------------------------
# 3a. Validation-rejected update (touches nothing) -> no rebuild
# ---------------------------------------------------------------------------

def test_rejected_update_does_not_rebuild_cache():
    server = fresh_server()
    inc_id = _seed_incident(server)
    lesson_id = _make_lesson(server, inc_id)

    before = _read_incidents_cache(server)
    before_mtime = os.path.getmtime(server.core.ERROR_INCIDENTS_PATH)

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"bogus_unknown_field": "nope"},
    })))
    assert "error" in upd, upd
    assert "cache_rebuild" not in upd, upd

    after = _read_incidents_cache(server)
    after_mtime = os.path.getmtime(server.core.ERROR_INCIDENTS_PATH)
    assert after == before
    assert after_mtime == before_mtime


# ---------------------------------------------------------------------------
# 3b. Direct guard-logic test: a mutable field that ISN'T cache-relevant
#     must not trigger a rebuild, even if it were ever added to the mutable
#     set. Proves the intersection check, not just today's field coincidence.
# ---------------------------------------------------------------------------

def test_non_cache_mutable_field_does_not_rebuild_cache(monkeypatch):
    server = fresh_server()
    inc_id = _seed_incident(server)
    lesson_id = _make_lesson(server, inc_id)

    epistemic = server._epistemic_mod
    monkeypatch.setattr(
        epistemic, "_LESSON_MUTABLE_FIELDS",
        frozenset(epistemic._LESSON_MUTABLE_FIELDS | {"_test_only_field"}),
    )

    before = _read_incidents_cache(server)
    before_mtime = os.path.getmtime(server.core.ERROR_INCIDENTS_PATH)

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"_test_only_field": "irrelevant to the cache"},
    })))
    assert "error" not in upd, upd
    assert upd["status"] == "updated"
    assert "cache_rebuild" not in upd, upd

    after = _read_incidents_cache(server)
    after_mtime = os.path.getmtime(server.core.ERROR_INCIDENTS_PATH)
    assert after == before
    assert after_mtime == before_mtime
