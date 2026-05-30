"""
Tests for hot-swap focus sets — named, persistent focus-group snapshots (tabs model).

Covers the 5 new tools (engram_focus_save / load / swap / sets / delete_set) and
the integration points with existing tools (engram_focus / engram_unfocus /
engram_list_focused) per active-work/focus-sets.md.

Coverage:
  1. Name pattern validation (^[a-z0-9_-]{1,50}$)
  2. Save — create, collision protection, overwrite, empty-active rejection
  3. Load — into empty, already_active fast path, if_active='error'/'overwrite',
     implicit-overwrite reload when cascade state shifted, missing set
  4. Swap — atomic save+load, same-name no-op, rollback on missing load target,
     save-only
  5. Sets listing — ordering, is_active flag, active_set_name
  6. Delete — non-active, active (clears active_set_name, preserves pins), ghost
  7. Cascade resolution — supersede auto-follow, retract drop, missing drop
  8. active_set_name tracking — focus/unfocus diverge clear; list_focused
     includes the field
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_focus_sets_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


class _DirectClient:
    """Minimal shim — calls server functions directly and parses JSON output.

    The focus tools are not registered in engram_client.py's TOOL_SCHEMAS
    (pre-existing gap; even the legacy engram_focus isn't exposed via the
    client wrapper). Tests dispatch through server module functions directly,
    mirroring the EngramClient.call signature for readability.
    """

    def __init__(self, db_dir):
        for key in list(sys.modules):
            if key == "server" or key.startswith("engram_"):
                del sys.modules[key]
        import server
        server._configure_paths(db_dir)
        server._ensure_data_dir()
        self._server = server

    def call(self, tool_name, arguments=None):
        fn = getattr(self._server, tool_name)
        args = arguments or {}
        # Mirror EngramClient.call's legacy-form auto-wrap for single-payload
        # tools (currently engram_add_observation): if the caller passes the
        # legacy named-field dict, wrap as payload_json.
        try:
            from engram_client import TOOL_SCHEMAS
            schema = TOOL_SCHEMAS.get(tool_name)
            if (schema
                    and list(schema.get("parameters", {}).keys()) == ["payload_json"]
                    and "payload_json" not in args):
                args = {"payload_json": json.dumps(args)}
        except ImportError:
            pass
        result_str = fn(**args)
        return json.loads(result_str)


def fresh_client():
    """Return a clean client on a fresh sandbox DB."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    return _DirectClient(TEST_DIR)


def seed_observations(client, count=5):
    """Create `count` observations and return their IDs in order."""
    ids = []
    for i in range(count):
        letter = chr(ord("a") + i)
        r = client.call("engram_add_observation", {
            "url": f"https://example.com/{letter}",
            "title": letter.upper(),
            "quoted_text": f"quote {letter}",
            "interpretation": "seed",
            "claim": f"claim {letter}",
            "quote_type": "hard_data",
        })
        ids.append(r["observation_id"])
    return ids


# ── Name pattern validation ────────────────────────────────────────────────


def test_name_rejects_uppercase():
    client = fresh_client()
    seed_observations(client, 2)
    client.call("engram_focus", {"node_ids": "ob_0001,ob_0002", "reason": "t"})
    r = client.call("engram_focus_save", {"name": "BadName"})
    assert r["status"] == "error" and "Invalid name" in r["error"], r


def test_name_rejects_space():
    client = fresh_client()
    seed_observations(client, 2)
    client.call("engram_focus", {"node_ids": "ob_0001", "reason": "t"})
    r = client.call("engram_focus_save", {"name": "has space"})
    assert r["status"] == "error", r


def test_name_rejects_empty():
    client = fresh_client()
    seed_observations(client, 1)
    client.call("engram_focus", {"node_ids": "ob_0001", "reason": "t"})
    r = client.call("engram_focus_save", {"name": ""})
    assert r["status"] == "error", r


def test_name_accepts_hyphen_and_digit():
    client = fresh_client()
    seed_observations(client, 1)
    client.call("engram_focus", {"node_ids": "ob_0001", "reason": "t"})
    r = client.call("engram_focus_save", {"name": "arc-agi-l2"})
    assert r["status"] == "ok", r


# ── Save ──────────────────────────────────────────────────────────────────


def test_save_creates_and_sets_active_name():
    client = fresh_client()
    seed_observations(client, 3)
    client.call("engram_focus", {"node_ids": "ob_0001,ob_0002,ob_0003", "reason": "t"})
    r = client.call("engram_focus_save", {"name": "s1", "description": "first"})
    assert r["status"] == "ok"
    assert r["action"] == "created"
    assert r["node_count"] == 3
    assert r["active_set_name"] == "s1"


def test_save_collision_requires_overwrite():
    client = fresh_client()
    seed_observations(client, 2)
    client.call("engram_focus", {"node_ids": "ob_0001,ob_0002", "reason": "t"})
    client.call("engram_focus_save", {"name": "s1"})
    r = client.call("engram_focus_save", {"name": "s1"})
    assert r["status"] == "error" and "already exists" in r["error"]


def test_save_overwrite_succeeds():
    client = fresh_client()
    seed_observations(client, 2)
    client.call("engram_focus", {"node_ids": "ob_0001,ob_0002", "reason": "t"})
    client.call("engram_focus_save", {"name": "s1"})
    r = client.call("engram_focus_save", {"name": "s1", "overwrite": True})
    assert r["status"] == "ok" and r["action"] == "overwritten"


def test_save_empty_active_rejected():
    client = fresh_client()
    seed_observations(client, 1)
    # No focus calls — active list is empty
    r = client.call("engram_focus_save", {"name": "s1"})
    assert r["status"] == "error" and "empty" in r["error"].lower()


# ── Load ──────────────────────────────────────────────────────────────────


def test_load_into_empty_active():
    client = fresh_client()
    seed_observations(client, 3)
    client.call("engram_focus", {"node_ids": "ob_0001,ob_0002,ob_0003", "reason": "t"})
    client.call("engram_focus_save", {"name": "s1"})
    client.call("engram_unfocus", {"node_ids": "ob_0001,ob_0002,ob_0003"})
    r = client.call("engram_focus_load", {"name": "s1"})
    assert r["status"] == "ok"
    assert set(r["loaded"]) == {"ob_0001", "ob_0002", "ob_0003"}
    assert r["active_set_name"] == "s1"


def test_load_already_active_fast_path():
    client = fresh_client()
    seed_observations(client, 2)
    client.call("engram_focus", {"node_ids": "ob_0001,ob_0002", "reason": "t"})
    client.call("engram_focus_save", {"name": "s1"})
    # Active already matches s1's resolved members
    r = client.call("engram_focus_load", {"name": "s1"})
    assert r["status"] == "already_active"


def test_load_missing_set_rejected():
    client = fresh_client()
    r = client.call("engram_focus_load", {"name": "ghost"})
    assert r["status"] == "error" and "No focus set" in r["error"]


def test_load_if_active_error_blocks_diverged():
    client = fresh_client()
    seed_observations(client, 3)
    client.call("engram_focus", {"node_ids": "ob_0001,ob_0002", "reason": "t"})
    client.call("engram_focus_save", {"name": "s1"})
    # Save a second set so we have something different to load
    client.call("engram_focus", {"node_ids": "ob_0003", "reason": "t"})
    # Diverged: active now has 3 pins, active_set_name is cleared
    r = client.call("engram_focus_load", {"name": "s1"})
    assert r["status"] == "error" and "overwrite" in r["error"]


def test_load_if_active_overwrite_clears_and_loads():
    client = fresh_client()
    seed_observations(client, 3)
    client.call("engram_focus", {"node_ids": "ob_0001,ob_0002", "reason": "t"})
    client.call("engram_focus_save", {"name": "s1"})
    client.call("engram_focus", {"node_ids": "ob_0003", "reason": "diverge"})
    r = client.call("engram_focus_load", {"name": "s1", "if_active": "overwrite"})
    assert r["status"] == "ok"
    assert set(r["loaded"]) == {"ob_0001", "ob_0002"}


def test_load_implicit_overwrite_on_cascade_shift():
    """Name matches active_set_name but cascade state has shifted → reload without erroring."""
    client = fresh_client()
    ids = seed_observations(client, 3)
    client.call("engram_focus", {"node_ids": ",".join(ids), "reason": "t"})
    client.call("engram_focus_save", {"name": "s1"})
    # Supersede ob_0001 → new ID. Under the relational-only API, the caller
    # first creates the replacement node via the canonical creation tool and
    # then calls engram_supersede with both IDs.
    rep = client.call("engram_add_observation", {
        "url": "https://example.com/a-v2",
        "title": "A v2",
        "quoted_text": "quote a v2",
        "interpretation": "refined",
        "claim": "claim a v2",
        "quote_type": "hard_data",
    })
    new_id = rep["observation_id"]
    sr = client.call("engram_supersede", {
        "old_node_id": "ob_0001",
        "new_node_id": new_id,
        "supersede_reason": "refinement",
    })
    assert sr["status"] == "superseded", sr
    # Load s1 again — active still has old IDs, resolved has new. Implicit overwrite.
    r = client.call("engram_focus_load", {"name": "s1"})
    assert r["status"] == "ok"
    assert r["auto_followed_supersede"] == [{"original": "ob_0001", "loaded": new_id}]
    assert new_id in r["loaded"]


# ── Swap ──────────────────────────────────────────────────────────────────


def test_swap_atomic_save_and_load():
    client = fresh_client()
    seed_observations(client, 3)
    client.call("engram_focus", {"node_ids": "ob_0001,ob_0002", "reason": "t"})
    client.call("engram_focus_save", {"name": "s1"})
    client.call("engram_focus", {"node_ids": "ob_0003", "reason": "t"})
    # Active has 3 now. Swap: save as s2, load s1.
    r = client.call("engram_focus_swap", {"save_as": "s2", "load": "s1"})
    assert r["status"] == "ok"
    assert r["saved"]["node_count"] == 3
    assert r["loaded"]["node_count"] == 2
    assert r["active_set_name"] == "s1"


def test_swap_same_name_already_active():
    client = fresh_client()
    seed_observations(client, 2)
    client.call("engram_focus", {"node_ids": "ob_0001,ob_0002", "reason": "t"})
    client.call("engram_focus_save", {"name": "s1"})
    r = client.call("engram_focus_swap", {"save_as": "s1", "load": "s1"})
    assert r["status"] == "already_active"


def test_swap_rolls_back_on_missing_load_target():
    client = fresh_client()
    seed_observations(client, 2)
    client.call("engram_focus", {"node_ids": "ob_0001,ob_0002", "reason": "t"})
    before = client.call("engram_focus_sets")
    names_before = {s["name"] for s in before["sets"]}
    r = client.call("engram_focus_swap", {"save_as": "s1", "load": "ghost"})
    assert r["status"] == "error" and "rolled back" in r["error"]
    after = client.call("engram_focus_sets")
    names_after = {s["name"] for s in after["sets"]}
    assert names_before == names_after, "s1 should NOT have been saved"


def test_swap_save_only_no_load():
    client = fresh_client()
    seed_observations(client, 2)
    client.call("engram_focus", {"node_ids": "ob_0001,ob_0002", "reason": "t"})
    r = client.call("engram_focus_swap", {"save_as": "s1"})
    assert r["status"] == "ok"
    assert r["loaded"] is None
    assert r["active_set_name"] == "s1"


# ── Sets listing ──────────────────────────────────────────────────────────


def test_sets_lists_with_is_active_flag():
    client = fresh_client()
    seed_observations(client, 3)
    client.call("engram_focus", {"node_ids": "ob_0001", "reason": "t"})
    client.call("engram_focus_save", {"name": "s1"})
    client.call("engram_focus", {"node_ids": "ob_0002", "reason": "t"})
    client.call("engram_focus_save", {"name": "s2"})
    r = client.call("engram_focus_sets")
    assert r["status"] == "ok"
    assert r["active_set_name"] == "s2"
    active = [s for s in r["sets"] if s["is_active"]]
    assert len(active) == 1 and active[0]["name"] == "s2"


# ── Delete ────────────────────────────────────────────────────────────────


def test_delete_non_active_set():
    client = fresh_client()
    seed_observations(client, 2)
    client.call("engram_focus", {"node_ids": "ob_0001", "reason": "t"})
    client.call("engram_focus_save", {"name": "s1"})
    client.call("engram_focus", {"node_ids": "ob_0002", "reason": "t"})
    client.call("engram_focus_save", {"name": "s2"})
    # s2 is active; delete s1
    r = client.call("engram_focus_delete_set", {"name": "s1"})
    assert r["status"] == "ok" and r["was_active"] is False


def test_delete_active_set_clears_name_preserves_pins():
    client = fresh_client()
    seed_observations(client, 2)
    client.call("engram_focus", {"node_ids": "ob_0001,ob_0002", "reason": "t"})
    client.call("engram_focus_save", {"name": "s1"})
    r = client.call("engram_focus_delete_set", {"name": "s1"})
    assert r["was_active"] is True
    assert r["active_set_name"] is None
    lf = client.call("engram_list_focused")
    assert lf["count"] == 2, "Pins should remain after deleting the set"
    assert lf["active_set_name"] is None


def test_delete_ghost_rejected():
    client = fresh_client()
    r = client.call("engram_focus_delete_set", {"name": "ghost"})
    assert r["status"] == "error" and "No focus set" in r["error"]


# ── Cascade resolution on load ─────────────────────────────────────────────


def test_load_cascade_supersede_and_retract():
    client = fresh_client()
    seed_observations(client, 3)
    client.call("engram_focus", {"node_ids": "ob_0001,ob_0002,ob_0003", "reason": "t"})
    client.call("engram_focus_save", {"name": "s1"})
    # Two-step supersede: create the replacement via the canonical creation
    # tool, then wire the relational supersede.
    rep = client.call("engram_add_observation", {
        "url": "https://example.com/a-v2",
        "title": "A v2",
        "quoted_text": "quote a v2",
        "interpretation": "refined",
        "claim": "claim a v2",
        "quote_type": "hard_data",
    })
    new_id = rep["observation_id"]
    sr = client.call("engram_supersede", {
        "old_node_id": "ob_0001",
        "new_node_id": new_id,
        "supersede_reason": "refinement",
    })
    assert sr["status"] == "superseded", sr
    client.call("engram_retract", {
        "node_id": "ob_0002",
        "error_type": "hallucinated_claim",
        "reason": "test",
    })
    # Clear active explicitly, then load
    client.call("engram_unfocus", {"node_ids": "ob_0003"})
    # ob_0001 already unfocused by supersede flow; ob_0002 still focused (retract doesn't unfocus)
    # but focused_at is irrelevant because is_current=0 excludes it
    r = client.call("engram_focus_load", {"name": "s1", "if_active": "overwrite"})
    assert r["status"] == "ok"
    assert r["auto_followed_supersede"] == [{"original": "ob_0001", "loaded": new_id}]
    assert r["dropped_retracted"] == ["ob_0002"]
    assert r["dropped_missing"] == []
    assert set(r["loaded"]) == {new_id, "ob_0003"}


def test_load_cascade_missing_id_dropped():
    client = fresh_client()
    seed_observations(client, 1)
    client.call("engram_focus", {"node_ids": "ob_0001", "reason": "t"})
    r = client.call("engram_focus_save", {"name": "s1"})
    # Inject a fake raw_id into the saved set via direct SQL to simulate corruption
    import sqlite3
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE focus_sets SET node_ids = ? WHERE name = ?",
        (json.dumps(["ob_0001", "ob_9999"]), "s1"),
    )
    conn.commit()
    conn.close()
    client.call("engram_unfocus", {"node_ids": "ob_0001"})
    r = client.call("engram_focus_load", {"name": "s1"})
    assert r["status"] == "ok"
    assert r["dropped_missing"] == ["ob_9999"]
    assert r["loaded"] == ["ob_0001"]


# ── active_set_name tracking ───────────────────────────────────────────────


def test_list_focused_returns_active_set_name_null_initially():
    client = fresh_client()
    r = client.call("engram_list_focused")
    assert "active_set_name" in r and r["active_set_name"] is None


def test_focus_diverges_clears_active_set_name():
    client = fresh_client()
    seed_observations(client, 3)
    client.call("engram_focus", {"node_ids": "ob_0001,ob_0002", "reason": "t"})
    client.call("engram_focus_save", {"name": "s1"})
    # Adding ob_0003 diverges from s1
    r = client.call("engram_focus", {"node_ids": "ob_0003", "reason": "diverge"})
    assert r["active_set_name"] is None
    lf = client.call("engram_list_focused")
    assert lf["active_set_name"] is None


def test_unfocus_member_clears_active_set_name():
    client = fresh_client()
    seed_observations(client, 2)
    client.call("engram_focus", {"node_ids": "ob_0001,ob_0002", "reason": "t"})
    client.call("engram_focus_save", {"name": "s1"})
    r = client.call("engram_unfocus", {"node_ids": "ob_0001"})
    assert r["active_set_name"] is None


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    failed = []
    for t in tests:
        try:
            t()
            passed += 1
            print(f"PASS  {t.__name__}")
        except Exception:
            failed.append(t.__name__)
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} passed")
    if failed:
        sys.exit(1)
