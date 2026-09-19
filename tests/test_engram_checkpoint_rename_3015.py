"""Tests for the engram_nap -> engram_checkpoint rename (issue #3015).

The MCP tool named `engram_nap` collided with the `engram-nap` SKILL name
(the eight-step nap routine, of which calling the tool is only one step —
Step 3). A nudge that literally said "Run engram_nap" read ambiguously as
"call the tool" (which is all it mechanically does) vs. "run the skill"
(the full routine, including the load-bearing Step 5b warm-briefing
rotation) -- an agent that took the nudge literally skipped the skill.

Fix (two parts, both covered here):
  1. The MCP tool is renamed to `engram_checkpoint`; `engram_nap` remains a
     thin, behavior-identical deprecated alias for one release.
  2. The two hook nudge sites (context_tracker.py, engram-surface-hook.py)
     no longer say "Run engram_nap" -- they say to load and run the
     engram-nap skill instead.

Coverage:
  1. engram_checkpoint exists and works (a real payload_json call succeeds
     and returns the expected checkpoint-result shape).
  2. engram_nap still exists and works identically -- same output shape,
     same behavior -- confirming it is a genuine deprecated alias, not a
     dead/broken name.
  3. Grep-based regression guard: neither nudge site's source contains the
     literal string "Run engram_nap" anymore.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_checkpoint_rename_3015_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTEXT_TRACKER_PATH = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "context_tracker.py"
SURFACE_HOOK_PATH = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"


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


# ---------------------------------------------------------------------------
# 1. engram_checkpoint exists and works
# ---------------------------------------------------------------------------

def test_engram_checkpoint_exists_and_works():
    """The renamed primary tool is callable and returns the expected shape."""
    server = fresh_server()
    assert hasattr(server, "engram_checkpoint"), (
        "server module is missing engram_checkpoint -- the #3015 rename did "
        "not land the primary tool."
    )
    r = json.loads(server.engram_checkpoint(payload_json=json.dumps({
        "message": "test_engram_checkpoint_exists_and_works checkpoint",
    })))
    assert "error" not in r, f"engram_checkpoint returned error: {r}"
    assert "status" in r, f"engram_checkpoint missing 'status' key: {r}"
    assert "mode" in r, f"engram_checkpoint missing 'mode' key: {r}"
    assert "previous_turn" in r, f"engram_checkpoint missing 'previous_turn' key: {r}"
    assert "current_turn" in r, f"engram_checkpoint missing 'current_turn' key: {r}"
    assert "turn_advanced" in r, f"engram_checkpoint missing 'turn_advanced' key: {r}"
    assert r["turn_advanced"] is False, (
        f"engram_checkpoint unexpectedly advanced the turn: {r}"
    )


# ---------------------------------------------------------------------------
# 2. engram_nap deprecated alias works identically
# ---------------------------------------------------------------------------

def test_engram_nap_deprecated_alias_works_identically():
    """engram_nap still exists, still works, and produces the same output
    shape as engram_checkpoint -- confirming it is a genuine delegating
    alias, not a stale/dead name left behind by the rename.
    """
    server = fresh_server()
    assert hasattr(server, "engram_nap"), (
        "server module is missing engram_nap -- the deprecated alias was "
        "not kept (issue #3015 requires it survive for one release)."
    )

    checkpoint_result = json.loads(server.engram_checkpoint(payload_json=json.dumps({
        "message": "shape-comparison checkpoint call",
    })))
    nap_result = json.loads(server.engram_nap(payload_json=json.dumps({
        "message": "shape-comparison nap call",
    })))

    assert "error" not in nap_result, f"engram_nap returned error: {nap_result}"
    # Same key shape (both calls independent checkpoints on the same fresh
    # graph, so exact values like turn numbers may legitimately differ run
    # to run -- what must match is the RETURN CONTRACT, key-for-key).
    assert set(nap_result.keys()) == set(checkpoint_result.keys()), (
        "engram_nap's return shape has drifted from engram_checkpoint's -- "
        "the alias must delegate to the exact same implementation.\n"
        f"engram_checkpoint keys: {sorted(checkpoint_result.keys())}\n"
        f"engram_nap keys:        {sorted(nap_result.keys())}"
    )
    assert nap_result["mode"] == checkpoint_result["mode"], (
        f"mode mismatch: nap={nap_result['mode']!r} "
        f"checkpoint={checkpoint_result['mode']!r}"
    )
    assert nap_result["turn_advanced"] is False, (
        f"engram_nap alias unexpectedly advanced the turn: {nap_result}"
    )


def test_engram_nap_alias_rejects_payload_same_as_checkpoint():
    """Payload validation behavior (unknown field, missing message) must be
    identical between the alias and the primary tool -- same _CHECKPOINT_FIELDS
    gate, same error shape.
    """
    server = fresh_server()

    checkpoint_err = json.loads(server.engram_checkpoint(payload_json=json.dumps({})))
    nap_err = json.loads(server.engram_nap(payload_json=json.dumps({})))
    assert "error" in checkpoint_err and "error" in nap_err, (
        f"Expected both to reject an empty payload: "
        f"checkpoint={checkpoint_err}, nap={nap_err}"
    )
    assert "message" in checkpoint_err["error"] and "message" in nap_err["error"]

    checkpoint_unknown = json.loads(server.engram_checkpoint(payload_json=json.dumps({
        "message": "x", "bogus_field": "y",
    })))
    nap_unknown = json.loads(server.engram_nap(payload_json=json.dumps({
        "message": "x", "bogus_field": "y",
    })))
    assert "Unknown fields" in checkpoint_unknown["error"]
    assert "Unknown fields" in nap_unknown["error"]


# ---------------------------------------------------------------------------
# 3. Grep-based regression guard: the two nudge sites no longer say
#    "Run engram_nap"
# ---------------------------------------------------------------------------

def test_context_tracker_nudge_does_not_say_run_engram_nap():
    """context_tracker.py's urgent-drowsiness nudge must not tell the agent
    to literally 'Run engram_nap' -- it must point at the engram-nap skill.
    """
    assert CONTEXT_TRACKER_PATH.exists(), f"missing file: {CONTEXT_TRACKER_PATH}"
    src = CONTEXT_TRACKER_PATH.read_text()
    assert "Run engram_nap" not in src, (
        "context_tracker.py still contains the literal nudge text "
        "'Run engram_nap' -- issue #3015 requires this to point at the "
        "engram-nap skill instead of the bare tool name."
    )
    assert "engram-nap skill" in src, (
        "context_tracker.py's nudge no longer mentions the engram-nap "
        "skill by name -- expected the urgent-drowsiness nudge to say "
        "something like 'Load and run the engram-nap skill'."
    )


def test_surface_hook_nudge_does_not_say_run_engram_nap():
    """engram-surface-hook.py's prompt-count nap warning must not tell the
    agent to literally 'Run engram_nap' -- it must point at the engram-nap
    skill.
    """
    assert SURFACE_HOOK_PATH.exists(), f"missing file: {SURFACE_HOOK_PATH}"
    src = SURFACE_HOOK_PATH.read_text()
    assert "Run engram_nap" not in src, (
        "engram-surface-hook.py still contains the literal nudge text "
        "'Run engram_nap' -- issue #3015 requires this to point at the "
        "engram-nap skill instead of the bare tool name."
    )
    assert "engram-nap skill" in src, (
        "engram-surface-hook.py's format_nap_warning() no longer mentions "
        "the engram-nap skill by name -- expected the urgent-band warning "
        "to say something like 'Load and run the engram-nap skill'."
    )


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"PASS: {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {t.__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
