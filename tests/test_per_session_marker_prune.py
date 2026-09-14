"""Tests for the 7-day session marker pruning added at SessionStart.

The prune_stale_session_markers() function lives in
hooks/claude/engram-session-start-hook.py. These tests exercise the Claude
implementation.

Coverage:
  (a) 8-day-old marker is deleted; 3-day and current-time markers are kept.
  (b) Non-.json file with old mtime is not deleted.
  (c) Current session's own marker is not deleted even if it appears old.
"""

import importlib.util
import os
import sys
import time
from pathlib import Path


def _load_hook(engram_home: Path):
    """Load hooks/claude/engram-session-start-hook.py with ENGRAM_HOME overridden.

    Returns the module. The caller is responsible for setting up SESSIONS_DIR
    so that calls to prune_stale_session_markers() act on a temp directory.
    """
    os.environ["ENGRAM_HOME"] = str(engram_home)
    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"
    module_name = "engram_session_start_hook_prune_test"
    # Force a fresh import so module-level ENGRAM_HOME and SESSIONS_DIR pick
    # up the env var pointing at our temp directory.
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(repo_root / "src" / "engram" / "hooks" / "claude"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def _set_mtime(path: Path, age_seconds: float) -> None:
    """Set a file's mtime to (now - age_seconds)."""
    target = time.time() - age_seconds
    os.utime(str(path), (target, target))


# ---------------------------------------------------------------------------
# (a) Age-based deletion: 8-day deleted, 3-day kept, now kept.
# ---------------------------------------------------------------------------

def test_prune_deletes_8day_keeps_3day_and_now(tmp_path):
    sessions_dir = tmp_path / "engram" / "sessions"
    sessions_dir.mkdir(parents=True)

    old_marker = sessions_dir / "aaaa1111-bbbb-cccc-dddd-eeeeffffaaaa.json"
    mid_marker = sessions_dir / "bbbb2222-cccc-dddd-eeee-ffffaaaabbbb.json"
    new_marker = sessions_dir / "cccc3333-dddd-eeee-ffff-aaaabbbbcccc.json"

    for f in (old_marker, mid_marker, new_marker):
        f.write_text("{}")

    _set_mtime(old_marker, 8 * 86400)   # 8 days old  — should be pruned
    _set_mtime(mid_marker, 3 * 86400)   # 3 days old  — should survive
    _set_mtime(new_marker, 10)          # 10 seconds  — should survive

    module = _load_hook(tmp_path / "engram")
    # Use a different session_id so none of the above are "current"
    module.prune_stale_session_markers("dddd4444-eeee-ffff-aaaa-bbbbccccdddd")

    assert not old_marker.exists(), "8-day marker should have been deleted"
    assert mid_marker.exists(),     "3-day marker should be kept"
    assert new_marker.exists(),     "current-time marker should be kept"


# ---------------------------------------------------------------------------
# (b) Non-.json files are never deleted regardless of age.
# ---------------------------------------------------------------------------

def test_prune_does_not_delete_non_json_file(tmp_path):
    sessions_dir = tmp_path / "engram" / "sessions"
    sessions_dir.mkdir(parents=True)

    txt_file = sessions_dir / "random.txt"
    txt_file.write_text("should not be touched")
    _set_mtime(txt_file, 30 * 86400)  # 30 days old

    module = _load_hook(tmp_path / "engram")
    module.prune_stale_session_markers("eeee5555-ffff-aaaa-bbbb-ccccddddeeee")

    assert txt_file.exists(), "non-.json file must not be deleted by prune"


# ---------------------------------------------------------------------------
# (c) Current session's own marker is never deleted, even if age > 7 days.
# ---------------------------------------------------------------------------

def test_prune_skips_current_session_marker(tmp_path):
    sessions_dir = tmp_path / "engram" / "sessions"
    sessions_dir.mkdir(parents=True)

    current_session_id = "ffff6666-aaaa-bbbb-cccc-ddddeeeeffff"
    current_marker = sessions_dir / f"{current_session_id}.json"
    current_marker.write_text("{}")
    _set_mtime(current_marker, 10 * 86400)  # 10 days old — would normally be pruned

    module = _load_hook(tmp_path / "engram")
    module.prune_stale_session_markers(current_session_id)

    assert current_marker.exists(), (
        "current session's own marker must not be deleted even if older than 7 days"
    )


# ---------------------------------------------------------------------------
# Extra: non-UUID-shaped .json file is not deleted (pattern guard).
# ---------------------------------------------------------------------------

def test_prune_does_not_delete_non_uuid_json(tmp_path):
    sessions_dir = tmp_path / "engram" / "sessions"
    sessions_dir.mkdir(parents=True)

    # A file that ends in .json but doesn't match any UUID or hex-id pattern.
    config_file = sessions_dir / "last-sleep-success.json"
    config_file.write_text("{}")
    _set_mtime(config_file, 30 * 86400)  # 30 days old

    module = _load_hook(tmp_path / "engram")
    module.prune_stale_session_markers("aaaa7777-bbbb-cccc-dddd-eeeeffff0000")

    assert config_file.exists(), (
        "non-UUID-shaped .json file (e.g. last-sleep-success.json) must not be deleted"
    )


# ---------------------------------------------------------------------------
# Round-2: prune with None session_id works (no current-session safeguard).
# ---------------------------------------------------------------------------

def test_prune_with_none_session_id(tmp_path):
    """prune_stale_session_markers(None) must not crash and must still delete
    old files. The None branch is reachable if the caller can't determine the
    current session — the prune should still operate, just without skipping
    a specific 'current' marker.
    """
    sessions_dir = tmp_path / "engram" / "sessions"
    sessions_dir.mkdir(parents=True)

    old_marker = sessions_dir / "1234abcd-5678-9012-3456-abcdef012345.json"
    new_marker = sessions_dir / "fedcba98-7654-3210-fedc-ba9876543210.json"
    old_marker.write_text("{}")
    new_marker.write_text("{}")
    _set_mtime(old_marker, 8 * 86400)
    _set_mtime(new_marker, 10)

    module = _load_hook(tmp_path / "engram")
    module.prune_stale_session_markers(None)  # must not crash

    assert not old_marker.exists(), "8-day marker should be pruned even with None session_id"
    assert new_marker.exists(),      "recent marker should survive"


# ---------------------------------------------------------------------------
# Round-2: old-style .meta.json files (pre-PR-160 era) are pruned by design.
# ---------------------------------------------------------------------------

def test_prune_deletes_old_meta_json_files(tmp_path):
    """The pre-PR-160 era left `*.meta.json` sidecar files in sessions/
    (visible today as e.g. 0759feb9.meta.json from April 15). These are
    stale and should be pruned alongside their .jsonl siblings. The
    _MARKER_RE second branch covers them intentionally — making the
    behavior explicit here so it can't silently drift.
    """
    sessions_dir = tmp_path / "engram" / "sessions"
    sessions_dir.mkdir(parents=True)

    old_meta = sessions_dir / "0759feb9.meta.json"
    old_hex_jsonl = sessions_dir / "0759feb9.json"  # plain hex-id form too
    old_meta.write_text("{}")
    old_hex_jsonl.write_text("{}")
    _set_mtime(old_meta, 31 * 86400)        # 31 days old
    _set_mtime(old_hex_jsonl, 31 * 86400)

    module = _load_hook(tmp_path / "engram")
    module.prune_stale_session_markers("aaaa9999-bbbb-cccc-dddd-eeeeffff1111")

    assert not old_meta.exists(),       ".meta.json sidecars older than 7 days should be pruned"
    assert not old_hex_jsonl.exists(),  "hex-id .json older than 7 days should be pruned"


# ---------------------------------------------------------------------------
# Round-3: fairy transcript deletion.
# ---------------------------------------------------------------------------

def test_prune_deletes_fairy_transcript_with_marker(tmp_path):
    sessions_dir = tmp_path / "engram" / "sessions"
    sessions_dir.mkdir(parents=True)
    transcript_file = tmp_path / "fairy-transcript.jsonl"
    transcript_file.write_text("[]")

    marker_path = sessions_dir / "aaaa1111-bbbb-cccc-dddd-eeeeffffbbbb.json"
    import json
    marker_path.write_text(json.dumps({
        "session_id": "aaaa1111-bbbb-cccc-dddd-eeeeffffbbbb",
        "transcript_path": str(transcript_file),
        "role": "fairy",
        "cwd": "/home/user/.claude/worktrees/agent-xyz/repo",
    }))
    _set_mtime(marker_path, 8 * 86400)
    _set_mtime(transcript_file, 8 * 86400)

    mod = _load_hook(tmp_path / "engram")
    mod.prune_stale_session_markers(None)

    assert not marker_path.exists(), "marker should be deleted"
    assert not transcript_file.exists(), "fairy transcript should be deleted"


def test_prune_keeps_interactive_transcript(tmp_path):
    sessions_dir = tmp_path / "engram" / "sessions"
    sessions_dir.mkdir(parents=True)
    transcript_file = tmp_path / "interactive-transcript.jsonl"
    transcript_file.write_text("[]")

    marker_path = sessions_dir / "bbbb2222-cccc-dddd-eeee-ffffffff1234.json"
    import json
    marker_path.write_text(json.dumps({
        "session_id": "bbbb2222-cccc-dddd-eeee-ffffffff1234",
        "transcript_path": str(transcript_file),
        "role": "interactive",
        "cwd": "/home/user/project",
    }))
    _set_mtime(marker_path, 8 * 86400)
    _set_mtime(transcript_file, 8 * 86400)

    mod = _load_hook(tmp_path / "engram")
    mod.prune_stale_session_markers(None)

    assert not marker_path.exists(), "marker should be deleted"
    assert transcript_file.exists(), "interactive transcript must NOT be deleted"


def test_prune_fairy_missing_transcript_no_error(tmp_path):
    sessions_dir = tmp_path / "engram" / "sessions"
    sessions_dir.mkdir(parents=True)

    marker_path = sessions_dir / "cccc3333-dddd-eeee-ffff-aaaa11112222.json"
    import json
    marker_path.write_text(json.dumps({
        "session_id": "cccc3333-dddd-eeee-ffff-aaaa11112222",
        "transcript_path": str(tmp_path / "nonexistent.jsonl"),
        "role": "fairy",
    }))
    _set_mtime(marker_path, 8 * 86400)

    mod = _load_hook(tmp_path / "engram")
    mod.prune_stale_session_markers(None)  # must not raise

    assert not marker_path.exists(), "marker should be deleted"


def test_write_marker_fairy_cwd_detection(tmp_path):
    import json, os
    sessions_dir = tmp_path / "engram" / "sessions"
    sessions_dir.mkdir(parents=True)
    # Ensure env var does not override
    os.environ.pop("ENGRAM_SESSION_ROLE", None)
    mod = _load_hook(tmp_path / "engram")

    hook_input = {
        "session_id": "dddd4444-eeee-ffff-aaaa-bbbbcccc5678",
        "transcript_path": "/tmp/fairy-session.jsonl",
        "cwd": "/home/user/.claude/worktrees/agent-abc123/engram-alpha",
        "source": "startup",
    }
    marker = mod.write_marker(hook_input)
    assert marker is not None
    assert marker["role"] == "fairy", f"expected fairy, got {marker['role']!r}"

    # Also verify the written file
    written = sessions_dir / "dddd4444-eeee-ffff-aaaa-bbbbcccc5678.json"
    with open(written) as f:
        data = json.load(f)
    assert data["role"] == "fairy"


# ---------------------------------------------------------------------------
# (d) #2859 follow-up (reviewer-fairy catch on PR-2925): piece_c_git_log()'s
# sidecar file is pruned alongside its marker, not left to accumulate.
# ---------------------------------------------------------------------------

def test_prune_deletes_piece_c_sidecar_alongside_stale_marker(tmp_path):
    sessions_dir = tmp_path / "engram" / "sessions"
    sessions_dir.mkdir(parents=True)

    session_id = "ffff6666-aaaa-bbbb-cccc-ddddeeeeffff"
    marker = sessions_dir / f"{session_id}.json"
    sidecar = sessions_dir / f"{session_id}-piece-c.txt"
    marker.write_text("{}")
    sidecar.write_text("Piece C — recent commits (2 days ago), 3 commit(s) across 1 repo(s):\n")

    _set_mtime(marker, 8 * 86400)  # 8 days old — should be pruned
    _set_mtime(sidecar, 8 * 86400)

    module = _load_hook(tmp_path / "engram")
    module.prune_stale_session_markers("aaaa0000-bbbb-cccc-dddd-eeeeffff2222")

    assert not marker.exists(), "8-day marker should have been deleted"
    assert not sidecar.exists(), "its piece-c sidecar should be deleted alongside it"


def test_prune_keeps_piece_c_sidecar_for_fresh_marker(tmp_path):
    sessions_dir = tmp_path / "engram" / "sessions"
    sessions_dir.mkdir(parents=True)

    session_id = "bbbb1111-cccc-dddd-eeee-ffffaaaa3333"
    marker = sessions_dir / f"{session_id}.json"
    sidecar = sessions_dir / f"{session_id}-piece-c.txt"
    marker.write_text("{}")
    sidecar.write_text("Piece C — recent commits (2 days ago), 1 commit(s) across 1 repo(s):\n")

    _set_mtime(marker, 10)  # 10 seconds old — should survive
    _set_mtime(sidecar, 10)

    module = _load_hook(tmp_path / "engram")
    module.prune_stale_session_markers("cccc2222-dddd-eeee-ffff-aaaabbbb4444")

    assert marker.exists(), "fresh marker should be kept"
    assert sidecar.exists(), "its piece-c sidecar should be kept too, not deleted unconditionally"


def test_prune_does_not_crash_when_marker_has_no_sidecar(tmp_path):
    """Most markers never had a Piece C sidecar at all (zero commits in the
    window, or a pre-#2859 marker) -- the prune step must not assume one
    exists."""
    sessions_dir = tmp_path / "engram" / "sessions"
    sessions_dir.mkdir(parents=True)

    session_id = "dddd3333-eeee-ffff-aaaa-bbbbcccc5555"
    marker = sessions_dir / f"{session_id}.json"
    marker.write_text("{}")
    _set_mtime(marker, 8 * 86400)

    module = _load_hook(tmp_path / "engram")
    module.prune_stale_session_markers("eeee4444-ffff-aaaa-bbbb-ccccdddd6666")  # must not raise

    assert not marker.exists(), "marker should still be deleted even with no sidecar present"
