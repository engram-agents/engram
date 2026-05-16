"""Tests for the per-session marker path (issue #140 retirement).

PR #160 retired the single shared ~/.engram/active-session.json in favor of
per-session markers at ~/.engram/sessions/<session_id>.json. These tests
exercise the new code paths in context_tracker.py:

- find_active_jsonl(session_id) reads the per-session marker by id.
- find_active_jsonl(None) returns None (no global fallback exists).
- _resolve_jsonl(transcript_path, session_id) prefers explicit transcript_path.
- _resolve_jsonl(None, session_id) falls back to per-session marker lookup.
- _resolve_jsonl(None, None) returns None.
- _detect_mode reads context_mode from the per-session marker.

The Gemini twin (hooks/gemini/context_tracker.py) carries identical logic;
covering the Claude path here is enough to catch regressions to the design
shared by both.
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path


def _load_context_tracker(engram_home: Path):
    """Load hooks/claude/context_tracker.py with ENGRAM_HOME pointing at engram_home."""
    os.environ["ENGRAM_HOME"] = str(engram_home)
    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / "hooks" / "claude" / "context_tracker.py"
    # Force a fresh import so module-level ENGRAM_HOME picks up the env var.
    if "context_tracker" in sys.modules:
        del sys.modules["context_tracker"]
    spec = importlib.util.spec_from_file_location("context_tracker", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_marker(engram_home: Path, session_id: str, transcript_path: str, **extra) -> None:
    sessions_dir = engram_home / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    marker = {"session_id": session_id, "transcript_path": transcript_path, **extra}
    (sessions_dir / f"{session_id}.json").write_text(json.dumps(marker))


def test_find_active_jsonl_returns_path_for_known_session(tmp_path):
    engram_home = tmp_path / "engram"
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("{}\n")
    _write_marker(engram_home, "sess-abc", str(transcript))
    ct = _load_context_tracker(engram_home)
    assert ct.find_active_jsonl("sess-abc") == str(transcript)


def test_find_active_jsonl_returns_none_without_session_id(tmp_path):
    """The global fallback was the source of issue #140 — verify it's gone."""
    ct = _load_context_tracker(tmp_path / "engram")
    assert ct.find_active_jsonl(None) is None
    assert ct.find_active_jsonl("") is None


def test_find_active_jsonl_returns_none_for_unknown_session(tmp_path):
    ct = _load_context_tracker(tmp_path / "engram")
    assert ct.find_active_jsonl("does-not-exist") is None


def test_find_active_jsonl_returns_none_when_transcript_missing(tmp_path):
    """Marker exists but transcript_path points at a missing file."""
    engram_home = tmp_path / "engram"
    _write_marker(engram_home, "sess-xyz", str(tmp_path / "missing.jsonl"))
    ct = _load_context_tracker(engram_home)
    assert ct.find_active_jsonl("sess-xyz") is None


def test_concurrent_sessions_resolve_to_their_own_transcripts(tmp_path):
    """The whole point of the retirement: two sessions can't clobber each other."""
    engram_home = tmp_path / "engram"
    t1 = tmp_path / "session1.jsonl"
    t2 = tmp_path / "session2.jsonl"
    t1.write_text("{}\n")
    t2.write_text("{}\n")
    _write_marker(engram_home, "sess-1", str(t1))
    _write_marker(engram_home, "sess-2", str(t2))
    ct = _load_context_tracker(engram_home)
    assert ct.find_active_jsonl("sess-1") == str(t1)
    assert ct.find_active_jsonl("sess-2") == str(t2)


def test_resolve_jsonl_prefers_explicit_transcript_path(tmp_path):
    engram_home = tmp_path / "engram"
    explicit = tmp_path / "explicit.jsonl"
    fallback = tmp_path / "fallback.jsonl"
    explicit.write_text("{}\n")
    fallback.write_text("{}\n")
    _write_marker(engram_home, "sess-1", str(fallback))
    ct = _load_context_tracker(engram_home)
    assert ct._resolve_jsonl(str(explicit), "sess-1") == str(explicit)


def test_resolve_jsonl_falls_back_to_session_marker(tmp_path):
    engram_home = tmp_path / "engram"
    fallback = tmp_path / "fallback.jsonl"
    fallback.write_text("{}\n")
    _write_marker(engram_home, "sess-1", str(fallback))
    ct = _load_context_tracker(engram_home)
    assert ct._resolve_jsonl(None, "sess-1") == str(fallback)
    assert ct._resolve_jsonl("", "sess-1") == str(fallback)


def test_resolve_jsonl_returns_none_when_both_args_empty(tmp_path):
    ct = _load_context_tracker(tmp_path / "engram")
    assert ct._resolve_jsonl(None, None) is None


def test_detect_mode_reads_context_mode_from_per_session_marker(tmp_path):
    engram_home = tmp_path / "engram"
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    _write_marker(engram_home, "sess-1m", str(transcript), context_mode="1m")
    _write_marker(engram_home, "sess-200k", str(transcript), context_mode="200k")
    ct = _load_context_tracker(engram_home)
    # Without an env-var override, the per-session marker's context_mode wins.
    os.environ.pop("ENGRAM_CONTEXT_MODE", None)
    assert ct._detect_mode(current_tokens=1000, jsonl_path=None, session_id="sess-1m") == "1m"
    assert ct._detect_mode(current_tokens=1000, jsonl_path=None, session_id="sess-200k") == "200k"


def test_detect_mode_env_var_overrides_marker(tmp_path):
    engram_home = tmp_path / "engram"
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    _write_marker(engram_home, "sess-1", str(transcript), context_mode="200k")
    ct = _load_context_tracker(engram_home)
    os.environ["ENGRAM_CONTEXT_MODE"] = "1m"
    try:
        assert ct._detect_mode(current_tokens=1000, jsonl_path=None, session_id="sess-1") == "1m"
    finally:
        os.environ.pop("ENGRAM_CONTEXT_MODE", None)


def test_detect_mode_no_session_id_falls_through(tmp_path):
    """When no session_id provided, _detect_mode shouldn't crash; falls through to other signals."""
    ct = _load_context_tracker(tmp_path / "engram")
    os.environ.pop("ENGRAM_CONTEXT_MODE", None)
    # With no signals, defaults to "200k" (the harness default per the existing logic).
    assert ct._detect_mode(current_tokens=1000, jsonl_path=None, session_id=None) == "200k"
