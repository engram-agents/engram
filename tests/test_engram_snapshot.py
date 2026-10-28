"""Tests for tools/engram-snapshot.

Coverage:
  - forum: snapshot (mock the forum read subprocess/import)
  - jsonl: span extraction (1-indexed inclusive, off-by-one boundaries)
  - content-addressed path layout (<sha[:2]>/<sha>.<ext>)
  - atomic-rename idempotency (same content twice → one file)
  - output JSON shape (all required fields present and typed correctly)
  - deferred-mode git_sha: null

Design notes:
  - We import the engram-snapshot module directly (no subprocess) for unit tests.
  - The forum: snapshot tests mock _forum_mod._api_get and _render_thread_human
    so the test suite doesn't need a running forum server.
  - ENGRAM_HOME and ENGRAM_EVIDENCE_ARCHIVE are overridden via env vars in each
    test to avoid polluting the real evidence archive.
"""

import contextlib
import hashlib
import http.client
import http.server
import importlib.machinery
import importlib.util
import ipaddress
import json
import os
import socket
import sys
import threading
import types
import unittest
import unittest.mock as mock
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_snapshot(
    engram_home: Path,
    evidence_archive: Path,
) -> types.ModuleType:
    """Load the engram-snapshot CLI module with paths overridden.

    The module is loaded fresh each call so module-level constants (which
    read ENGRAM_HOME at import time) reflect the overridden env vars.

    The script has no .py extension (CLI convention), so we must use the
    SourceFileLoader directly rather than spec_from_file_location, which
    returns None for extension-less files.
    """
    os.environ["ENGRAM_HOME"] = str(engram_home)
    os.environ["ENGRAM_EVIDENCE_ARCHIVE"] = str(evidence_archive)

    # Purge cached copy so constants reload
    for key in list(sys.modules):
        if key == "engram_snapshot":
            del sys.modules[key]

    script_path = _TOOLS_DIR / "engram-snapshot"
    loader = importlib.machinery.SourceFileLoader("engram_snapshot", str(script_path))
    spec = importlib.util.spec_from_loader("engram_snapshot", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helper: write a session marker
# ---------------------------------------------------------------------------

def _write_marker(
    engram_home: Path,
    session_id: str,
    transcript_path: str,
) -> None:
    sessions_dir = engram_home / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    marker = {"session_id": session_id, "transcript_path": transcript_path}
    (sessions_dir / f"{session_id}.json").write_text(json.dumps(marker))


# ---------------------------------------------------------------------------
# Helper: SHA-256
# ---------------------------------------------------------------------------

def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ============================================================================
# Tests: content-addressed path layout
# ============================================================================

class TestArchivePathLayout:
    """_archive_write puts files at <root>/<sha[:2]>/<sha>.<ext>."""

    def test_path_uses_first_two_chars_as_bucket(self, tmp_path):
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(tmp_path / "engram", evidence_archive)

        content = b"hello world"
        sha = _sha256(content)
        dest, returned_sha = snap._archive_write(content, "txt")

        assert returned_sha == sha
        assert dest.parent.name == sha[:2], "bucket dir should be first 2 chars of sha"
        assert dest.name == f"{sha}.txt", "filename should be full sha + ext"
        assert dest.parent.parent == evidence_archive

    def test_file_contents_match_input(self, tmp_path):
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(tmp_path / "engram", evidence_archive)

        content = b"test content bytes"
        dest, sha = snap._archive_write(content, "jsonl")
        assert dest.read_bytes() == content

    def test_jsonl_extension_for_jsonl_source(self, tmp_path):
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(tmp_path / "engram", evidence_archive)

        content = b'{"ts": "2026-01-01"}\n'
        dest, _ = snap._archive_write(content, "jsonl")
        assert dest.suffix == ".jsonl"

    def test_txt_extension_for_forum_source(self, tmp_path):
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(tmp_path / "engram", evidence_archive)

        content = b"rendered forum text\n"
        dest, _ = snap._archive_write(content, "txt")
        assert dest.suffix == ".txt"


# ============================================================================
# Tests: atomic-rename idempotency
# ============================================================================

class TestIdempotency:
    """Same content bytes written twice → exactly one file, same path returned."""

    def test_same_content_same_path(self, tmp_path):
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(tmp_path / "engram", evidence_archive)

        content = b"idempotency test content"
        dest1, sha1 = snap._archive_write(content, "txt")
        dest2, sha2 = snap._archive_write(content, "txt")

        assert dest1 == dest2, "same content → same path"
        assert sha1 == sha2

    def test_same_content_one_file(self, tmp_path):
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(tmp_path / "engram", evidence_archive)

        content = b"one file only"
        sha = _sha256(content)
        bucket = evidence_archive / sha[:2]

        snap._archive_write(content, "txt")
        snap._archive_write(content, "txt")

        files = list(bucket.glob(f"{sha}.*"))
        assert len(files) == 1, f"expected 1 file, found {len(files)}: {files}"

    def test_different_content_different_files(self, tmp_path):
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(tmp_path / "engram", evidence_archive)

        content_a = b"content A"
        content_b = b"content B"
        dest_a, sha_a = snap._archive_write(content_a, "txt")
        dest_b, sha_b = snap._archive_write(content_b, "txt")

        assert dest_a != dest_b
        assert sha_a != sha_b


# ============================================================================
# Tests: JSONL span extraction
# ============================================================================

class TestJsonlSpanExtraction:
    """_extract_jsonl_span — 1-indexed inclusive line range correctness."""

    def _make_jsonl(self, tmp_path: Path, n_lines: int = 10) -> Path:
        """Write a JSONL file with N lines, each {"line": N}."""
        p = tmp_path / "transcript.jsonl"
        lines = [json.dumps({"line": i + 1}) for i in range(n_lines)]
        p.write_text("\n".join(lines) + "\n")
        return p

    def _load_snap(self, tmp_path):
        return _load_snapshot(tmp_path / "engram", tmp_path / "archive")

    def test_span_1_to_1_extracts_first_line(self, tmp_path):
        snap = self._load_snap(tmp_path)
        jsonl = self._make_jsonl(tmp_path)
        result = snap._extract_jsonl_span(str(jsonl), 1, 1).decode()
        data = [json.loads(l) for l in result.strip().splitlines()]
        assert len(data) == 1
        assert data[0]["line"] == 1

    def test_span_inclusive_end(self, tmp_path):
        """Span 3:5 extracts lines 3, 4, and 5 — end is INCLUSIVE."""
        snap = self._load_snap(tmp_path)
        jsonl = self._make_jsonl(tmp_path, n_lines=10)
        result = snap._extract_jsonl_span(str(jsonl), 3, 5).decode()
        data = [json.loads(l) for l in result.strip().splitlines()]
        assert [d["line"] for d in data] == [3, 4, 5]

    def test_span_last_line(self, tmp_path):
        """Span 10:10 for a 10-line file extracts only the last line."""
        snap = self._load_snap(tmp_path)
        jsonl = self._make_jsonl(tmp_path, n_lines=10)
        result = snap._extract_jsonl_span(str(jsonl), 10, 10).decode()
        data = [json.loads(l) for l in result.strip().splitlines()]
        assert len(data) == 1
        assert data[0]["line"] == 10

    def test_span_beyond_end_clamps_to_file_length(self, tmp_path):
        """End past file length is clamped, not an error."""
        snap = self._load_snap(tmp_path)
        jsonl = self._make_jsonl(tmp_path, n_lines=5)
        result = snap._extract_jsonl_span(str(jsonl), 4, 99).decode()
        data = [json.loads(l) for l in result.strip().splitlines()]
        assert [d["line"] for d in data] == [4, 5]

    def test_off_by_one_boundary_start(self, tmp_path):
        """Span 2:4 does NOT include line 1 (off-by-one guard)."""
        snap = self._load_snap(tmp_path)
        jsonl = self._make_jsonl(tmp_path, n_lines=10)
        result = snap._extract_jsonl_span(str(jsonl), 2, 4).decode()
        data = [json.loads(l) for l in result.strip().splitlines()]
        assert data[0]["line"] == 2, "start should be 2, not 1"
        assert len(data) == 3

    def test_off_by_one_boundary_end(self, tmp_path):
        """Span 1:3 does NOT include line 4 (off-by-one guard)."""
        snap = self._load_snap(tmp_path)
        jsonl = self._make_jsonl(tmp_path, n_lines=10)
        result = snap._extract_jsonl_span(str(jsonl), 1, 3).decode()
        data = [json.loads(l) for l in result.strip().splitlines()]
        assert data[-1]["line"] == 3, "end should be 3, not 4"
        assert len(data) == 3

    def test_no_span_returns_all_lines(self, tmp_path):
        """Passing None for both start and end returns the full file."""
        snap = self._load_snap(tmp_path)
        jsonl = self._make_jsonl(tmp_path, n_lines=5)
        result = snap._extract_jsonl_span(str(jsonl), None, None).decode()
        data = [json.loads(l) for l in result.strip().splitlines()]
        assert [d["line"] for d in data] == [1, 2, 3, 4, 5]

    def test_invalid_span_start_zero_raises(self, tmp_path):
        """Start of 0 (not 1-indexed) raises ValueError."""
        snap = self._load_snap(tmp_path)
        jsonl = self._make_jsonl(tmp_path, n_lines=5)
        with pytest.raises(ValueError, match="less than 1"):
            snap._extract_jsonl_span(str(jsonl), 0, 3)

    def test_invalid_span_end_before_start_raises(self, tmp_path):
        """End before start raises ValueError."""
        snap = self._load_snap(tmp_path)
        jsonl = self._make_jsonl(tmp_path, n_lines=5)
        with pytest.raises(ValueError, match="before start"):
            snap._extract_jsonl_span(str(jsonl), 5, 2)

    def test_start_beyond_file_length_raises(self, tmp_path):
        """Start line beyond file length raises ValueError."""
        snap = self._load_snap(tmp_path)
        jsonl = self._make_jsonl(tmp_path, n_lines=3)
        with pytest.raises(ValueError, match="exceeds file length"):
            snap._extract_jsonl_span(str(jsonl), 10, 20)


# ============================================================================
# Tests: JSONL snapshot output JSON shape
# ============================================================================

class TestJsonlSnapshotOutput:
    """_snapshot_jsonl returns a well-shaped output dict."""

    def test_output_json_shape(self, tmp_path):
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        # Write a real JSONL transcript
        transcript = tmp_path / "sess.jsonl"
        transcript.write_text('{"line": 1}\n{"line": 2}\n')
        _write_marker(engram_home, "test-sess-001", str(transcript))

        result = snap._snapshot_jsonl("test-sess-001", 1, 2, {})

        # Required keys
        required_keys = {
            "file_url", "content_hash", "captured_at",
            "git_sha", "archive_status", "original_url",
            "source_type", "block_reason", "evidence_type",
        }
        assert required_keys == set(result.keys()), (
            f"Missing keys: {required_keys - set(result.keys())}, "
            f"Extra keys: {set(result.keys()) - required_keys}"
        )

        # Types and values
        assert result["file_url"].startswith("file://")
        assert len(result["content_hash"]) == 64  # sha256 hex
        assert result["git_sha"] is None, "deferred mode → git_sha must be null"
        assert result["archive_status"] == "archived"
        assert result["source_type"] == "jsonl"
        assert result["block_reason"] is None
        assert result["original_url"].startswith("file://")
        assert result["evidence_type"] == "session_transcript"

    def test_captured_at_is_utc_iso(self, tmp_path):
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        transcript = tmp_path / "sess.jsonl"
        transcript.write_text('{"ts": "2026-01-01"}\n')
        _write_marker(engram_home, "test-sess-002", str(transcript))

        result = snap._snapshot_jsonl("test-sess-002", None, None, {})
        captured = result["captured_at"]
        # Should parse as UTC ISO 8601 ending in Z
        from datetime import datetime, timezone
        dt = datetime.strptime(captured, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        assert dt is not None

    def test_file_url_matches_archive_path(self, tmp_path):
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        content = b'{"ts": "2026-01-01"}\n'
        transcript = tmp_path / "sess.jsonl"
        transcript.write_bytes(content)
        _write_marker(engram_home, "test-sess-003", str(transcript))

        result = snap._snapshot_jsonl("test-sess-003", None, None, {})
        sha = result["content_hash"]
        expected_path = evidence_archive / sha[:2] / f"{sha}.jsonl"
        assert result["file_url"] == f"file://{expected_path.resolve()}"

    def test_git_sha_is_null_in_deferred_mode(self, tmp_path):
        """Deferred mode (the only P1 mode) always emits git_sha: null."""
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        transcript = tmp_path / "sess.jsonl"
        transcript.write_text('{"ts": "2026-06-21"}\n')
        _write_marker(engram_home, "test-sess-004", str(transcript))

        result = snap._snapshot_jsonl("test-sess-004", None, None, {})
        assert result["git_sha"] is None


# ============================================================================
# Tests: forum: snapshot (mock the forum HTTP layer)
# ============================================================================

class TestForumSnapshot:
    """_snapshot_forum archives a forum thread and returns correct JSON shape."""

    def _make_fake_forum_mod(self) -> types.ModuleType:
        """Build a minimal mock of forum.py sufficient for _forum_read_rendered."""
        fake = types.ModuleType("forum")
        fake._FORUM_URL_CACHE = None

        def _api_get(url, params=None, swallow_errors=False):
            return {
                "thread": {
                    "id": 119,
                    "title": "Test thread",
                    "category_slug": "inter-agent",
                    "author": {"name": "borges"},
                    "created_at": "2026-06-21T10:00:00Z",
                    "last_activity_at": "2026-06-21T11:00:00Z",
                    "pinned": False,
                    "unresolved": False,
                },
                "posts": [
                    {
                        "id": 1,
                        "author": {"name": "borges"},
                        "created_at": "2026-06-21T10:00:00Z",
                        "edited_at": None,
                        "body_md": "This is the OP body.",
                    },
                    {
                        "id": 2,
                        "author": {"name": "ariadne"},
                        "created_at": "2026-06-21T11:00:00Z",
                        "edited_at": None,
                        "body_md": "This is a reply.",
                    },
                ],
            }

        def _render_thread_human(thread, posts):
            import sys as _sys
            print(f"=== Thread #{thread.get('id', '?')} ===")
            for post in posts:
                print(post.get("body_md", ""))

        fake._api_get = _api_get
        fake._render_thread_human = _render_thread_human
        return fake

    def test_forum_snapshot_output_shape(self, tmp_path):
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        fake_forum = self._make_fake_forum_mod()
        with mock.patch.dict(sys.modules, {"forum": fake_forum}):
            result = snap._snapshot_forum(119, {}, "borges")

        required_keys = {
            "file_url", "content_hash", "captured_at",
            "git_sha", "archive_status", "original_url",
            "source_type", "block_reason", "evidence_type",
        }
        assert required_keys == set(result.keys())
        assert result["source_type"] == "forum"
        assert result["git_sha"] is None
        assert result["archive_status"] == "archived"
        assert result["block_reason"] is None
        assert result["evidence_type"] == "forum_thread"

    def test_forum_snapshot_original_url(self, tmp_path):
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        fake_forum = self._make_fake_forum_mod()
        with mock.patch.dict(sys.modules, {"forum": fake_forum}):
            result = snap._snapshot_forum(119, {}, "borges")

        # original_url should be the LAN forum URL for this thread
        assert "119" in result["original_url"]
        assert result["original_url"].startswith("http")

    def test_forum_snapshot_file_url_starts_with_file(self, tmp_path):
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        fake_forum = self._make_fake_forum_mod()
        with mock.patch.dict(sys.modules, {"forum": fake_forum}):
            result = snap._snapshot_forum(119, {}, "borges")

        assert result["file_url"].startswith("file://")

    def test_forum_snapshot_archive_file_exists_and_is_text(self, tmp_path):
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        fake_forum = self._make_fake_forum_mod()
        with mock.patch.dict(sys.modules, {"forum": fake_forum}):
            result = snap._snapshot_forum(119, {}, "borges")

        archive_path = Path(result["file_url"][len("file://"):])
        assert archive_path.exists()
        assert archive_path.suffix == ".txt"
        text = archive_path.read_text()
        assert "119" in text  # thread id should appear in the rendered text

    def test_forum_snapshot_content_hash_matches_file(self, tmp_path):
        """content_hash is the SHA-256 of the actual archived bytes."""
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        fake_forum = self._make_fake_forum_mod()
        with mock.patch.dict(sys.modules, {"forum": fake_forum}):
            result = snap._snapshot_forum(119, {}, "borges")

        archive_path = Path(result["file_url"][len("file://"):])
        actual_sha = _sha256(archive_path.read_bytes())
        assert result["content_hash"] == actual_sha

    def test_forum_snapshot_idempotent(self, tmp_path):
        """Snapshotting the same thread twice → one file, same hash."""
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        fake_forum = self._make_fake_forum_mod()
        with mock.patch.dict(sys.modules, {"forum": fake_forum}):
            result1 = snap._snapshot_forum(119, {}, "borges")
            result2 = snap._snapshot_forum(119, {}, "borges")

        assert result1["content_hash"] == result2["content_hash"]
        assert result1["file_url"] == result2["file_url"]

        sha = result1["content_hash"]
        bucket = evidence_archive / sha[:2]
        files = list(bucket.glob(f"{sha}.*"))
        assert len(files) == 1


# ============================================================================
# Tests: bare jsonl: (current session) fallback
# ============================================================================

class TestBareJsonlSource:
    """Bare 'jsonl:' source falls back to most-recently-modified marker."""

    def test_bare_jsonl_resolves_current_session(self, tmp_path):
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        transcript = tmp_path / "current_sess.jsonl"
        transcript.write_text('{"ts": "2026-06-21"}\n')
        _write_marker(engram_home, "curr-sess-001", str(transcript))

        # _find_session_transcript(None) should resolve the most recent marker
        resolved = snap._find_session_transcript(None)
        assert resolved == str(transcript)

    def test_bare_jsonl_returns_none_when_no_markers(self, tmp_path):
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        result = snap._find_session_transcript(None)
        assert result is None

    def test_specific_session_id_resolves_correct_marker(self, tmp_path):
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        t1 = tmp_path / "sess1.jsonl"
        t2 = tmp_path / "sess2.jsonl"
        t1.write_text('{"line": 1}\n')
        t2.write_text('{"line": 2}\n')
        _write_marker(engram_home, "sess-aaa", str(t1))
        _write_marker(engram_home, "sess-bbb", str(t2))

        assert snap._find_session_transcript("sess-aaa") == str(t1)
        assert snap._find_session_transcript("sess-bbb") == str(t2)


# ============================================================================
# Tests: source parsing
# ============================================================================

class TestSourceParsing:
    def test_parse_forum_source(self, tmp_path):
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        source_type, source_id = snap._parse_source("forum:119")
        assert source_type == "forum"
        assert source_id == 119

    def test_parse_jsonl_source_with_id(self, tmp_path):
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        source_type, source_id = snap._parse_source("jsonl:abc-123")
        assert source_type == "jsonl"
        assert source_id == "abc-123"

    def test_parse_bare_jsonl_source(self, tmp_path):
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        source_type, source_id = snap._parse_source("jsonl:")
        assert source_type == "jsonl"
        assert source_id is None

    def test_parse_forum_invalid_id_exits(self, tmp_path):
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with pytest.raises(SystemExit) as exc_info:
            snap._parse_source("forum:not-an-int")
        assert exc_info.value.code == snap.EXIT_VALIDATION

    def test_parse_unknown_source_exits(self, tmp_path):
        """web: is now implemented (#2606 facet 1) -- use a genuinely
        unrecognised prefix instead."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with pytest.raises(SystemExit) as exc_info:
            snap._parse_source("ftp:example.com/file")
        assert exc_info.value.code == snap.EXIT_VALIDATION

    def test_parse_file_remote_authority_exits(self, tmp_path):
        """file://host/path is rejected; file: is local-only."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with pytest.raises(SystemExit) as exc_info:
            snap._parse_source("file://host/etc/passwd")
        assert exc_info.value.code == snap.EXIT_VALIDATION

    def test_parse_web_source(self, tmp_path):
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        source_type, url = snap._parse_source("web:https://example.com/page")
        assert source_type == "web"
        assert url == "https://example.com/page"

    def test_parse_pdf_source(self, tmp_path):
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        source_type, url = snap._parse_source("pdf:https://example.com/paper.pdf")
        assert source_type == "pdf"
        assert url == "https://example.com/paper.pdf"

    def test_parse_web_https_source(self, tmp_path):
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        source_type, url = snap._parse_source("web:http://example.com/page")
        assert source_type == "web"
        assert url == "http://example.com/page"

    def test_parse_web_non_http_scheme_rejected(self, tmp_path):
        """web:/pdf: only accept http(s) URLs -- rejecting e.g. file:// stops
        urllib's own file:// support from becoming reachable (and therefore a
        local-file-read surface) through this fetch-only prefix."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with pytest.raises(SystemExit) as exc_info:
            snap._parse_source("web:file:///etc/passwd")
        assert exc_info.value.code == snap.EXIT_VALIDATION

    def test_parse_pdf_non_http_scheme_rejected(self, tmp_path):
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with pytest.raises(SystemExit) as exc_info:
            snap._parse_source("pdf:ftp://example.com/paper.pdf")
        assert exc_info.value.code == snap.EXIT_VALIDATION

    def test_parse_bare_web_colon_exits(self, tmp_path):
        """Bare 'web:' (no URL) exits with EXIT_VALIDATION."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with pytest.raises(SystemExit) as exc_info:
            snap._parse_source("web:")
        assert exc_info.value.code == snap.EXIT_VALIDATION

    def test_parse_bare_pdf_colon_exits(self, tmp_path):
        """Bare 'pdf:' (no URL) exits with EXIT_VALIDATION."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with pytest.raises(SystemExit) as exc_info:
            snap._parse_source("pdf:")
        assert exc_info.value.code == snap.EXIT_VALIDATION


# ============================================================================
# Tests: span parsing
# ============================================================================

class TestSpanParsing:
    def test_parse_valid_span(self, tmp_path):
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        start, end = snap._parse_span("10:20")
        assert start == 10
        assert end == 20

    def test_parse_span_none_returns_none_none(self, tmp_path):
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        start, end = snap._parse_span(None)
        assert start is None
        assert end is None

    def test_parse_span_bad_format_exits(self, tmp_path):
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with pytest.raises(SystemExit) as exc_info:
            snap._parse_span("10-20")  # wrong delimiter
        assert exc_info.value.code == snap.EXIT_VALIDATION

    def test_parse_span_non_int_exits(self, tmp_path):
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with pytest.raises(SystemExit) as exc_info:
            snap._parse_span("a:b")
        assert exc_info.value.code == snap.EXIT_VALIDATION


# ============================================================================
# Tests: file: snapshot
# ============================================================================

class TestSnapshotFile:
    """_snapshot_file archives a local file and returns correct JSON shape."""

    def _load_snap(self, tmp_path):
        return _load_snapshot(tmp_path / "engram", tmp_path / "archive")

    def test_archives_a_real_file(self, tmp_path):
        """Basic round-trip: write a .py file, snapshot it, check all fields."""
        snap = self._load_snap(tmp_path)
        evidence_archive = tmp_path / "archive"

        src = tmp_path / "x.py"
        src.write_bytes(b"# hello\nprint('world')\n")
        content = src.read_bytes()
        expected_sha = _sha256(content)

        result = snap._snapshot_file(str(src), {})

        assert result["source_type"] == "file"
        assert result["content_hash"] == expected_sha
        assert result["git_sha"] is None
        assert result["archive_status"] == "archived"
        assert result["block_reason"] is None
        assert result["original_url"].startswith("file://")
        assert result["original_url"].endswith(str(src.resolve()))

        # Archive file exists at <root>/<sha[:2]>/<sha>.py
        archive_file = evidence_archive / expected_sha[:2] / f"{expected_sha}.py"
        assert archive_file.exists(), f"archive file missing: {archive_file}"
        assert archive_file.read_bytes() == content

    def test_content_addressing_is_idempotent(self, tmp_path):
        """Snapshotting the same file twice produces identical hash and path."""
        snap = self._load_snap(tmp_path)

        src = tmp_path / "repeat.txt"
        src.write_bytes(b"idempotent content\n")

        result1 = snap._snapshot_file(str(src), {})
        result2 = snap._snapshot_file(str(src), {})

        assert result1["content_hash"] == result2["content_hash"]
        assert result1["file_url"] == result2["file_url"]

        # Only one file in the archive bucket
        sha = result1["content_hash"]
        evidence_archive = tmp_path / "archive"
        bucket = evidence_archive / sha[:2]
        files = list(bucket.glob(f"{sha}.*"))
        assert len(files) == 1, f"expected 1 file, found {len(files)}: {files}"

    def test_extension_fallback_for_extensionless_file(self, tmp_path):
        """A file with no extension uses .bin in the archive."""
        snap = self._load_snap(tmp_path)
        evidence_archive = tmp_path / "archive"

        src = tmp_path / "noext"
        src.write_bytes(b"binary content\x00\x01\x02")

        result = snap._snapshot_file(str(src), {})
        archive_path = result["file_url"][len("file://"):]
        assert archive_path.endswith(".bin"), f"expected .bin, got: {archive_path}"

    def test_tilde_expansion(self, tmp_path, monkeypatch):
        """A ~-prefixed path resolves correctly."""
        snap = self._load_snap(tmp_path)

        # Place a file in tmp_path and make ~ resolve to tmp_path
        monkeypatch.setenv("HOME", str(tmp_path))
        src = tmp_path / "myfile.md"
        src.write_bytes(b"# A note\n")

        result = snap._snapshot_file("~/myfile.md", {})
        assert result["source_type"] == "file"
        assert result["content_hash"] == _sha256(b"# A note\n")

    def test_relative_path_resolves_against_cwd(self, tmp_path, monkeypatch):
        """A relative path resolves against the current working directory."""
        snap = self._load_snap(tmp_path)
        monkeypatch.chdir(tmp_path)

        src = tmp_path / "relative.txt"
        src.write_bytes(b"relative content\n")

        result = snap._snapshot_file("relative.txt", {})
        assert result["source_type"] == "file"
        assert result["content_hash"] == _sha256(b"relative content\n")

    def test_error_nonexistent_path(self, tmp_path, capsys):
        """Nonexistent path exits with EXIT_STATE; stderr mentions 'regular file'."""
        snap = self._load_snap(tmp_path)

        nonexistent = str(tmp_path / "does_not_exist.py")
        with pytest.raises(SystemExit) as exc_info:
            snap._snapshot_file(nonexistent, {})
        assert exc_info.value.code == snap.EXIT_STATE
        assert "regular file" in capsys.readouterr().err

    def test_error_directory(self, tmp_path):
        """Passing a directory exits with EXIT_STATE."""
        snap = self._load_snap(tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            snap._snapshot_file(str(tmp_path), {})
        assert exc_info.value.code == snap.EXIT_STATE

    def test_error_bare_file_colon(self, tmp_path):
        """Bare 'file:' (no path) exits with EXIT_VALIDATION."""
        snap = self._load_snap(tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            snap._parse_source("file:")
        assert exc_info.value.code == snap.EXIT_VALIDATION

    def test_file_uri_normalization(self, tmp_path):
        """_parse_source('file:///etc/hostname') returns ('file', '/etc/hostname')."""
        snap = self._load_snap(tmp_path)

        source_type, path_str = snap._parse_source("file:///etc/hostname")
        assert source_type == "file"
        assert path_str == "/etc/hostname"

    def test_dotfile_archives_as_bin(self, tmp_path):
        """A dotfile (.bashrc) has an empty Path.suffix and archives with .bin extension."""
        snap = self._load_snap(tmp_path)
        evidence_archive = tmp_path / "archive"

        src = tmp_path / ".bashrc"
        src.write_bytes(b"export PATH=$PATH:/usr/local/bin\n")
        content = src.read_bytes()
        expected_sha = _sha256(content)

        result = snap._snapshot_file(str(src), {})

        archive_path = result["file_url"][len("file://"):]
        assert archive_path.endswith(".bin"), f"dotfile should archive as .bin, got: {archive_path}"
        # Traceability via original_url
        assert result["original_url"].endswith(str(src.resolve()))
        # Archive file exists
        archive_file = evidence_archive / expected_sha[:2] / f"{expected_sha}.bin"
        assert archive_file.exists(), f"archive file missing: {archive_file}"

    def test_output_json_has_all_required_keys(self, tmp_path):
        """The output dict has exactly the required 9 keys."""
        snap = self._load_snap(tmp_path)

        src = tmp_path / "schema_check.json"
        src.write_bytes(b'{"key": "value"}\n')

        result = snap._snapshot_file(str(src), {})

        required_keys = {
            "file_url", "content_hash", "captured_at",
            "git_sha", "archive_status", "original_url",
            "source_type", "block_reason", "evidence_type",
        }
        assert required_keys == set(result.keys()), (
            f"Missing: {required_keys - set(result.keys())}, "
            f"Extra: {set(result.keys()) - required_keys}"
        )
        assert result["evidence_type"] == "local_file"


# ============================================================================
# Tests: web:/pdf: snapshot (#2606 facet 1 unit 1) -- happy path
# ============================================================================

_DECLARED_LENGTH_HONEST = object()


class _FakeHTTPResponse:
    """Minimal file-like context manager mimicking urllib's response object,
    enough to exercise _fetch_url_bytes's chunked-read loop.

    `length` models http.client.HTTPResponse.length -- BYTES STILL OWED, not
    the body size -- because _fetch_url_bytes's short-read check reads exactly
    that attribute.  A stub that OMITS it does not merely under-model the real
    object: `getattr(resp, "length", None)` then yields None for every test
    patching the _urlopen seam with this class, so the short-read check is
    SILENTLY SKIPPED and those tests only LOOK like they exercise the fetch
    path.  Every future test written against this stub would inherit that skip
    with nothing to announce it.

    Like the real object it starts at the declared body length and is
    decremented by each read(), so an honest stub lands on 0 -- nothing owed.
    Pass `declared_length=<n>` to model a server that under-delivers against
    its own declaration (a positive residue), or `declared_length=None` to
    model a response with no authoritative length at all -- chunked, or framed
    by the close -- which is what http.client leaves the attribute for those.
    """

    def __init__(
        self,
        data: bytes,
        status: int = 200,
        declared_length=_DECLARED_LENGTH_HONEST,
    ):
        self._data = data
        self.status = status
        self._pos = 0
        self.length = (
            len(data)
            if declared_length is _DECLARED_LENGTH_HONEST
            else declared_length
        )

    def getcode(self):
        return self.status

    def read(self, n=-1):
        if n is None or n < 0:
            chunk = self._data[self._pos:]
            self._pos = len(self._data)
        else:
            chunk = self._data[self._pos:self._pos + n]
            self._pos += len(chunk)
        if self.length is not None:
            self.length -= len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class _LengthlessResponse:
    """A response object that models NO `length` at all -- deliberately NOT a
    subclass of the stub above.

    This is the case `getattr(resp, "length", None)` exists for, and giving
    _FakeHTTPResponse an honest `length` is exactly what stops it covering
    that case: with every stub in the file now carrying the attribute, the
    bare `resp.length` form passes the whole file, where before it reddened
    three happy-path tests.  So the defence has to be re-pinned by an object
    that genuinely lacks the attribute -- a hand-rolled or third-party
    response, which "nothing owed" is the right reading of, never an
    AttributeError raised inside the fetch path.
    """

    def __init__(self, data: bytes, status: int = 200):
        self._data = data
        self.status = status
        self._pos = 0

    def getcode(self):
        return self.status

    def read(self, n=-1):
        if n is None or n < 0:
            chunk = self._data[self._pos:]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class TestStubResponseModelsBytesOwed:
    """The stub above must MODEL the short-read check, never disable it.

    Before this, `_FakeHTTPResponse` defined `_data`, `status` and `_pos` and
    no `length`, so `getattr(resp, "length", None)` returned None on every one
    of the four tests that patch _urlopen with it -- the check was skipped, not
    exercised, and the skip was invisible.  These rows pin the replacement in
    both directions: an honest stub must reach zero owed, and a stub that
    under-delivers must trip the same loud refusal the raw-socket rows below
    get from a real server.  Without the attack row the new attribute would be
    untested and could be wrong in the SAFE direction -- always-None, always-0
    -- which is indistinguishable from the defect it replaces.
    """

    URL = "https://example.com/doc"
    MAX_BYTES = 100_000

    def _fetch(self, snap, monkeypatch, resp):
        monkeypatch.setattr(snap, "_urlopen", lambda req, timeout=None: resp)
        return snap._fetch_url_bytes(self.URL, 5.0, self.MAX_BYTES)

    def test_stub_under_delivering_its_declared_length_is_refused_LOUDLY(
        self, tmp_path, monkeypatch
    ):
        """10 bytes behind a declared 1000 -> the same refusal a real server
        gets, proving the stub now reaches the check at all."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        resp = _FakeHTTPResponse(b"x" * 10, declared_length=1000)
        with pytest.raises(RuntimeError) as exc_info:
            self._fetch(snap, monkeypatch, resp)
        message = str(exc_info.value)
        assert "short read" in message, message
        # The OWED count, not the declared 1000 or the delivered 10 -- the
        # same accounting the raw-socket short-read row pins.
        assert "990 bytes" in message, message
        assert self.URL in message, message

    def test_CONTROL_an_honest_stub_reaches_zero_owed_and_succeeds(
        self, tmp_path, monkeypatch
    ):
        """THE control for the row above, and the one that pins the DECREMENT.

        A stub that sets `length` and never decrements it lands on a positive
        residue for every honest body, which would refuse the four pre-existing
        happy-path tests as well as this one.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        resp = _FakeHTTPResponse(b"x" * 10)
        assert self._fetch(snap, monkeypatch, resp) == b"x" * 10
        assert resp.length == 0, (
            "an honest stub must end the read owing nothing, the way "
            f"http.client's own HTTPResponse does; got {resp.length!r}"
        )

    def test_CONTROL_a_stub_declaring_NO_length_still_succeeds(
        self, tmp_path, monkeypatch
    ):
        """The stub's analogue of a chunked / close-framed response: nothing
        was ever declared, so nothing can be owed, and None must read as
        "nothing owed" rather than as a failure."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        resp = _FakeHTTPResponse(b"x" * 10, declared_length=None)
        assert self._fetch(snap, monkeypatch, resp) == b"x" * 10
        assert resp.length is None, (
            "a response with no declared length must keep length None across "
            f"the read; got {resp.length!r}"
        )

    def test_CONTROL_a_response_object_with_NO_length_ATTRIBUTE_succeeds(
        self, tmp_path, monkeypatch
    ):
        """The getattr DEFAULT, which giving the stub a length would otherwise
        have left unguarded.

        `owed = getattr(resp, "length", None)` is written with a default on
        purpose -- _urlopen is a patched seam, so an object modelling no
        length must mean "nothing owed", never an AttributeError raised inside
        the one function whose contract is to fail loudly through its wrapper.
        Measured: against the pre-change stub, rewriting it as bare
        `resp.length` reddened three happy-path tests; against the honest stub
        it passes the entire file.  This row is what replaces that accidental
        coverage with a deliberate one.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        resp = _LengthlessResponse(b"x" * 10)
        assert not hasattr(resp, "length"), (
            "this row is worthless unless the object really lacks the "
            "attribute -- that absence IS the condition under test"
        )
        assert self._fetch(snap, monkeypatch, resp) == b"x" * 10


class TestSnapshotWebPdfHappyPath:
    """_snapshot_web / _snapshot_pdf archive raw fetched bytes and return the
    correct JSON shape, the _urlopen seam mocked so no real network
    call occurs."""

    def test_archives_web_page_raw_bytes(self, tmp_path, monkeypatch):
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        body = b"<html><body>hello world</body></html>"

        def _fake_urlopen(req, timeout=None):
            return _FakeHTTPResponse(body, status=200)

        monkeypatch.setattr(snap, "_urlopen", _fake_urlopen)

        result = snap._snapshot_web("https://example.com/page", {})

        required_keys = {
            "file_url", "content_hash", "captured_at",
            "git_sha", "archive_status", "original_url",
            "source_type", "block_reason", "evidence_type",
        }
        assert required_keys == set(result.keys())
        assert result["source_type"] == "web"
        assert result["evidence_type"] == "web_page"
        assert result["original_url"] == "https://example.com/page"
        assert result["git_sha"] is None
        assert result["archive_status"] == "archived"
        assert result["block_reason"] is None

        archive_path = Path(result["file_url"][len("file://"):])
        assert archive_path.exists()
        assert archive_path.suffix == ".html"
        assert archive_path.read_bytes() == body
        assert result["content_hash"] == _sha256(body)

    def test_archives_pdf_raw_bytes(self, tmp_path, monkeypatch):
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        body = b"%PDF-1.4 fake pdf bytes for testing\n%%EOF"

        def _fake_urlopen(req, timeout=None):
            return _FakeHTTPResponse(body, status=200)

        monkeypatch.setattr(snap, "_urlopen", _fake_urlopen)

        result = snap._snapshot_pdf("https://example.com/paper.pdf", {})

        assert result["source_type"] == "pdf"
        assert result["evidence_type"] == "pdf"
        assert result["original_url"] == "https://example.com/paper.pdf"

        archive_path = Path(result["file_url"][len("file://"):])
        assert archive_path.exists()
        assert archive_path.suffix == ".pdf"
        assert archive_path.read_bytes() == body
        assert result["content_hash"] == _sha256(body)

    def test_web_snapshot_idempotent(self, tmp_path, monkeypatch):
        """Snapshotting the same URL twice (same bytes) -> one archive file."""
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        body = b"<html>stable content</html>"

        def _fake_urlopen(req, timeout=None):
            return _FakeHTTPResponse(body, status=200)

        monkeypatch.setattr(snap, "_urlopen", _fake_urlopen)

        result1 = snap._snapshot_web("https://example.com/stable", {})
        result2 = snap._snapshot_web("https://example.com/stable", {})

        assert result1["content_hash"] == result2["content_hash"]
        assert result1["file_url"] == result2["file_url"]

        sha = result1["content_hash"]
        bucket = evidence_archive / sha[:2]
        files = list(bucket.glob(f"{sha}.*"))
        assert len(files) == 1


# ============================================================================
# Tests: web:/pdf: fetch FAILURES (#2606 facet 1 unit 1) -- fail loudly,
# write NOTHING to the archive.
#
# Each test asserts BOTH the non-zero exit code AND the absence of any
# archived file. The second assertion is the one that matters -- the first
# passes for the wrong reasons if the write happened before the failure was
# detected (a half-written archive file whose hash names truncated content
# is worse than no archive at all, per the spec's own live example: a
# transcript archived at 26,582,953 bytes against a live 27,282,639, where
# sha256(archived) == sha256(live[:26582953]) -- content-identity scored a
# truncated ancestor as a DIFFERENT file rather than as a partial one).
# ============================================================================

def _archive_is_empty(evidence_archive: Path) -> bool:
    if not evidence_archive.exists():
        return True
    return not any(evidence_archive.rglob("*"))


class TestSnapshotWebPdfFetchFailures:
    def test_non_2xx_status_exits_and_writes_nothing(self, tmp_path, monkeypatch):
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        def _raise_http_error(req, timeout=None):
            raise snap.urllib.error.HTTPError(
                "https://example.com/gone", 404, "Not Found", {}, None
            )

        monkeypatch.setattr(snap, "_urlopen", _raise_http_error)

        with pytest.raises(SystemExit) as exc_info:
            snap._snapshot_web("https://example.com/gone", {})
        assert exc_info.value.code == snap.EXIT_IO
        assert _archive_is_empty(evidence_archive), (
            "a failed fetch must not leave any file under the archive root"
        )

    def test_timeout_exits_and_writes_nothing(self, tmp_path, monkeypatch):
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        def _raise_timeout(req, timeout=None):
            raise TimeoutError("timed out")

        monkeypatch.setattr(snap, "_urlopen", _raise_timeout)

        with pytest.raises(SystemExit) as exc_info:
            snap._snapshot_web("https://example.com/slow", {})
        assert exc_info.value.code == snap.EXIT_IO
        assert _archive_is_empty(evidence_archive), (
            "a failed fetch must not leave any file under the archive root"
        )

    def test_dns_failure_exits_and_writes_nothing(self, tmp_path, monkeypatch):
        """URLError (DNS/connection failure) -- distinct code path from
        HTTPError and from a raw socket timeout."""
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        def _raise_url_error(req, timeout=None):
            raise snap.urllib.error.URLError("Name or service not known")

        monkeypatch.setattr(snap, "_urlopen", _raise_url_error)

        with pytest.raises(SystemExit) as exc_info:
            snap._snapshot_web("https://does-not-exist.invalid/page", {})
        assert exc_info.value.code == snap.EXIT_IO
        assert _archive_is_empty(evidence_archive), (
            "a failed fetch must not leave any file under the archive root"
        )

    def test_oversize_exits_and_writes_nothing(self, tmp_path, monkeypatch):
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        # Shrink the size cap so a small fake body still trips it, rather
        # than fabricating a multi-MB fixture.
        monkeypatch.setattr(snap, "WEB_FETCH_MAX_BYTES", 10)
        oversized_body = b"x" * 1000

        def _fake_urlopen(req, timeout=None):
            return _FakeHTTPResponse(oversized_body, status=200)

        monkeypatch.setattr(snap, "_urlopen", _fake_urlopen)

        with pytest.raises(SystemExit) as exc_info:
            snap._snapshot_web("https://example.com/huge", {})
        assert exc_info.value.code == snap.EXIT_IO
        assert _archive_is_empty(evidence_archive), (
            "an oversize fetch must not leave any (truncated) file under "
            "the archive root -- a truncated file's hash would otherwise "
            "certify the truncation as if it were the whole document"
        )

    def test_pdf_non_2xx_exits_and_writes_nothing(self, tmp_path, monkeypatch):
        """pdf: shares _fetch_url_bytes/the archive-after-fetch ordering
        with web: -- spot-check rather than repeating every failure mode."""
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        def _raise_http_error(req, timeout=None):
            raise snap.urllib.error.HTTPError(
                "https://example.com/paper.pdf", 403, "Forbidden", {}, None
            )

        monkeypatch.setattr(snap, "_urlopen", _raise_http_error)

        with pytest.raises(SystemExit) as exc_info:
            snap._snapshot_pdf("https://example.com/paper.pdf", {})
        assert exc_info.value.code == snap.EXIT_IO
        assert _archive_is_empty(evidence_archive), (
            "a failed fetch must not leave any file under the archive root"
        )


class TestMalformedAuthorityFailsLoudly:
    """A URL whose authority carries an UNUSABLE PORT must fail through
    _fetch_url_bytes's own RuntimeError wrapper, never as a bare ValueError.

    _fetch_url_bytes's contract is that it "must fail LOUDLY" and that its
    only failure surface is RuntimeError -- and _snapshot_web/_snapshot_pdf
    take it at its word, catching RuntimeError alone. So a ValueError that
    escapes it does not merely look ugly: it walks straight past that
    `except RuntimeError` and reaches the operator as a raw traceback out of
    the CLI, instead of the tool's clean EXIT_IO message.

    There are TWO independent sources, covered separately below because only
    one of them is new:

      * ``urlparse(...).port`` raises ValueError for a non-numeric or
        out-of-0..65535 port ('h:abc', 'h:99999').  PRE-EXISTING -- reached
        via _effective_port, identically before and after #2691's authority
        work.
      * ``InheritedAuthority.__post_init__`` raises ValueError for port 0.
        INTRODUCED by #2691's range check: port 0 previously reached the
        network layer and came back as an ordinary connection failure.

    Both are reached through _urlopen -> _SafeRedirectHandler.__init__ ->
    _resolve_inherited, i.e. BEFORE any connection is opened, so nothing
    partial can have been read in either case.

    These tests deliberately do NOT patch the _urlopen seam the rest of this
    file patches: _urlopen is exactly where the ValueError is raised, so a
    stubbed one would test nothing.
    """

    # A valid port that nothing is listening on.  Used by the control below.
    CLOSED_PORT_URL = "http://127.0.0.1:9/x"

    @staticmethod
    def _assert_loudly_wrapped(snap, url):
        """The shared assertion: a RuntimeError in the documented shape.

        Returns the exception so each caller can additionally pin WHICH cause
        it travelled through -- which is what keeps the malformed-port cases
        and the control below distinguishable rather than merely all-red.
        """
        with pytest.raises(RuntimeError) as exc_info:
            snap._fetch_url_bytes(url, 2.0, 1000)
        message = str(exc_info.value)
        assert message.startswith(f"failed to fetch '{url}'"), (
            "the wrapped error must name the URL the operator asked for, in "
            f"the same shape as every other fetch failure; got: {message}"
        )
        return exc_info.value

    def test_port_zero_is_wrapped_not_a_bare_ValueError(self, tmp_path):
        """Port 0 -- the half #2691 INTRODUCED.

        The range check in InheritedAuthority.__post_init__ is correct and
        stays: port 0 is not a destination, and refusing it is the point.
        What is fixed here is only HOW it is refused.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        exc = self._assert_loudly_wrapped(snap, "http://127.0.0.1:0/x")
        assert isinstance(exc.__cause__, ValueError), (
            "the wrap must preserve the ValueError as __cause__, so the "
            "operator still gets the underlying reason and the traceback is "
            "still diagnosable"
        )
        assert "1..65535" in str(exc), (
            f"the reason should be the range refusal itself; got: {exc}"
        )

    def test_out_of_range_port_is_wrapped_not_a_bare_ValueError(self, tmp_path):
        """A port above 65535 -- PRE-EXISTING, raised by urlparse itself.

        Not merely a typo the operator can see: the same escape is reachable
        from an attacker-controlled ``Location:`` header (see
        TestMalformedRedirectAuthority), which is why this half is handled in
        the same pass rather than left as a follow-up.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        exc = self._assert_loudly_wrapped(snap, "http://127.0.0.1:99999/x")
        assert isinstance(exc.__cause__, ValueError), exc.__cause__

    def test_non_numeric_port_is_wrapped_not_a_bare_ValueError(self, tmp_path):
        """A non-numeric port -- PRE-EXISTING, the other urlparse raise."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        exc = self._assert_loudly_wrapped(snap, "http://127.0.0.1:abc/x")
        assert isinstance(exc.__cause__, ValueError), exc.__cause__

    def test_CONTROL_closed_but_valid_port_wraps_by_the_ORDINARY_path(
        self, tmp_path
    ):
        """THE CONTROL, and it is the reason the three above mean anything.

        Port 9 is well-formed and simply refuses the connection, so it never
        touches the ValueError path at all -- it reaches the network and comes
        back through the pre-existing URLError clause. It must produce the
        SAME loud wrapper, via a DIFFERENT cause.

        Without it, all four rows agreeing is equally consistent with a
        harness that never reaches _fetch_url_bytes, or with a fix that turns
        every fetch into a RuntimeError. Pinning __cause__ is what makes the
        rows discriminate: URLError here, ValueError above.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        exc = self._assert_loudly_wrapped(snap, self.CLOSED_PORT_URL)
        assert not isinstance(exc.__cause__, ValueError), (
            "a closed-but-valid port must NOT travel the malformed-authority "
            f"path; got cause {exc.__cause__!r}"
        )
        assert isinstance(exc.__cause__, snap.urllib.error.URLError), (
            "it should come back through the ordinary connection-failure "
            f"clause; got cause {exc.__cause__!r}"
        )


class TestMalformedBracketedHostFailsLoudly:
    """A URL whose authority carries MALFORMED BRACKET SYNTAX must also fail
    through _fetch_url_bytes's own RuntimeError wrapper.

    Sibling of TestMalformedAuthorityFailsLoudly above, and deliberately a
    SEPARATE class because the raise happens at a different STATEMENT.  Those
    rows raise inside ``_urlopen`` (``_SafeRedirectHandler`` ->
    ``_effective_port``), which the function's ``try`` already covered.  These
    raise one statement EARLIER, in ``urllib.request.Request.__init__``
    itself: ``__init__`` -> ``request_host`` -> ``urlparse`` -> ``urlsplit``,
    which validates the netloc's brackets before any opener or handler
    exists.  That construction used to sit OUTSIDE the ``try``, so its
    ValueError could not reach ANY of the function's own except clauses --
    and the docstring's promise to fail loudly on "a malformed authority in
    the URL" was therefore not kept for this shape.  Measured at 218d1678
    through the shipped CLI: ``web:http://[::1/x`` printed a raw traceback
    and exited 1, where every other fetch failure exits EXIT_IO with the
    tool's clean message.

    The two malformed rows below are DIFFERENT code paths inside
    urllib.parse, not two instances of one, and no single URL can reach both:

      * ``http://[::1/x`` -- brackets UNBALANCED.  ``urlsplit`` raises
        ``ValueError("Invalid IPv6 URL")`` from its own ``'[' in netloc and
        ']' not in netloc`` guard (CPython 3.12.3 urllib/parse.py:514).
      * ``http://[1.2.3.4]/x`` -- brackets BALANCED, so the guard above does
        not fire and control falls into ``_check_bracketed_netloc`` ->
        ``_check_bracketed_host``, which raises ``ValueError("An IPv4 address
        cannot be in brackets")`` (parse.py:465).

    The second is reachable ONLY on the branch the first did not take, which
    is what makes it a second path rather than a second example of the first.

    Like the sibling class, these deliberately do NOT patch the ``_urlopen``
    seam the rest of this file patches -- the failure is upstream of it, so a
    stub would test nothing.
    """

    # Well-formed authority, nothing listening.  See the control below.
    CLOSED_PORT_URL = "http://127.0.0.1:9/"

    @staticmethod
    def _assert_loudly_wrapped(snap, url):
        """The shared assertion: a RuntimeError in the documented shape.

        Returned so each caller can additionally pin WHICH cause it travelled
        through -- which is what keeps the malformed rows and the control
        below distinguishable rather than merely all-red.
        """
        with pytest.raises(RuntimeError) as exc_info:
            snap._fetch_url_bytes(url, 2.0, 1000)
        message = str(exc_info.value)
        assert message.startswith(f"failed to fetch '{url}'"), (
            "the wrapped error must name the URL the operator asked for, in "
            f"the same shape as every other fetch failure; got: {message}"
        )
        return exc_info.value

    def test_unterminated_IPv6_literal_is_wrapped_not_a_bare_ValueError(
        self, tmp_path
    ):
        """Path 1: unbalanced brackets, refused by ``urlsplit`` itself."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        exc = self._assert_loudly_wrapped(snap, "http://[::1/x")
        assert isinstance(exc.__cause__, ValueError), (
            "the wrap must preserve the ValueError as __cause__, so the "
            "operator still gets the underlying reason and the traceback is "
            f"still diagnosable; got cause {exc.__cause__!r}"
        )
        assert "Invalid IPv6 URL" in str(exc), (
            "the reason should be urlsplit's own bracket-balance refusal, "
            f"not some other ValueError that happens to be loud; got: {exc}"
        )

    def test_bracketed_IPv4_literal_is_wrapped_not_a_bare_ValueError(
        self, tmp_path
    ):
        """Path 2: brackets BALANCED, refused by ``_check_bracketed_host``.

        Reached only because path 1's guard did not fire, which is why this
        is a second code path rather than a second instance of the first.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        exc = self._assert_loudly_wrapped(snap, "http://[1.2.3.4]/x")
        assert isinstance(exc.__cause__, ValueError), exc.__cause__
        assert "brackets" in str(exc), (
            "the reason should be the bracketed-host refusal, distinct from "
            f"path 1's message; got: {exc}"
        )

    def test_CONTROL_closed_but_valid_port_wraps_by_the_ORDINARY_path(
        self, tmp_path
    ):
        """THE CONTROL, and it is the reason the two rows above mean anything.

        Port 9 is well-formed, so ``Request.__init__`` constructs fine and the
        call reaches the network, coming back through the pre-existing
        URLError clause.  It must produce the SAME loud wrapper via a
        DIFFERENT cause -- and it must be GREEN on BOTH sides of the fix,
        which is what shows the rows above report the moved statement rather
        than a harness that never reaches _fetch_url_bytes at all.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        exc = self._assert_loudly_wrapped(snap, self.CLOSED_PORT_URL)
        assert not isinstance(exc.__cause__, ValueError), (
            "a closed-but-valid port must NOT travel the malformed-authority "
            f"path; got cause {exc.__cause__!r}"
        )
        assert isinstance(exc.__cause__, snap.urllib.error.URLError), (
            "it should come back through the ordinary connection-failure "
            f"clause; got cause {exc.__cause__!r}"
        )


# ============================================================================
# Tests: a SHORT or TRUNCATED body must fail loudly -- the two remaining
# unkept clauses of _fetch_url_bytes's "Never returns a partial read".
#
# Both were measured against the pre-fix code before either was written:
#
#   short Content-Length  (declares 1000, sends 10, closes)
#       -> returned 10 bytes, NO exception
#   truncated chunked     (declares a 1000-byte chunk, sends 10, closes)
#       -> http.client.IncompleteRead escaping as a bare traceback
#
# The first is silent, which is the worse of the two: the caller archives the
# fragment and the content hash then certifies the truncation as if it were
# the whole document. The second is loud but escapes through the WRONG type --
# IncompleteRead is an HTTPException, neither an OSError nor a ValueError, so
# it passes every existing except clause and every `except RuntimeError` the
# callers rely on.
#
# Every attack row below is paired with a control that must stay GREEN, and
# the controls are the load-bearing half. Three of them (honest chunked,
# chunked-with-a-stray-Content-Length, 204-with-a-stray-Content-Length) are
# legitimate response shapes that were measured succeeding BEFORE the fix,
# and they are what discriminates between the two candidate implementations
# of the short-read check:
#
#   comparing the Content-Length HEADER against len(body) -- reddens the
#       chunked-with-a-stray-Content-Length and 204-with-a-stray-
#       Content-Length rows, because the header is present in both while
#       http.client correctly ignores it (measured: exactly those two rows
#       fail, every other row stays green)
#   reading http.client's own HTTPResponse.length -- the bytes still owed,
#       which is None for chunked and 0 for a 204 -- passes every row
#
# The second is what ships: http.client has already decided whether the
# header is authoritative, and re-deciding it by hand is what creates the
# false rejections.
# ============================================================================

@contextlib.contextmanager
def _raw_response_server(payload: bytes):
    """Serve `payload` verbatim to each connection, then close it.

    A raw socket rather than http.server ON PURPOSE. These tests are about
    WIRE FRAMING -- a body shorter than the declared Content-Length, a chunked
    stream that stops mid-chunk -- and http.server emits neither: it computes
    its own framing from what the handler writes. A stubbed response object
    would be worse still, replaying only the mismatch the test itself
    fabricated while leaving the code that actually decides the outcome --
    http.client's deliberate non-enforcement of Content-Length, and its chunk
    decoder's IncompleteRead -- entirely unexercised.
    """
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    srv.settimeout(0.25)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:  # pragma: no cover - socket closed under us
                return
            with contextlib.closing(conn):
                try:
                    conn.recv(65536)
                    conn.sendall(payload)
                except OSError:  # pragma: no cover - client hung up first
                    pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/x"
    finally:
        stop.set()
        thread.join(timeout=5)
        srv.close()


class TestShortAndTruncatedBodiesFailLoudly:
    """The two halves of "Never returns a partial read", and the legitimate
    shapes that must survive them.

    These tests deliberately do NOT patch the _urlopen seam most of this file
    patches: the behaviour under test belongs to http.client's own framing,
    below _urlopen, so a stubbed opener would test only the fixture.
    """

    # declares 1000, sends 10, closes -- the silent short read.
    SHORT_CL = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n"
        + b"x" * 10
    )
    # declares 1000, sends 1000 -- the same declaration, honestly served.
    HONEST_CL = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n"
        + b"x" * 1000
    )
    # declares 11, sends 10 -- the MINIMUM short read: exactly one byte owed.
    SHORT_CL_BY_ONE = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\n"
        + b"x" * 10
    )
    # declares 11, sends 11 -- the same declaration, honestly served.
    HONEST_CL_BY_ONE = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\n"
        + b"x" * 11
    )
    # no Content-Length at all: framed by the close, which is legitimate.
    NO_CL = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + b"x" * 10
    # announces a 0x3e8 == 1000-byte chunk, sends 10 bytes, closes.
    TRUNCATED_CHUNKED = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
        b"Connection: close\r\n\r\n" + b"3e8\r\n" + b"x" * 10
    )
    # a properly terminated 10-byte chunked body.
    HONEST_CHUNKED = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
        b"Connection: close\r\n\r\n" + b"a\r\n" + b"x" * 10 + b"\r\n0\r\n\r\n"
    )
    # chunked AND a stray Content-Length: the chunk framing is authoritative
    # and the header must be ignored (RFC 9112 S6.3, and http.client's own
    # "we ignore this if tr_enc is chunked").
    CHUNKED_WITH_STRAY_CL = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
        b"Content-Length: 1000\r\nConnection: close\r\n\r\n"
        + b"a\r\n" + b"x" * 10 + b"\r\n0\r\n\r\n"
    )
    # 204 with a stray Content-Length: http.client forces the body length to
    # 0 for 204 regardless of the header, so there is no truncation here.
    NO_CONTENT_WITH_STRAY_CL = (
        b"HTTP/1.1 204 No Content\r\nContent-Length: 5\r\n"
        b"Connection: close\r\n\r\n"
    )

    # The read loop's own chunk size, mirrored here because it is a local
    # inside _fetch_url_bytes rather than anything importable.
    READ_CHUNK_SIZE = 65536
    # Strictly BETWEEN READ_CHUNK_SIZE and MAX_BYTES, and deliberately so:
    # above READ_CHUNK_SIZE so the body cannot arrive in a single read() --
    # that span IS the condition test_CONTROL_honest_body_SPANNING_
    # MULTIPLE_READS_succeeds exists to exercise -- and below MAX_BYTES (the
    # shared cap this row -- like every other row in this class except the
    # size-cap row below -- is fetched under via _fetch) so its own honest
    # body never trips that cap regardless of which implementation is
    # running.  It used to sit exactly ON MAX_BYTES (both constants were
    # 100000), which meant this row reddened for two unrelated reasons at
    # once and a failure told you nothing about which.  The declared length
    # and the delivered length are derived from the SAME constant, because
    # an honest control is worth nothing if the two can silently drift
    # apart.
    MULTI_READ_BODY_SIZE = 90_000
    HONEST_CL_MULTI_READ = (
        b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
        % MULTI_READ_BODY_SIZE
        + b"x" * MULTI_READ_BODY_SIZE
    )

    MAX_BYTES = 100_000

    # A future edit to either constant must not silently break the
    # multi-read row's own baseline -- fail loudly at collection time
    # instead of a confusing runtime failure deep in a test body.
    assert READ_CHUNK_SIZE < MULTI_READ_BODY_SIZE < MAX_BYTES, (
        "MULTI_READ_BODY_SIZE must stay strictly between READ_CHUNK_SIZE "
        "(so the multi-read row below still spans more than one read()) "
        "and MAX_BYTES (the shared cap that row is fetched under via "
        "_fetch -- a body at or above it would trip the cap even under "
        "the CORRECT implementation, breaking the row's own baseline)"
    )

    # Small and strictly BELOW READ_CHUNK_SIZE, on purpose: the property
    # this row pins -- that the size-cap comparison is a strict `>`, not
    # `>=` -- is SCALE-FREE.  It holds or fails identically at a cap of
    # 100000 or a cap of 1000, so pinning it at any particular size buys
    # nothing, and pinning it at self.MAX_BYTES (above READ_CHUNK_SIZE, as
    # an earlier version of this row did) would force this body to span
    # more than one read() too and re-entangle it with the
    # read-loop-position mutant the multi-read row above exists to isolate
    # -- the exact coupling this file exists to remove, just relocated onto
    # a different row.  Deliberately its OWN constant, not self.MAX_BYTES:
    # do not "simplify" it back.
    CAP_ROW_MAX_BYTES = 1000
    assert CAP_ROW_MAX_BYTES < READ_CHUNK_SIZE, (
        "CAP_ROW_MAX_BYTES must stay below READ_CHUNK_SIZE so its honest "
        "body arrives in a single read() -- otherwise the size-cap row "
        "re-entangles with the short-read-check-position mutant"
    )
    HONEST_CL_AT_CAP_ROW_MAX_BYTES = (
        b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
        % CAP_ROW_MAX_BYTES
        + b"x" * CAP_ROW_MAX_BYTES
    )

    def _fetch(self, snap, url):
        return snap._fetch_url_bytes(url, 5.0, self.MAX_BYTES)

    # -- attack rows ------------------------------------------------------

    def test_short_content_length_is_refused_LOUDLY(self, tmp_path):
        """Declares 1000, sends 10 -> RuntimeError, not 10 quiet bytes.

        Measured returning 10 bytes and no exception before the fix, because
        http.client's read(amt) declines to enforce Content-Length ("Ideally,
        we would raise IncompleteRead ... but it might break compatibility").
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with _raw_response_server(self.SHORT_CL) as url:
            with pytest.raises(RuntimeError) as exc_info:
                self._fetch(snap, url)
        message = str(exc_info.value)
        assert "short read" in message, message
        # The OWED count (1000 declared - 10 delivered), so a check that
        # fires with the wrong accounting -- reporting the declared 1000 or
        # the delivered 10 -- is not indistinguishable from one that fires
        # correctly.
        assert "990 bytes" in message, message
        assert url in message, message
        # __cause__ pinned per row: this one is raised DIRECTLY, so it must
        # NOT have travelled the IncompleteRead path below.
        assert exc_info.value.__cause__ is None, (
            "the short-read refusal is raised directly by the length check, "
            f"not wrapped from another exception; got {exc_info.value.__cause__!r}"
        )

    def test_short_by_a_SINGLE_byte_is_refused_LOUDLY(self, tmp_path):
        """Declares 11, sends 10 -> the MINIMUM short read, one byte owed.

        The 990-byte row above cannot see a THRESHOLD: it is satisfied by any
        `if owed and owed > k` for every k below 990, and `owed > 1` in
        particular passes the whole file.  (That is a different mutant from
        the `owed >= 0`/`>= 1` off-by-one on the boundary, which the honest
        controls already redden -- this one leaves every existing row green.)
        One byte is the smallest residue http.client can report, so this row
        kills every positive threshold at once and pins the check to ANYTHING
        still owed, which is what "Never returns a partial read" says.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with _raw_response_server(self.SHORT_CL_BY_ONE) as url:
            with pytest.raises(RuntimeError) as exc_info:
                self._fetch(snap, url)
        message = str(exc_info.value)
        # The whole clause, not "1 bytes" alone: a bare "1" is a substring of
        # the 1000-scale numbers the neighbouring rows carry.
        assert "short read: 1 bytes still owed" in message, message
        assert url in message, message
        # __cause__ pinned per row, as above: raised DIRECTLY by the length
        # check, so it must not have travelled the IncompleteRead path.
        assert exc_info.value.__cause__ is None, (
            "the short-read refusal is raised directly by the length check, "
            f"not wrapped from another exception; got {exc_info.value.__cause__!r}"
        )

    def test_truncated_chunked_stream_is_refused_LOUDLY(self, tmp_path):
        """A chunked stream that stops mid-chunk -> the same loud wrapper.

        Measured escaping as a raw http.client.IncompleteRead traceback before
        the fix: it is an HTTPException, so it is neither an OSError nor a
        ValueError and passed every existing clause -- and, one level up, the
        callers' own `except RuntimeError`.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with _raw_response_server(self.TRUNCATED_CHUNKED) as url:
            with pytest.raises(RuntimeError) as exc_info:
                self._fetch(snap, url)
        message = str(exc_info.value)
        assert message.startswith(f"failed to fetch '{url}'"), message
        assert "IncompleteRead" in message, message
        # __cause__ pinned per row: this one MUST carry the original, and it
        # is what distinguishes this row from the short-read row above.
        assert isinstance(exc_info.value.__cause__, http.client.IncompleteRead), (
            "the wrap must preserve the IncompleteRead as __cause__; got "
            f"{exc_info.value.__cause__!r}"
        )

    def test_short_content_length_writes_NOTHING_to_the_archive(self, tmp_path):
        """The harm the two rows above exist to prevent, end to end.

        A silently-short read is not merely an ugly return value: it reaches
        _archive_write, and the content hash then names the fragment as if it
        were the whole document.
        """
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(tmp_path / "engram", evidence_archive)
        with _raw_response_server(self.SHORT_CL) as url:
            with pytest.raises(SystemExit) as exc_info:
                snap._snapshot_web(url, {})
        assert exc_info.value.code == snap.EXIT_IO
        assert _archive_is_empty(evidence_archive), (
            "a short read must not leave a file whose hash certifies the "
            "truncation as the whole document"
        )

    # -- controls, all of which must stay GREEN ---------------------------

    def test_CONTROL_honest_content_length_of_the_same_size_succeeds(self, tmp_path):
        """THE control for the short-read row: the SAME declared 1000, served
        honestly. Without it, a red short-read row is equally consistent with
        a harness that never reaches the code, or with a check that refuses
        every Content-Length response."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with _raw_response_server(self.HONEST_CL) as url:
            assert self._fetch(snap, url) == b"x" * 1000

    def test_CONTROL_honest_content_length_of_ELEVEN_succeeds(self, tmp_path):
        """THE control for the one-byte-short row: the SAME declared 11,
        served honestly.  Without it, a red one-byte row is equally consistent
        with a check that refuses every small body, or with a harness that
        never reaches the code at this size."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with _raw_response_server(self.HONEST_CL_BY_ONE) as url:
            assert self._fetch(snap, url) == b"x" * 11

    def test_CONTROL_honest_chunked_response_succeeds(self, tmp_path):
        """The control for the chunked row: a properly terminated chunked
        body must still come back clean."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with _raw_response_server(self.HONEST_CHUNKED) as url:
            assert self._fetch(snap, url) == b"x" * 10

    def test_CONTROL_no_content_length_header_at_all_succeeds(self, tmp_path):
        """The control that pins the check to a length being OWED.

        A close-framed HTTP/1.0-style response legitimately declares no
        length, and resp.length is None there. Reading that None as a
        failure -- rather than as "nothing owed" -- would refuse the common
        case, and this is the likeliest way the new check could break
        something real.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with _raw_response_server(self.NO_CL) as url:
            assert self._fetch(snap, url) == b"x" * 10

    def test_CONTROL_chunked_beside_a_stray_Content_Length_succeeds(self, tmp_path):
        """THE row that discriminates between the two implementations.

        A server must not send both, but proxies do, and chunk framing is
        authoritative: Content-Length is ignored (RFC 9112 S6.3; http.client
        sets self.length = None). This response is honest -- 10 bytes,
        properly terminated -- and succeeded before the fix. A check that
        compared the Content-Length HEADER against the body length would
        refuse it; reading resp.length does not, because http.client already
        made that call.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with _raw_response_server(self.CHUNKED_WITH_STRAY_CL) as url:
            assert self._fetch(snap, url) == b"x" * 10

    def test_CONTROL_204_beside_a_stray_Content_Length_succeeds(self, tmp_path):
        """The second row that discriminates between the two implementations.

        http.client forces the body length to 0 for 204/304/1xx and for HEAD
        regardless of any header, so a 204 carrying a stray Content-Length
        yields zero bytes with nothing owed -- and succeeded before the fix.
        A header comparison would refuse it; resp.length is 0, so the check
        does not fire.

        Of those statuses only 204 is reachable here: 304 and 1xx are already
        refused by the 2xx status gate above the read loop, and this tool
        never issues a HEAD. A 304 carrying the entity's Content-Length with
        no body -- legitimate under RFC 9110 S15.4.5 -- therefore cannot
        reach either implementation of this check.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with _raw_response_server(self.NO_CONTENT_WITH_STRAY_CL) as url:
            assert self._fetch(snap, url) == b""

    def test_CONTROL_honest_body_SPANNING_MULTIPLE_READS_succeeds(self, tmp_path):
        """THE control for the check's POSITION: AFTER the read loop, never
        inside it.

        Every other row in this file with a body that fits in a SINGLE
        read(65536) -- the largest is 1000 bytes -- cannot see WHERE the
        `owed` test sits.  This row's body (MULTI_READ_BODY_SIZE,
        deliberately kept strictly between READ_CHUNK_SIZE and MAX_BYTES --
        see the constant's own comment) is the one built to span more than
        one read(), so it is the only row that can pin the check's
        POSITION.  The size-cap comparison is a SEPARATE, scale-free
        property, pinned independently and at a small size by
        test_CONTROL_body_of_exactly_the_cap_succeeds below -- the two
        properties no longer share a row, or a mutant.

        The position is load-bearing because `resp.length` is BYTES STILL
        OWED, not a verdict: part-way through an honest body it is
        legitimately POSITIVE, and it reaches 0 only once the last chunk has
        arrived.  Testing it per-chunk instead of once at the end therefore
        refuses every honest response larger than 64 KiB -- and does so while
        leaving the rest of this file green, because nothing else with a
        body under that size can reach a second read() at all.

        Measured with the check moved inside the loop: this response -- 90000
        declared, all 90000 delivered -- is refused with "short read: 24464
        bytes still owed", i.e. 90000 - 65536, exactly one read in.

        That mutant is wrong a second way too, on bodies bigger than one
        chunk: 90000 declared with 89990 delivered -- 10 genuinely owed --
        also reports 24464, the residue as of the first read rather than the
        true count.  It reports the RIGHT number on every short body this
        file already carries, because each of those finishes inside a single
        read (the 1000-declared/10-delivered row still says 990) -- which is
        exactly why none of them moves, and why this row has to exist.

        This row's body sits strictly below MAX_BYTES, the shared cap it is
        fetched under, so its own honest delivery never trips that cap --
        measured, `>` -> `>=` leaves this row green, exactly as it should:
        this row has nothing to say about the cap comparison either way.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with _raw_response_server(self.HONEST_CL_MULTI_READ) as url:
            body = self._fetch(snap, url)
        # Length first: a short or refused read is the failure this row is
        # built to catch, and asserting the 90000-byte payload directly
        # would bury it in an unreadable diff.
        assert len(body) == self.MULTI_READ_BODY_SIZE, (
            "the whole declared body must come back across every read(); got "
            f"{len(body)} bytes"
        )
        assert body == b"x" * self.MULTI_READ_BODY_SIZE

    def test_CONTROL_body_of_exactly_the_cap_succeeds(self, tmp_path):
        """THE row that pins the size-cap comparison as a strict `>` --
        isolated from the read-loop-position mutant, not merely from a
        coincidence of shared constants.

        Split out of the multi-read row above, which used to carry this
        property purely by coincidence: both constants happened to be
        100000, so that one row reddened under EITHER of two independent
        mutants (the short-read check's position, and this cap comparison)
        and a failure there told you nothing about which caused it. A first
        attempt at splitting this row still used self.MAX_BYTES (100000) as
        the body size, which genuinely stopped the CAP mutant from also
        reddening the multi-read row -- but MAX_BYTES is itself bigger than
        READ_CHUNK_SIZE, so that body ALSO spanned more than one read(), and
        the coupling simply relocated onto this row instead of disappearing.

        The property this row pins -- "is the comparison a strict `>` or a
        `>=`?" -- is SCALE-FREE: it holds or fails identically at any cap
        size, so nothing about it requires the large value. This row uses
        its own small cap (CAP_ROW_MAX_BYTES, deliberately kept below
        READ_CHUNK_SIZE -- see the constant's own comment) via a direct
        `_fetch_url_bytes(url, timeout, CAP_ROW_MAX_BYTES)` call rather than
        the class's `_fetch` helper, which is pinned to self.MAX_BYTES. A
        body of exactly CAP_ROW_MAX_BYTES, delivered honestly, must succeed
        under `len(buf) > max_bytes` (1000 > 1000 is False) and must be
        refused under a `>=` mutant (1000 >= 1000 is True) -- measured, the
        mutant raises "response exceeded the 1000-byte size cap" on this
        row ALONE: the multi-read row above stays green (90000 never
        approaches 1000 either way), and this row in turn stays green under
        the short-read-check-position mutant, because its whole body
        arrives in a single read() and `owed` is never positive mid-loop to
        catch. Each mutant now reddens exactly one row.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with _raw_response_server(self.HONEST_CL_AT_CAP_ROW_MAX_BYTES) as url:
            body = snap._fetch_url_bytes(url, 5.0, self.CAP_ROW_MAX_BYTES)
        assert len(body) == self.CAP_ROW_MAX_BYTES, (
            "a body of exactly the cap must be delivered in full, not "
            f"refused by an off-by-one cap check; got {len(body)} bytes"
        )
        assert body == b"x" * self.CAP_ROW_MAX_BYTES


class TestHTTPExceptionClauseIsBROAD:
    """`except http.client.HTTPException` catches the SIBLINGS too, not only
    IncompleteRead -- and the breadth is the claim nothing else here tests.

    The truncated-chunked row above is satisfied by a narrow
    `except http.client.IncompleteRead`: that mutant keeps the import and the
    handler, does the pre-fix thing for every other sibling, and passes the
    whole file.  So the reasoning the breadth rests on -- every sibling is a
    remote- or transport-triggered failure of the same fetch, and urllib's
    do_open wraps only the OSError from h.request(...) while h.getresponse()
    is passed through untouched -- had no row standing behind it.

    BadStatusLine is the sibling that demonstrates it: raised out of
    _read_status inside getresponse(), it is neither an OSError (so no
    URLError clause sees it) nor a ValueError, and under the narrow mutant it
    escapes _fetch_url_bytes entirely -- reaching the callers' own
    `except RuntimeError` as a bare traceback, out of the one function whose
    documented contract is to fail loudly THROUGH the wrapper.
    """

    # Not an HTTP status line at all: http.client's _read_status splits it,
    # finds a version that does not start with "HTTP/", and raises
    # BadStatusLine -- an HTTPException that is NOT an IncompleteRead.
    GARBAGE_STATUS_LINE = b"NOT-HTTP GARBAGE\r\n\r\n"
    # The same server, the same shape, a well-formed status line.
    WELL_FORMED = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\n"
        + b"x" * 10
    )

    MAX_BYTES = 100_000

    def _fetch(self, snap, url):
        return snap._fetch_url_bytes(url, 5.0, self.MAX_BYTES)

    def test_a_NON_IncompleteRead_HTTPException_is_wrapped_LOUDLY(self, tmp_path):
        """A garbage status line -> the loud RuntimeError, carrying a
        BadStatusLine as __cause__.

        The __cause__ assertions are what the narrow clause cannot satisfy:
        the row demands an HTTPException that is explicitly NOT an
        IncompleteRead, so widening the mutant back to IncompleteRead-only
        does not make it pass -- it makes it error with a bare BadStatusLine.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with _raw_response_server(self.GARBAGE_STATUS_LINE) as url:
            with pytest.raises(RuntimeError) as exc_info:
                self._fetch(snap, url)
        cause = exc_info.value.__cause__
        assert isinstance(cause, http.client.HTTPException), (
            "the wrap must preserve the protocol-layer failure as __cause__; "
            f"got {cause!r}"
        )
        assert not isinstance(cause, http.client.IncompleteRead), (
            "this row exists to exercise a sibling OTHER than IncompleteRead, "
            f"so a narrow except-clause cannot satisfy it; got {cause!r}"
        )
        # The TYPE NAME in the message is deliberate and unlike the sibling
        # clauses (see the comment on the clause): _snapshot_web/_snapshot_pdf
        # print str(exc) and exit, so the traceback `from exc` preserves never
        # reaches the operator -- and BadStatusLine's own str is a bare
        # protocol fragment that names nothing.  Dropping the prefix also
        # passes the rest of the file, so pin it here.
        assert str(exc_info.value).startswith(
            f"failed to fetch '{url}': BadStatusLine: "
        ), str(exc_info.value)

    def test_CONTROL_a_well_formed_status_line_over_the_same_server_succeeds(
        self, tmp_path
    ):
        """THE control: the same raw server, the same connection shape, a
        parseable status line.  Without it, a red row above is equally
        consistent with a fixture that never completes a request at all."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with _raw_response_server(self.WELL_FORMED) as url:
            assert self._fetch(snap, url) == b"x" * 10


# ============================================================================
# Tests: redirect safety (#2639) -- the http/https invariant and the
# internal-host guard, enforced across redirects.
#
# The tool validated web:/pdf: input as http(s) at parse time and then fetched
# with urllib's DEFAULT opener, whose HTTPRedirectHandler allows redirect
# targets with scheme in ('http', 'https', 'ftp', '') and to ANY host. So a
# validated https:// input could be steered by a remote 302 to ftp://, or to an
# internal address, with the bytes landing in the evidence store as a citable
# source.
#
# Most of the coverage below runs against the two pure predicates, so it needs
# no server. The integration tests that follow use a real loopback http.server.
# ============================================================================

class TestRedirectSchemePredicate:
    """_redirect_scheme_ok: only http/https may be followed."""

    @pytest.mark.parametrize("url", [
        "http://example.com/x",
        "https://example.com/x",
        "HTTP://example.com/x",          # scheme comparison is case-insensitive
        "HTTPS://EXAMPLE.COM/x",
    ])
    def test_allowed_schemes(self, tmp_path, url):
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        assert snap._redirect_scheme_ok(url) is True

    @pytest.mark.parametrize("url", [
        "ftp://example.com/x",           # THE gap: urllib's own allowlist has ftp
        "ftps://example.com/x",
        "file:///etc/passwd",
        "gopher://example.com/x",
        "data:text/plain;base64,aGk=",
        "javascript:alert(1)",
    ])
    def test_rejected_schemes(self, tmp_path, url):
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        assert snap._redirect_scheme_ok(url) is False


class TestRedirectHostPredicate:
    """_redirect_host_block_reason: redirect targets may not resolve internal.

    Every address below is an IP literal, so getaddrinfo parses it without
    touching DNS -- these tests are hermetic.
    """

    @pytest.mark.parametrize("url,label", [
        ("http://127.0.0.1:5001/health", "loopback"),
        ("http://127.0.0.53/x", "loopback"),
        ("http://[::1]:8080/x", "loopback"),
        ("http://10.0.0.5/x", "private"),
        ("http://192.168.1.1/x", "private"),
        ("http://172.16.0.1/x", "private"),
        ("http://169.254.169.254/latest/meta-data/", "link-local"),
        ("http://0.0.0.0/x", "unspecified"),
        # IPv4-mapped IPv6 reports is_loopback False; the unwrap is what
        # catches it, and it must be NAMED as loopback rather than private.
        ("http://[::ffff:127.0.0.1]/x", "loopback"),
        # RFC 6598 shared address space -- the ONE range the previous
        # loopback/link-local/unspecified/private quartet missed, because
        # CPython deliberately reports is_private False for it. It is also
        # Tailscale's range, and we are on a tailnet, so this is reachable
        # rather than theoretical. Boundaries included: a mutation that
        # mis-sizes the prefix (/12, /8) is caught by one end or the other.
        ("http://100.64.0.1/x", "CGNAT"),
        ("http://100.64.0.0/x", "CGNAT"),
        ("http://100.127.255.254/x", "CGNAT"),
        ("http://100.127.255.255/x", "CGNAT"),
    ])
    @pytest.mark.real_dns  # IP literals: parsed numerically, no DNS traffic
    def test_internal_targets_blocked(self, tmp_path, url, label):
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        reason = snap._redirect_host_block_reason(url)
        assert reason is not None, f"{url} should be blocked as {label}"
        assert label in reason, f"reason should name {label!r}, got: {reason}"

    @pytest.mark.parametrize("url", [
        "https://93.184.216.34/x",
        "https://8.8.8.8/x",
        "https://[2606:2800:220:1:248:1893:25c8:1946]/x",
        # The addresses immediately OUTSIDE 100.64.0.0/10, either side. These
        # are the other half of the boundary check above: without them, a
        # mutation widening the CGNAT prefix blocks real public traffic and
        # nothing goes red.
        "https://100.63.255.255/x",
        "https://100.128.0.0/x",
    ])
    @pytest.mark.real_dns  # IP literals: parsed numerically, no DNS traffic
    def test_public_targets_allowed(self, tmp_path, url):
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        assert snap._redirect_host_block_reason(url) is None

    def test_hostname_resolving_to_loopback_is_blocked(self, tmp_path, monkeypatch):
        """The check RESOLVES first -- a name-based blocklist is trivially
        bypassed by a hostname that resolves to 127.0.0.1."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")

        def _fake_getaddrinfo(host, port, *args, **kwargs):
            assert host == "totally-innocent.example.com"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

        monkeypatch.setattr(snap.socket, "getaddrinfo", _fake_getaddrinfo)

        reason = snap._redirect_host_block_reason(
            "https://totally-innocent.example.com/x"
        )
        assert reason is not None
        assert "loopback" in reason
        assert "127.0.0.1" in reason

    def test_blocked_when_any_resolved_address_is_internal(self, tmp_path, monkeypatch):
        """A host resolving to one public AND one internal address is blocked;
        checking only the first answer would let this through."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")

        def _fake_getaddrinfo(host, port, *args, **kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0)),
            ]

        monkeypatch.setattr(snap.socket, "getaddrinfo", _fake_getaddrinfo)

        reason = snap._redirect_host_block_reason("https://mixed.example.com/x")
        assert reason is not None
        assert "10.1.2.3" in reason

    def test_unresolvable_host_fails_closed(self, tmp_path, monkeypatch):
        """A host we cannot resolve is a host we cannot certify as external."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")

        def _fake_getaddrinfo(host, port, *args, **kwargs):
            raise socket.gaierror(-2, "Name or service not known")

        monkeypatch.setattr(snap.socket, "getaddrinfo", _fake_getaddrinfo)

        reason = snap._redirect_host_block_reason("https://nope.invalid/x")
        assert reason is not None
        assert "resolve" in reason

    def test_target_without_host_is_blocked(self, tmp_path):
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        assert snap._redirect_host_block_reason("http:///no-host") is not None

    @pytest.mark.real_dns  # a malformed label must reach the REAL getaddrinfo
    @pytest.mark.parametrize("host", [
        "a" * 300 + ".com",   # label too long
        "a" * 64 + ".com",    # label one over the 63-octet DNS limit
        "..com",              # empty label
    ])
    def test_malformed_hostname_fails_closed_without_raising(
        self, tmp_path, host
    ):
        """getaddrinfo raises UnicodeError -- NOT an OSError, so it escapes
        both `except socket.gaierror` and any wider `except OSError` -- for a
        malformed DNS label. Uncaught it surfaces as an unhandled traceback
        instead of the tool's clean EXIT_IO path.

        The assertion is deliberately two-sided: the call must RETURN a reason
        (fail closed) rather than raise. A test that only caught the exception
        type would pass against code that still lets it escape.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")

        # Precondition: this really is the UnicodeError path, not a gaierror
        # one wearing a long name. Without this the test could pass for the
        # wrong reason on a platform whose resolver reports it differently.
        with pytest.raises(UnicodeError):
            socket.getaddrinfo(host, None)

        reason = snap._redirect_host_block_reason(f"http://{host}/x")
        assert reason is not None, "a host we cannot resolve must fail CLOSED"
        assert "resolve" in reason
        # Name the cause by FAMILY, not by concrete class. The reason string
        # carries `type(exc).__name__`, and WHICH UnicodeError subclass
        # getaddrinfo raises varies by interpreter: 3.12 raises UnicodeError
        # itself, 3.14 raises UnicodeEncodeError -- and "UnicodeError" is not
        # a substring of "UnicodeEncodeError". Pinning the exact class name
        # reddens this test on an interpreter bump for a purely cosmetic
        # reason, while the behaviour under test is unchanged (the production
        # catch is on the UnicodeError BASE class, which both satisfy).
        # "Unicode" still separates this from a gaierror reason, which is the
        # distinction an operator reading the message actually needs.
        assert "Unicode" in reason, (
            f"the message should name the cause, not just fail; got: {reason}"
        )

    @pytest.mark.real_dns  # IP literals: parsed numerically, no DNS traffic
    @pytest.mark.parametrize("url,why", [
        ("http://[fec0::1]/x", "deprecated IPv6 site-local: not is_private"),
        ("http://[::7f00:1]/x", "IPv4-COMPATIBLE IPv6; ipv4_mapped is None"),
        ("http://[64:ff9b::a00:1]/x", "NAT64 wrapping 10.0.0.1"),
        ("http://224.0.0.1/x", "IPv4 multicast"),
        ("http://[ff02::1]/x", "IPv6 multicast"),
    ])
    def test_documented_residuals_are_still_allowed(self, tmp_path, url, why):
        """CHARACTERIZATION, not endorsement.

        These are the ranges the switch to `is_global` was measured NOT to
        close (they are recorded in _internal_address_label's docstring). They
        are asserted here so the residual is a checked fact rather than a
        claim in prose: if a later change closes one, this test goes red and
        whoever closed it updates the docstring in the same commit.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        assert snap._redirect_host_block_reason(url) is None, (
            f"{url} was documented as an OPEN residual ({why}); it is now "
            f"blocked -- update _internal_address_label's docstring"
        )


class TestInternalAddressGate:
    """_internal_address_label: `is_global` decides, the labels only name.

    The bug this replaced enumerated four attributes
    (loopback/link-local/unspecified/private) and therefore inherited every
    hole in that enumeration -- notably 100.64.0.0/10, which CPython
    deliberately reports is_private False for, and which is Tailscale's range.
    """

    @pytest.mark.parametrize("addr", [
        # blocked, each by a different label
        "127.0.0.1", "::1", "169.254.169.254", "fe80::1", "0.0.0.0", "::",
        "10.0.0.1", "192.168.1.1", "172.16.0.1", "fd00::1",
        "255.255.255.255", "192.0.2.1", "198.18.0.1", "240.0.0.1",
        "100.64.0.1", "100.127.255.255",
        # allowed
        "8.8.8.8", "93.184.216.34", "100.63.255.255", "100.128.0.0",
        "2606:2800:220:1:248:1893:25c8:1946",
        "fec0::1", "::7f00:1", "64:ff9b::a00:1", "224.0.0.1", "ff02::1",
    ])
    def test_label_is_exactly_the_complement_of_is_global(self, tmp_path, addr):
        """THE GATE, asserted as an equivalence rather than by example.

        `label is None` must hold exactly when `is_global` is True. This is
        what makes "is_global is the gate" checkable: any label branch that
        blocked something is_global, or let through something that is not,
        breaks the equivalence on some row here.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        a = ipaddress.ip_address(addr)
        label = snap._internal_address_label(a)
        assert (label is None) == a.is_global, (
            f"{addr}: label={label!r} but is_global={a.is_global}"
        )

    def test_cgnat_is_the_range_the_old_quartet_missed(self, tmp_path):
        """The regression, named. Asserted against the OLD predicate spelled
        out inline, so this fails if someone reverts the gate to the quartet
        -- rather than merely asserting the new answer, which a revert to a
        differently-wrong classification could still satisfy.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        a = ipaddress.ip_address("100.64.0.1")

        old_quartet_would_block = (
            a.is_loopback or a.is_link_local or a.is_unspecified or a.is_private
        )
        assert old_quartet_would_block is False, (
            "precondition: this test is only meaningful because the old "
            "four-attribute test allowed CGNAT"
        )
        assert snap._internal_address_label(a) == "shared-address-space (CGNAT)"

    @pytest.mark.parametrize("addr,label", [
        ("127.0.0.1", "loopback"),
        ("169.254.169.254", "link-local"),
        ("0.0.0.0", "unspecified"),
        ("10.0.0.1", "private"),
        ("100.64.0.1", "shared-address-space (CGNAT)"),
    ])
    def test_specific_labels_survive_the_switch(self, tmp_path, addr, label):
        """The labels are for humans; a blocked fetch must still say WHY, not
        collapse every refusal into a generic 'non-global'."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        assert snap._internal_address_label(ipaddress.ip_address(addr)) == label

    def test_public_addresses_get_no_label(self, tmp_path):
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        for addr in ("8.8.8.8", "93.184.216.34"):
            assert snap._internal_address_label(ipaddress.ip_address(addr)) is None


class _FakeRedirectFP:
    """Stand-in for the 30x response body that redirect_request closes.

    redirect_request only ever calls .close() on it, and only on the blocked
    path -- so this is the whole interface, and a real socket is not needed to
    ask the handler for a FOLLOWED/BLOCKED verdict.
    """

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class TestRedirectIntentInheritance:
    """An internal redirect target is followed IFF the ORIGINAL input url's
    host already resolved to that same (address, effective_port) authority
    AND that host resolved to no globally reachable address at all -- with
    one named exception, an http->https upgrade on the same address (port
    80 -> 443).

    The threat model is a remote server steering us somewhere we did not
    choose. A redirect back to the host (and port) the operator typed is not
    steering; a redirect to a DIFFERENT internal host, or to a DIFFERENT PORT
    on the same host, still is -- and a host whose DNS the ATTACKER controls
    was never the operator's choice at all, however many of its own addresses
    it answers with. The authority is also sampled ONCE, at handler
    construction, before urllib ever opens the original connection -- not
    lazily after a 30x has already arrived, which an attacker could otherwise
    time to flip DNS between the connection and the check.

    The predicate cases pass the inherited set (and, where the port dimension
    is under test, the inherited port) explicitly, so they are hermetic and
    test the address/port-matching RULE rather than any particular resolver
    result. The handler cases after them stub the resolver instead, because
    the no-global condition and the eager-sampling timing both live in
    _SafeRedirectHandler._resolve_inherited -- the predicate honours whatever
    set and port it is handed.
    """

    LOOPBACK = frozenset({"127.0.0.1"})
    PUBLIC = frozenset({"93.184.216.34"})
    # The normalised host that corresponds to each address set above, for
    # the InheritedAuthority.host field every non-empty-addrs authority must
    # also carry (#2691's host-authority guard). Where a test's target URL
    # literally uses the address as its hostname (the common case in this
    # class), the two are the same string.
    LOOPBACK_HOST = "127.0.0.1"
    PUBLIC_HOST = "93.184.216.34"

    @pytest.mark.real_dns  # IP literals: parsed numerically, no DNS traffic
    def test_internal_target_in_the_original_set_is_allowed(self, tmp_path):
        """The workflow the input exemption exists to preserve: an
        operator-chosen LAN host redirecting to ITSELF (trailing-slash
        normalisation, slash merging, HTTP->HTTPS upgrade, /login bounce).

        The inherited port (5002) matches the target's own port -- the
        same-authority case."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        assert snap._redirect_host_block_reason(
            "http://127.0.0.1:5002/threads/",
            snap.InheritedAuthority(
                host=self.LOOPBACK_HOST, addrs=self.LOOPBACK, port=5002
            ),
        ) is None

    @pytest.mark.real_dns  # IP literals: parsed numerically, no DNS traffic
    def test_internal_target_same_address_different_port_is_refused(
        self, tmp_path
    ):
        """The sibling of the case above, and what makes the port check
        DISCRIMINATING at the PREDICATE layer rather than only at the
        handler layer above it: the same address the original authority
        chose (127.0.0.1), but reached on a port (5001) that authority did
        not also choose (5002) -- still steering."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        reason = snap._redirect_host_block_reason(
            "http://127.0.0.1:5002/threads/",
            snap.InheritedAuthority(
                host=self.LOOPBACK_HOST, addrs=self.LOOPBACK, port=5001
            ),
        )
        assert reason is not None
        assert "5002" in reason
        assert "5001" in reason

    @pytest.mark.real_dns  # IP literals: parsed numerically, no DNS traffic
    def test_internal_target_NOT_in_the_original_set_is_refused(self, tmp_path):
        """THE test that proves this is not a blanket loosening. The original
        was internal, so a naive 'operator opted into internal' rule would
        exempt the whole chain; a DIFFERENT internal host is still steering.

        The inherited port (5002) and host (self.LOOPBACK_HOST) are both
        irrelevant to this outcome -- the address itself never matches
        self.LOOPBACK -- but InheritedAuthority still requires both."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        reason = snap._redirect_host_block_reason(
            "http://10.1.2.3/secret",
            snap.InheritedAuthority(
                host=self.LOOPBACK_HOST, addrs=self.LOOPBACK, port=5002
            ),
        )
        assert reason is not None
        assert "10.1.2.3" in reason
        assert "private" in reason

    @pytest.mark.real_dns  # IP literals: parsed numerically, no DNS traffic
    def test_public_original_inherits_nothing_internal(self, tmp_path):
        """The core SSRF case, stated as the inheritance rule sees it: a
        legitimate external URL that 302s inward inherits no internal address,
        so the target is refused.

        The inherited port (443) and host (self.PUBLIC_HOST) are both
        irrelevant to this outcome -- the address itself never matches
        self.PUBLIC -- but InheritedAuthority still requires both."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        reason = snap._redirect_host_block_reason(
            "http://127.0.0.1:5001/health",
            snap.InheritedAuthority(
                host=self.PUBLIC_HOST, addrs=self.PUBLIC, port=443
            ),
        )
        assert reason is not None
        assert "loopback" in reason

    @pytest.mark.real_dns  # IP literals: parsed numerically, no DNS traffic
    def test_cgnat_target_is_not_inherited_from_a_loopback_original(self, tmp_path):
        """A tailnet peer is a different internal host, not the one typed.

        The inherited port (5002) and host (self.LOOPBACK_HOST) are both
        irrelevant to this outcome -- the address itself never matches
        self.LOOPBACK -- but InheritedAuthority still requires both."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        reason = snap._redirect_host_block_reason(
            "http://100.64.0.1/x",
            snap.InheritedAuthority(
                host=self.LOOPBACK_HOST, addrs=self.LOOPBACK, port=5002
            ),
        )
        assert reason is not None
        assert "CGNAT" in reason

    @pytest.mark.real_dns  # IP literals: parsed numerically, no DNS traffic
    def test_inheritance_cannot_allow_a_target_the_gate_would_pass_anyway(
        self, tmp_path
    ):
        """A public target is unaffected by inheritance in either direction --
        the parameter only ever RESCUES an internal address, never blocks.

        A single port (443) and a single host (self.LOOPBACK_HOST) are used
        for all three ``addrs`` values, including the empty-set case -- an
        empty ``addrs`` alongside a real host/port is a fully legitimate
        InheritedAuthority (see InheritedAuthority's own docstring); it just
        grants nothing."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        for addrs in (frozenset(), self.LOOPBACK, self.PUBLIC):
            assert snap._redirect_host_block_reason(
                "https://8.8.8.8/x",
                snap.InheritedAuthority(
                    host=self.LOOPBACK_HOST, addrs=addrs, port=443
                ),
            ) is None

    @pytest.mark.real_dns  # IP literals: parsed numerically, no DNS traffic
    def test_default_is_strict_no_inheritance(self, tmp_path):
        """Omitting the parameter must give the pre-inheritance behaviour, so
        a caller cannot get the exemption by forgetting to ask."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        assert snap._redirect_host_block_reason("http://127.0.0.1/x") is not None

    # ------------------------------------------------------------------
    # #2691 security re-read, BLOCKER 1: the partial authority must be
    # UNCONSTRUCTIBLE, not merely annotated as impossible.
    #
    # test_addrs_without_port_raises, test_addrs_without_host_raises, and
    # test_addrs_without_port_raises_even_with_host_given used to live here.
    # They asserted that _redirect_host_block_reason raised ValueError when
    # handed address authority without a matching port and/or host -- i.e.
    # that a PARTIAL authority was a loud caller error.  They were deleted
    # on the theory that InheritedAuthority, "a NamedTuple with all three
    # fields required and no defaults", made the partial state
    # unconstructible and left them nothing to exercise.
    #
    # THAT WAS FALSE, and the tests below are its replacement rather than
    # its restoration.  A NamedTuple's field annotations are not runtime
    # guards, so InheritedAuthority(host='chosen.lan',
    # addrs=frozenset({'127.0.0.1'}), port=None) constructed cleanly -- and
    # that explicit None then authorised EVERY port, because
    # _port_inherited read it as "no port stated" and returned True.  Two
    # loud call-time guards had been traded for a silent fail-open.
    #
    # The type now enforces what it claims: a frozen dataclass whose
    # __post_init__ rejects every partial or wrongly-typed dimension at
    # CONSTRUCTION -- so the refusal is earlier than the old guards, not
    # weaker.  These tests check the enforcement itself rather than a
    # downstream symptom of it, and the last one is the control: a
    # legitimate full authority must still build, or a guard that refused
    # everything would read as a pass.
    # ------------------------------------------------------------------

    def test_authority_with_no_port_is_UNCONSTRUCTIBLE(self, tmp_path):
        """THE blocker-1 case, in the exact shape that was reproduced:
        an explicit port=None alongside real host and addrs."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with pytest.raises(ValueError) as excinfo:
            snap.InheritedAuthority(
                host="chosen.lan", addrs=self.LOOPBACK, port=None
            )
        assert "port" in str(excinfo.value)

    def test_authority_with_no_host_is_UNCONSTRUCTIBLE(self, tmp_path):
        """The host dimension gets the same enforcement as the port one --
        an authority with no host grants nothing and must be spelled as
        the whole-authority None, not as a hole inside one."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        for bad_host in (None, ""):
            with pytest.raises(ValueError) as excinfo:
                snap.InheritedAuthority(
                    host=bad_host, addrs=self.LOOPBACK, port=5002
                )
            assert "host" in str(excinfo.value)

    def test_authority_with_no_addrs_is_UNCONSTRUCTIBLE(self, tmp_path):
        """addrs=None is a hole; addrs=frozenset() is a legitimate value
        meaning "nothing is inherited" (covered by
        test_inheritance_cannot_allow_a_target_the_gate_would_pass_anyway).
        The distinction is the point -- an empty set is a stated answer,
        a missing one is not."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        with pytest.raises(ValueError) as excinfo:
            snap.InheritedAuthority(host="chosen.lan", addrs=None, port=5002)
        assert "addrs" in str(excinfo.value)

    def test_authority_rejects_wrongly_typed_dimensions(self, tmp_path):
        """Present-in-name-only is the same defect as absent, and quieter.

        A str port compares unequal to every int _effective_port produces,
        a mutable set breaks the frozen dataclass's hashability, and
        port=True would read as port 1 through int's own subclassing --
        each fails closed but SILENTLY, and silence is what #2691 keeps
        re-finding in this function.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        cases = [
            dict(host="chosen.lan", addrs=self.LOOPBACK, port="5002"),
            dict(host="chosen.lan", addrs=self.LOOPBACK, port=True),
            dict(host="chosen.lan", addrs=self.LOOPBACK, port=0),
            dict(host="chosen.lan", addrs=self.LOOPBACK, port=65536),
            dict(host="chosen.lan", addrs={"127.0.0.1"}, port=5002),
            dict(host="chosen.lan", addrs=frozenset({1}), port=5002),
            dict(host=b"chosen.lan", addrs=self.LOOPBACK, port=5002),
        ]
        for kwargs in cases:
            with pytest.raises(ValueError):
                snap.InheritedAuthority(**kwargs)

    def test_a_FULL_authority_still_constructs(self, tmp_path):
        """THE CONTROL for the four tests above. Without it, a
        __post_init__ that raised unconditionally would satisfy every one
        of them -- a uniform refusal reading as a closed blocker.

        Also pins the empty-addrs case as legitimate, so the guard is
        known to reject a MISSING set rather than a merely empty one.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        full = snap.InheritedAuthority(
            host="chosen.lan", addrs=self.LOOPBACK, port=5002
        )
        assert full.host == "chosen.lan"
        assert full.addrs == self.LOOPBACK
        assert full.port == 5002

        empty = snap.InheritedAuthority(
            host="chosen.lan", addrs=frozenset(), port=443
        )
        assert empty.addrs == frozenset()

        # frozen: the authority is sampled once and cannot be edited after.
        with pytest.raises(Exception):
            full.port = 5001

    def test_port_predicate_has_no_unstated_port_branch(self, tmp_path):
        """The downstream half of blocker 1, checked directly.

        _port_inherited used to accept Optional[int] and return True for
        None -- "no port was stated" read as permissive -- which is what
        turned the unenforced port=None above into authority over EVERY
        port. The branch is removed, not merely made unreachable: a
        fail-open default that can no longer be constructed is dead code
        that still reads as permission to the next person.

        So the isolated misuse now fails CLOSED, and the two real rules
        (exact match; the one-way 80->443 upgrade bounce) are unchanged --
        the second and third assertions are what stop this from being
        satisfied by a predicate that returns False for everything.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        assert snap._port_inherited(None, 443) is False
        assert snap._port_inherited(5002, 5002) is True
        assert snap._port_inherited(80, 443) is True
        assert snap._port_inherited(443, 80) is False
        assert snap._port_inherited(5002, 5001) is False

    @pytest.mark.real_dns  # IP literals: parsed numerically, no DNS traffic
    def test_no_authority_still_refuses_internal(self, tmp_path):
        """The strict default path: omitting ``inherited`` entirely (the
        single legal "nothing is inherited" state, now that a partial
        authority is unconstructible -- see InheritedAuthority) still
        refuses every internal target exactly as before this change."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        reason = snap._redirect_host_block_reason("http://127.0.0.1:5002/x")
        assert reason is not None
        assert "loopback" in reason

    def test_both_sides_of_the_comparison_unwrap_ipv4_mapped(
        self, tmp_path, monkeypatch
    ):
        """The inherited set and the target address must be NORMALISED THE
        SAME WAY before they are compared.

        Added because a mutation that compared the target's RAW resolved
        address against the (unwrapped) inherited set survived the whole
        suite. It fails closed rather than open -- an operator-chosen host
        silently loses its exemption -- which is exactly the shape of the
        regression this round exists to fix, so it is worth a test rather
        than a shrug.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")

        def _mapped_getaddrinfo(host, port, *args, **kwargs):
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "",
                     ("::ffff:127.0.0.1", 0, 0, 0))]

        monkeypatch.setattr(snap.socket, "getaddrinfo", _mapped_getaddrinfo)

        # _resolve_host_addresses stores the UNWRAPPED form ...
        assert snap._resolve_host_addresses("svc.example.com") == frozenset(
            {"127.0.0.1"}
        )
        # ... so a target resolving to the mapped form must match it. The
        # target has no explicit port, so its effective port is 80 (http
        # default) -- the inherited port passed here (80) matches it. The
        # inherited host is the target's OWN host ("svc.example.com") --
        # this call checks the same host against itself, so host-identity
        # continuity holds and the address-normalisation rescue is reached.
        assert snap._redirect_host_block_reason(
            "http://svc.example.com/x",
            snap.InheritedAuthority(
                host="svc.example.com", addrs=frozenset({"127.0.0.1"}), port=80
            ),
        ) is None

        # And the negative: a public original inherits nothing, so the same
        # mapped target is still refused -- and still NAMED as loopback. The
        # port (80) and host ("svc.example.com") are both irrelevant to this
        # outcome -- the address never matches the inherited set, so the
        # rescue path (where host is even consulted) is never reached -- but
        # InheritedAuthority still requires both.
        reason = snap._redirect_host_block_reason(
            "http://svc.example.com/x",
            snap.InheritedAuthority(
                host="svc.example.com",
                addrs=frozenset({"93.184.216.34"}),
                port=80,
            ),
        )
        assert reason is not None
        assert "loopback" in reason

    def test_handler_resolves_the_ORIGINAL_url_host_once(self, tmp_path, monkeypatch):
        """The handler's inherited set comes from the original input url, and
        is computed once per handler however many hops follow."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        calls = []

        def _fake_getaddrinfo(host, port, *args, **kwargs):
            calls.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

        monkeypatch.setattr(snap.socket, "getaddrinfo", _fake_getaddrinfo)

        handler = snap._SafeRedirectHandler("http://chosen.example.com:5002/a")
        assert handler._inherited() == frozenset({"127.0.0.1"})
        assert calls == ["chosen.example.com"], (
            "the inherited set must come from the ORIGINAL url's host"
        )
        assert handler._inherited() == frozenset({"127.0.0.1"})
        assert calls == ["chosen.example.com"], "memoised: resolved exactly once"

    def test_handler_inherits_nothing_when_the_original_is_unresolvable(
        self, tmp_path, monkeypatch
    ):
        """Fail closed: an original we cannot resolve exempts nothing."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")

        def _fake_getaddrinfo(host, port, *args, **kwargs):
            raise socket.gaierror(-2, "Name or service not known")

        monkeypatch.setattr(snap.socket, "getaddrinfo", _fake_getaddrinfo)
        handler = snap._SafeRedirectHandler("http://nope.invalid/a")
        assert handler._inherited() == frozenset()

    @pytest.mark.real_dns  # a malformed label must reach the REAL getaddrinfo
    def test_handler_inherits_nothing_when_the_original_is_malformed(
        self, tmp_path
    ):
        """The UnicodeError path on the ORIGINAL host, not just the target.

        Needs real_dns: conftest's autouse resolver mock answers 127.0.0.1 for
        ANY hostname, malformed included, so without the opt-out this test
        would pass against code that never catches UnicodeError at all.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        handler = snap._SafeRedirectHandler("http://" + "a" * 300 + ".com/a")
        assert handler._inherited() == frozenset()

    # ------------------------------------------------------------------
    # The three-row SSRF table. Inheritance is trusted only when the
    # ORIGINAL host resolves to nothing globally reachable, because DNS for
    # a name is controlled by whoever owns the name. One row is the attack;
    # the other two are the controls that make it a test rather than an
    # observation that everything is blocked / everything is allowed.
    # ------------------------------------------------------------------

    ATTACKER_PUBLIC = "93.184.216.34"
    ATTACKER_INTERNAL = "127.0.0.1"

    @staticmethod
    def _handler_resolving(snap, monkeypatch, mapping, original_url):
        """A handler for original_url, with getaddrinfo stubbed by mapping.

        A hostname absent from mapping resolves to ITSELF if it is an IP
        literal (so a redirect target can be an address without needing an
        entry) and raises gaierror otherwise.
        """

        def _fake_getaddrinfo(host, port=None, *args, **kwargs):
            addrs = mapping.get(host)
            if addrs is None:
                try:
                    ipaddress.ip_address(host)
                except ValueError:
                    raise socket.gaierror(
                        socket.EAI_NONAME, "Name or service not known"
                    )
                addrs = [host]
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port or 0))
                for a in addrs
            ]

        monkeypatch.setattr(snap.socket, "getaddrinfo", _fake_getaddrinfo)
        return snap._SafeRedirectHandler(original_url)

    def _verdict(self, snap, handler, original_url, target):
        """FOLLOWED (the new Request) or BLOCKED (the RuntimeError message).

        Goes through redirect_request rather than calling the predicate with
        a hand-built set, because the rule under test is precisely WHICH set
        the handler hands the predicate.
        """
        req = snap.urllib.request.Request(original_url)
        fp = _FakeRedirectFP()
        try:
            new_req = handler.redirect_request(
                req, fp, 302, "Found", {}, target
            )
        except RuntimeError as exc:
            return "BLOCKED", str(exc)
        return "FOLLOWED", new_req.full_url

    def test_ROW1_attack_mixed_public_and_internal_original_inherits_nothing(
        self, tmp_path, monkeypatch
    ):
        """ROW 1, THE ATTACK -- must be BLOCKED.

        attacker.test resolves to a global address AND to loopback, which the
        attacker can simply publish because they own DNS for their own name,
        and then 302s to that loopback address.

        WITHOUT the no-global gate this row is FOLLOWED: loopback is a member
        of the original host's own resolved set, so intent inheritance rescues
        it and the fetch reaches the local admin surface -- the SSRF round 1's
        guard refused and round 2 reopened. The gate makes the inherited set
        empty for any original that is publicly reachable, so the hop is
        refused by the loopback label as it was before inheritance existed.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "http://attacker.test/start"
        handler = self._handler_resolving(
            snap,
            monkeypatch,
            {"attacker.test": [self.ATTACKER_PUBLIC, self.ATTACKER_INTERNAL]},
            original,
        )

        assert handler._inherited() == frozenset(), (
            "a publicly-reachable original must inherit NOTHING, not its own "
            "internal addresses"
        )

        verdict, detail = self._verdict(
            snap, handler, original, f"http://{self.ATTACKER_INTERNAL}:5001/admin"
        )
        assert verdict == "BLOCKED", f"SSRF: the attack redirect was {verdict}"
        assert "loopback" in detail, detail
        assert self.ATTACKER_INTERNAL in detail, detail

    def test_ROW2_control_purely_public_original_is_blocked_the_same_way(
        self, tmp_path, monkeypatch
    ):
        """ROW 2, CONTROL A -- BLOCKED, and it always was.

        legit.test resolves ONLY to the same global address and sends the
        SAME 302 to the same loopback target. It was blocked before this
        round and after it, in both cases because a public original inherits
        nothing internal.

        What it controls for: without it, row 1 going BLOCKED is equally
        consistent with 'the fix blocks everything'. This row shows the
        already-blocked case is UNCHANGED by the fix, so row 1's block is the
        attack being refused rather than the guard widening onto all
        redirects. Everything asserted here therefore holds on the PRE-fix
        code too, deliberately: a control that only passes after the change is
        a second detector of the change, not a control on it.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "http://legit.test/start"
        handler = self._handler_resolving(
            snap, monkeypatch, {"legit.test": [self.ATTACKER_PUBLIC]}, original
        )

        # Invariant across the fix -- before it the inherited set was the
        # original's own {public address}, after it the empty set, and
        # loopback is in neither.
        assert self.ATTACKER_INTERNAL not in handler._inherited()

        verdict, detail = self._verdict(
            snap, handler, original, f"http://{self.ATTACKER_INTERNAL}:5001/admin"
        )
        assert verdict == "BLOCKED", detail
        assert "loopback" in detail, detail

    def test_ROW3_control_purely_internal_original_still_inherits(
        self, tmp_path, monkeypatch
    ):
        """ROW 3, CONTROL B -- FOLLOWED, and it must stay FOLLOWED.

        lan.internal resolves only to internal addresses and 302s to
        ITSELF -- the same hostname, trailing slash added -- which is the
        operator-chosen-host workflow the exemption exists for.

        REWORKED for #2691's host-identity fix (security re-read): this row
        used to redirect to a SIBLING ADDRESS out of the same resolved set,
        reached via an IP LITERAL ('http://10.1.2.3/threads/') rather than by
        the original hostname.  That shape is now a DIFFERENT host identity
        (a literal IP is not the LAN name that resolves to it) and is
        deliberately BLOCKED post-fix -- see
        test_lan_name_redirecting_to_its_own_literal_IP_is_now_blocked, which
        carries that exact former scenario forward as the documented
        behaviour-change test. This row keeps ROW 3's original job -- does a
        purely-internal original still inherit at all -- on a redirect shape
        the fix actually still grants: same hostname, not merely a sibling
        address.

        What it controls for: if the fix had been written as 'never
        inherit' rather than 'inherit only from an original that is not
        publicly reachable', rows 1 and 2 would look identical to their
        current state and this row would go red. Measured rather than
        assumed -- stubbing _inherited to return frozenset() unconditionally
        reddens five tests: this one, ROW 4,
        test_handler_resolves_the_ORIGINAL_url_host_once, and the two
        integration controls that fetch through a redirect
        (test_permitted_redirect_succeeds_and_archives_final_body,
        test_lan_host_redirecting_to_ITSELF_succeeds_and_archives). So this
        row is not the only detector of that over-correction; it is the one
        that states the rule directly. (Was four before ROW 4 was added;
        re-measured on both interpreters when it was.)
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "http://lan.internal/threads"
        handler = self._handler_resolving(
            snap,
            monkeypatch,
            {"lan.internal": ["127.0.0.1", "10.1.2.3"]},
            original,
        )

        assert handler._inherited() == frozenset({"127.0.0.1", "10.1.2.3"}), (
            "a purely-internal original still inherits its own addresses"
        )

        verdict, detail = self._verdict(
            snap, handler, original, "http://lan.internal/threads/"
        )
        assert verdict == "FOLLOWED", f"inheritance was lost: {detail}"
        assert detail == "http://lan.internal/threads/"

    def test_ROW4_control_purely_CGNAT_original_still_inherits(
        self, tmp_path, monkeypatch
    ):
        """ROW 4, CONTROL C -- FOLLOWED, and it is the row that pins is_global.

        Same shape as ROW 3, but the purely-internal original is a TAILNET
        peer: every address it resolves to is in 100.64.0.0/10, RFC 6598
        shared address space, which is the range we actually live in.

        WHY THIS ROW EXISTS SEPARATELY FROM ROW 3.  Rows 1-3 are satisfied by
        EITHER spelling of the inheritance gate, because every address in them
        is is_private and is_global-False together.  CGNAT is the range where
        the two spellings come apart: 100.64.0.1 is is_global False AND
        is_private False (CPython's deliberate design -- see
        _internal_address_label).  So this row is the only one that
        DISTINGUISHES them.

        THE MUTATION IT DETECTS, named because it was measured to be
        undetected before this test existed: writing the gate in
        _SafeRedirectHandler._inherited as

            any(_is_globally_reachable(a))  ->  any(not a.is_private)

        i.e. `any(not is_private)` in place of `any(is_global)`.  Applied to
        that gate alone it left every OTHER test in this file green on both
        3.12.3 and 3.14.4 -- 159 passed with this row deselected.

        WHAT WOULD BE TRUE IF THE GATE WERE WRONG THAT WAY: every address
        here is not-is_private, so `any(not is_private)` fires, the inherited
        set is emptied, and this row's self-redirect is BLOCKED with the
        "shared-address-space (CGNAT)" label -- a tailnet peer that redirects
        to itself silently stops working.  The direction is over-blocking, so
        it is a workflow break rather than an SSRF hole; that is exactly why
        nothing else catches it.

        Not a second copy of ROW 3: ROW 3 states the inherit-from-internal
        rule, this row states which PREDICATE decides "internal".

        REWORKED for #2691's host-identity fix, same reason as ROW 3: the
        redirect target used to be a sibling address reached by IP LITERAL
        rather than by the original hostname, which is now a different host
        identity and deliberately blocked (see
        test_lan_name_redirecting_to_its_own_literal_IP_is_now_blocked). This
        row now redirects to the SAME hostname, keeping its original job --
        pin is_global as the gate -- on a shape the fix still grants.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "http://tailnet.peer/threads"
        handler = self._handler_resolving(
            snap,
            monkeypatch,
            {"tailnet.peer": ["100.64.0.1", "100.100.5.7"]},
            original,
        )

        # Guards the premise rather than assuming it: if a future CPython
        # made CGNAT is_private, this row would silently stop being the
        # discriminating one and would pass for the wrong reason.
        assert not ipaddress.ip_address("100.64.0.1").is_private, (
            "premise broken: CGNAT is now is_private, so this row no longer "
            "distinguishes any(is_global) from any(not is_private)"
        )

        assert handler._inherited() == frozenset(
            {"100.64.0.1", "100.100.5.7"}
        ), (
            "a purely-CGNAT original is purely internal and must inherit its "
            "own addresses"
        )

        verdict, detail = self._verdict(
            snap, handler, original, "http://tailnet.peer/threads/"
        )
        assert verdict == "FOLLOWED", (
            f"a tailnet peer's self-redirect was {verdict}: {detail}"
        )
        assert detail == "http://tailnet.peer/threads/"

    # ------------------------------------------------------------------
    # #2691 security re-read: HOST-IDENTITY CONTINUITY.  Inheritance is
    # keyed on ADDRESS, but HTTP authority is (address, port, HOST) -- a
    # DIFFERENT hostname on the SAME address and port is a DIFFERENT
    # virtual host, and address-set membership alone must never be read as
    # authorising a new one.  The code's own reasoning already said "the
    # operator chose the HOST"; the implementation checked only the
    # resolved address.  This section closes that gap: host equality (in
    # _redirect_host_block_reason) is now a prerequisite for the
    # address-set rescue on every path, including the 80->443 upgrade
    # exception.
    #
    # The equality is UNNORMALISED -- both sides are urlparse().hostname
    # verbatim -- because that is the string urllib puts in the Host
    # header and a server picks its vhost off the literal header.  The
    # trailing-dot tests below are where that stops being a stylistic
    # choice; see them, and the HOST IDENTITY block comment in the source
    # module, for the wire measurement.
    # ------------------------------------------------------------------

    def test_same_address_same_port_DIFFERENT_HOST_is_blocked(
        self, tmp_path, monkeypatch
    ):
        """THE core case. chosen.lan and admin.lan both resolve to the same
        loopback address and port -- two different LAN names sharing one
        (address, port) authority.  A compromised chosen.lan redirecting to
        admin.lan must not be rescued merely because DNS put both names on
        the same address: that is exactly steering at an internal vhost the
        operator never named.

        Before #2691's host-identity fix: FOLLOWED (address-set membership
        alone rescued it).  After: BLOCKED.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "http://chosen.lan/threads"
        handler = self._handler_resolving(
            snap,
            monkeypatch,
            {"chosen.lan": ["127.0.0.1"], "admin.lan": ["127.0.0.1"]},
            original,
        )
        assert handler._inherited() == frozenset({"127.0.0.1"})

        verdict, detail = self._verdict(
            snap, handler, original, "http://admin.lan/threads"
        )
        assert verdict == "BLOCKED", (
            f"a different hostname on the same (address, port) was "
            f"{verdict}: {detail}"
        )
        assert "admin.lan" in detail
        assert "DIFFERENT hostname" in detail

    def test_same_address_scheme_upgrade_DIFFERENT_HOST_is_blocked(
        self, tmp_path, monkeypatch
    ):
        """The named 80->443 upgrade exception is gated on host identity
        too.  In its address-only form the exception granted a NEW PORT
        *and* a NEW HOST in a single step; gating it on host equality closes
        that without removing the exception itself (see the sibling test
        below, where the host IS unchanged and the upgrade still succeeds).
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "http://chosen.lan/threads"  # port 80
        handler = self._handler_resolving(
            snap,
            monkeypatch,
            {"chosen.lan": ["127.0.0.1"], "admin.lan": ["127.0.0.1"]},
            original,
        )

        verdict, detail = self._verdict(
            snap, handler, original, "https://admin.lan/threads"  # port 443
        )
        assert verdict == "BLOCKED", (
            f"an http->https upgrade to a DIFFERENT hostname was "
            f"{verdict}: {detail}"
        )
        assert "DIFFERENT hostname" in detail

    def test_same_host_trailing_slash_self_redirect_still_allowed(
        self, tmp_path, monkeypatch
    ):
        """The workflow the whole exemption exists for, restated where a
        real hostname (not a bare IP literal) is on both ends -- the
        predicate-level sibling
        test_internal_target_in_the_original_set_is_allowed uses '127.0.0.1'
        throughout, so a name is never actually compared to a name there.
        Still FOLLOWED: the host is genuinely unchanged.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "http://chosen.lan/threads"
        handler = self._handler_resolving(
            snap, monkeypatch, {"chosen.lan": ["127.0.0.1"]}, original
        )

        verdict, detail = self._verdict(
            snap, handler, original, "http://chosen.lan/threads/"
        )
        assert verdict == "FOLLOWED", detail
        assert detail == "http://chosen.lan/threads/"

    def test_same_host_scheme_upgrade_still_allowed(
        self, tmp_path, monkeypatch
    ):
        """The 80->443 upgrade exception survives when it is genuinely the
        SAME host asking for it: host-identity continuity GATES the
        exception, it does not remove it."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "http://chosen.lan/threads"  # port 80
        handler = self._handler_resolving(
            snap, monkeypatch, {"chosen.lan": ["127.0.0.1"]}, original
        )

        verdict, detail = self._verdict(
            snap, handler, original, "https://chosen.lan/threads"  # port 443
        )
        assert verdict == "FOLLOWED", detail
        assert detail == "https://chosen.lan/threads"

    def test_lan_name_redirecting_to_its_own_literal_IP_is_now_blocked(
        self, tmp_path, monkeypatch
    ):
        """THE DOCUMENTED BEHAVIOUR CHANGE (#2691 security re-read).

        Before the host-identity fix, chosen.lan resolving to 127.0.0.1 and
        302ing to the literal address http://127.0.0.1/x was ALLOWED --
        127.0.0.1 is a member of chosen.lan's own resolved set.  It is now
        REFUSED: 'chosen.lan' and '127.0.0.1' are different host identities
        even though they name the same address, and such a rescue must be an
        explicit operator opt-in or a connection-pinned design, never
        ambient authority inferred from what DNS happens to answer.

        This carries forward the exact scenario ROW 3 and ROW 4 used to
        exercise (a LAN name redirecting to a sibling address by IP LITERAL
        rather than by hostname) -- both were reworked to redirect by
        hostname instead once this test took over their former shape; see
        their updated docstrings.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "http://chosen.lan/threads"
        handler = self._handler_resolving(
            snap, monkeypatch, {"chosen.lan": ["127.0.0.1"]}, original
        )
        assert handler._inherited() == frozenset({"127.0.0.1"})

        verdict, detail = self._verdict(
            snap, handler, original, "http://127.0.0.1/threads/"
        )
        assert verdict == "BLOCKED", (
            f"a LAN-name -> literal-IP redirect was {verdict}, but this is "
            f"the documented post-#2691 behaviour change: {detail}"
        )
        assert "DIFFERENT hostname" in detail
        assert "127.0.0.1" in detail

    # Removed-test: test_host_identity_is_case_and_trailing_dot_insensitive — it asserted the DEFECT
    # that a redirect chosen.lan -> chosen.lan. is the same host identity
    # and must be FOLLOWED. #2691's security re-read established that
    # urllib sends the raw host, so that hop arrives at a DIFFERENT virtual
    # host; the tests below replace it, and the first of them is the same
    # scenario with the opposite (now correct) expectation.

    def test_trailing_dot_is_a_DIFFERENT_host_and_is_refused(
        self, tmp_path, monkeypatch
    ):
        """#2691 security re-read, blocker 2 -- THE case, and the half of
        this pair that must BLOCK.

        The predecessor of this test asserted the opposite: that
        'chosen.lan.' and 'chosen.lan' are the same host identity, because
        a `_normalised_host` helper stripped a single trailing dot before
        the comparison, justified as 'they name the same DNS zone (the
        trailing dot is the root label, RFC 1035)'. That justification is
        true about NAME RESOLUTION and false about VHOST SELECTION, which
        is what this guard protects: a server picks its virtual host off
        the LITERAL Host header.

        And urllib sends the RAW host -- measured, and the reason the strip
        is gone rather than merely re-argued:

            input host    urlparse().hostname    wire "Host:" header
            chosen.lan    chosen.lan             chosen.lan:PORT
            chosen.lan.   chosen.lan.            chosen.lan.:PORT

        So under the old strip, a redirect chosen.lan -> chosen.lan. passed
        authorization as 'the same host' and arrived at a DIFFERENT virtual
        host. It is now refused.

        The control below is the discriminating other half: a genuinely
        same-host redirect must still be FOLLOWED, or this outcome would be
        indistinguishable from a guard that blocks everything.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "http://chosen.lan/threads"
        handler = self._handler_resolving(
            snap,
            monkeypatch,
            {"chosen.lan": ["127.0.0.1"], "chosen.lan.": ["127.0.0.1"]},
            original,
        )
        assert handler._inherited_authority.host == "chosen.lan", (
            "the stored inherited host is urlparse().hostname VERBATIM -- "
            "no fold, so it is the string that goes on the wire"
        )

        verdict, detail = self._verdict(
            snap, handler, original, "http://chosen.lan./threads/"
        )
        assert verdict == "BLOCKED", (
            f"a redirect to the trailing-dot form of the operator's own "
            f"host was {verdict}, but it selects a different vhost on the "
            f"wire: {detail}"
        )
        assert "DIFFERENT hostname" in detail
        assert "chosen.lan." in detail

    def test_trailing_dot_ORIGINAL_does_not_authorise_the_dotless_host(
        self, tmp_path, monkeypatch
    ):
        """The same rule in the other direction, because a fold is
        symmetric and its absence must be too: an operator who typed the
        trailing-dot form does not thereby authorise the dotless one.

        Without this, a fix that special-cased only 'target has a dot the
        original lacks' would pass the test above while leaving the
        mirror-image hop open.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "http://chosen.lan./threads"
        handler = self._handler_resolving(
            snap,
            monkeypatch,
            {"chosen.lan": ["127.0.0.1"], "chosen.lan.": ["127.0.0.1"]},
            original,
        )
        assert handler._inherited_authority.host == "chosen.lan."

        verdict, detail = self._verdict(
            snap, handler, original, "http://chosen.lan/threads"
        )
        assert verdict == "BLOCKED", (
            f"a trailing-dot original authorised the dotless host: {detail}"
        )
        assert "DIFFERENT hostname" in detail

    def test_same_host_verbatim_is_still_allowed_beside_the_dot_refusal(
        self, tmp_path, monkeypatch
    ):
        """THE CONTROL for the two tests above, and what makes them a
        finding rather than an observation that everything is blocked.

        Same handler, same stubbed resolver, same shape of hop -- the ONLY
        difference from the first test is that the target's host is the
        operator's own host verbatim rather than its trailing-dot form.
        This one must still be FOLLOWED: the workflow the whole inheritance
        exemption exists for (trailing-SLASH normalisation on the host the
        operator typed) is untouched by the trailing-DOT refusal.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "http://chosen.lan/threads"
        handler = self._handler_resolving(
            snap,
            monkeypatch,
            {"chosen.lan": ["127.0.0.1"], "chosen.lan.": ["127.0.0.1"]},
            original,
        )

        verdict, detail = self._verdict(
            snap, handler, original, "http://chosen.lan/threads/"
        )
        assert verdict == "FOLLOWED", (
            f"the operator's own host redirecting to itself was {verdict} "
            f"-- the dot refusal must not have become a blanket block: "
            f"{detail}"
        )
        assert detail == "http://chosen.lan/threads/"

    def test_ascii_case_still_matches_and_the_residual_is_urlparses(
        self, tmp_path, monkeypatch
    ):
        """The casefold half of blocker 2, resolved and PINNED rather than
        assumed, because the answer is not the same as the trailing dot's.

        `_normalised_host`'s `.casefold()` is gone too -- it collapsed
        pairs the wire keeps distinct ('straße.lan'.casefold() ==
        'strasse.lan', while urlparse leaves the two hostnames different
        and urllib sends them differently). But removing it does NOT make the
        comparison case-sensitive, because `urlparse().hostname` lowercases
        ASCII on its own, BEFORE anything in this module sees the host.
        That is a real residual divergence from the wire (measured: input
        'Chosen.LAN' -> urlparse 'chosen.lan' -> wire 'Chosen.LAN:PORT'),
        left in place deliberately: RFC 3986 s3.2.2 makes the host
        component case-insensitive, so a conformant server cannot select a
        different vhost on case alone, and closing it would need a second
        hostname parser diverging from the one every resolution path here
        uses.

        This test pins the residual so a future reader finds the behaviour
        stated rather than inferred -- and so a change to it is a visible
        test edit rather than a silent one.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")

        # The residual, measured at its source rather than asserted.
        assert snap.urllib.parse.urlparse(
            "http://Chosen.LAN/x"
        ).hostname == "chosen.lan"
        assert snap.urllib.parse.urlparse(
            "http://Chosen.LAN./x"
        ).hostname == "chosen.lan."

        original = "http://chosen.lan/threads"
        handler = self._handler_resolving(
            snap,
            monkeypatch,
            {"chosen.lan": ["127.0.0.1"], "chosen.lan.": ["127.0.0.1"]},
            original,
        )

        # ASCII case alone: still FOLLOWED (urlparse folded it, not us).
        verdict, detail = self._verdict(
            snap, handler, original, "http://Chosen.LAN/threads/"
        )
        assert verdict == "FOLLOWED", detail
        assert detail == "http://Chosen.LAN/threads/"

        # Case AND a trailing dot: BLOCKED, because the dot survives
        # urlparse and is no longer stripped by us.
        verdict, detail = self._verdict(
            snap, handler, original, "http://Chosen.LAN./threads/"
        )
        assert verdict == "BLOCKED", (
            f"the trailing-dot form was {verdict} when spelled in mixed "
            f"case -- the dot check must not depend on case: {detail}"
        )
        assert "DIFFERENT hostname" in detail

    def test_casefold_collapse_of_two_wire_distinct_hosts_is_refused(
        self, tmp_path, monkeypatch
    ):
        """The casefold removal, made ENFORCED rather than merely argued.

        Without this the removal is invisible to the suite: re-adding
        `.casefold()` to both sides of the comparison changes no other
        outcome here, because urlparse has already folded every ASCII case
        difference before the comparison runs. So the argument for removing
        it -- that casefold COLLAPSES pairs urlparse and the wire keep
        distinct -- needs the one input where that is observable.

        'straße.lan'.casefold() == 'strasse.lan', while
        urlparse('http://straße.lan/x').hostname is 'straße.lan' and
        urllib puts that raw string in the Host header. So under a
        casefolding comparison, an operator who chose straße.lan authorises
        a redirect to the wholly different host strasse.lan -- the same
        shape of defect as the trailing dot, and by the same mechanism (an
        authorization key that is not the wire string).

        Bound, stated so this is not read as more than it is: the claim
        under test is about the host-IDENTITY COMPARISON, not about what a
        resolver or an IDNA encoder would do with such a name; the resolver
        is stubbed here exactly as in the sibling tests.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")

        # The collapse, measured at its source.
        assert "straße.lan".casefold() == "strasse.lan"
        assert snap.urllib.parse.urlparse(
            "http://straße.lan/x"
        ).hostname == "straße.lan"

        original = "http://straße.lan/threads"
        handler = self._handler_resolving(
            snap,
            monkeypatch,
            {"straße.lan": ["127.0.0.1"], "strasse.lan": ["127.0.0.1"]},
            original,
        )
        assert handler._inherited_authority.host == "straße.lan", (
            "the stored host must not be casefolded"
        )

        verdict, detail = self._verdict(
            snap, handler, original, "http://strasse.lan/threads"
        )
        assert verdict == "BLOCKED", (
            f"a casefold-equal but wire-DISTINCT hostname was {verdict}: "
            f"{detail}"
        )
        assert "DIFFERENT hostname" in detail

        # CONTROL: the same host verbatim still passes, so the outcome
        # above is a host-identity refusal and not this hostname being
        # rejected for being non-ASCII.
        verdict, detail = self._verdict(
            snap, handler, original, "http://straße.lan/threads/"
        )
        assert verdict == "FOLLOWED", detail

    def test_host_comparison_is_exact_not_substring(
        self, tmp_path, monkeypatch
    ):
        """A plausible-WRONG implementation checks host containment
        ('inherited_host in target_host') rather than equality -- which a
        redirect to a hostname that merely CONTAINS the original's hostname
        as a substring would then satisfy, even though it names a wholly
        different host. 'evilchosen.lan' is not 'chosen.lan', however the
        comparison is spelled; it must still be refused.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "http://chosen.lan/threads"
        handler = self._handler_resolving(
            snap,
            monkeypatch,
            {"chosen.lan": ["127.0.0.1"], "evilchosen.lan": ["127.0.0.1"]},
            original,
        )

        verdict, detail = self._verdict(
            snap, handler, original, "http://evilchosen.lan/threads"
        )
        assert verdict == "BLOCKED", (
            f"a hostname merely CONTAINING the original's hostname as a "
            f"substring was {verdict}: {detail}"
        )
        assert "evilchosen.lan" in detail

    # ------------------------------------------------------------------
    # #2691 round N: the authority the original url grants must be sampled
    # BEFORE the connection (not lazily inside redirect_request, which an
    # attacker can time), and must be keyed on (address, effective_port),
    # not address alone.
    # ------------------------------------------------------------------

    def test_temporal_dns_flip_after_construction_does_not_authorize_the_hop(
        self, tmp_path, monkeypatch
    ):
        """Defect (1): the attacker chooses when their own 30x arrives, and
        so chooses when a LAZY authority lookup would run.

        attacker.test resolves GLOBALLY on the FIRST getaddrinfo call FOR
        THAT HOST -- modelling the address urllib actually connects to -- and
        to loopback on every subsequent call for it -- modelling the attacker
        flipping their own DNS between the original connection and the
        redirect being processed. A lazy, post-response sample would see only
        the post-flip, internal-only answer, satisfy the no-global gate on a
        lie, and inherit the attacker's own loopback. The fix samples once,
        at construction, before any of that can happen -- so this test also
        pins that the ORIGINAL host is resolved exactly ONCE overall, not
        just that the verdict comes out right. (The redirect TARGET's own
        host -- a bare IP literal here -- is still resolved every hop, same
        as always; that is a different, unrelated call this test does not
        forbid, so the count below is scoped to "attacker.test" specifically
        rather than to every getaddrinfo call.)
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        calls = []

        def _flipping_getaddrinfo(host, port=None, *args, **kwargs):
            calls.append(host)
            if host == "attacker.test":
                addr = (
                    self.ATTACKER_PUBLIC
                    if calls.count("attacker.test") == 1
                    else self.ATTACKER_INTERNAL
                )
            else:
                # The redirect target is a bare IP literal in this test;
                # resolve it to itself, same as real DNS/getaddrinfo would.
                addr = host
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port or 0))]

        monkeypatch.setattr(snap.socket, "getaddrinfo", _flipping_getaddrinfo)

        original = "http://attacker.test/start"
        handler = snap._SafeRedirectHandler(original)
        assert calls.count("attacker.test") == 1, (
            "the authority must already be sampled by the time __init__ "
            "returns -- before any redirect is even in play"
        )
        assert handler._inherited() == frozenset(), (
            "the FIRST (pre-flip) answer was globally reachable, so nothing "
            "is inherited -- exactly as if DNS never flipped at all"
        )

        verdict, detail = self._verdict(
            snap, handler, original, f"http://{self.ATTACKER_INTERNAL}:5001/admin"
        )
        assert calls.count("attacker.test") == 1, (
            "processing the redirect must not trigger a SECOND resolution of "
            "the ORIGINAL host -- a second call is exactly the lazy "
            "re-sample this test forbids"
        )
        assert verdict == "BLOCKED", (
            f"a post-construction DNS flip authorized the hop: {verdict}"
        )
        assert "loopback" in detail, detail

    def test_same_address_different_port_is_blocked(self, tmp_path, monkeypatch):
        """Defect (2), core case: a redirect to a DIFFERENT port on the SAME
        address is not the authority the operator granted -- sibling ports
        are different services on the same host, and a compromised one must
        not be able to steer us at a sibling admin surface."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "http://127.0.0.1:5001/threads/1"
        handler = self._handler_resolving(snap, monkeypatch, {}, original)

        verdict, detail = self._verdict(
            snap, handler, original, "http://127.0.0.1:5002/admin"
        )
        assert verdict == "BLOCKED", detail
        assert "127.0.0.1" in detail
        assert "5002" in detail and "5001" in detail, (
            f"the error should name both the target and original ports; got: {detail}"
        )

    def test_same_address_same_port_self_redirect_still_succeeds(
        self, tmp_path, monkeypatch
    ):
        """THE control for defect (2): the trailing-slash / self-redirect
        workflow the exemption exists for must still work when the port is
        explicit and UNCHANGED -- the new (address, port) key must not
        accidentally require a default port to match."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "http://127.0.0.1:5001/threads"
        handler = self._handler_resolving(snap, monkeypatch, {}, original)

        verdict, detail = self._verdict(
            snap, handler, original, "http://127.0.0.1:5001/threads/"
        )
        assert verdict == "FOLLOWED", detail
        assert detail == "http://127.0.0.1:5001/threads/"

    def test_mixed_public_and_internal_resolution_still_blocked(
        self, tmp_path, monkeypatch
    ):
        """The no-global gate is orthogonal to defects (1) and (2) and must
        survive both the eager-resolution and the port-keyed changes: a
        hostname original that resolves to ANY globally reachable address
        inherits nothing, even though resolution now happens eagerly, at
        construction, rather than lazily inside redirect_request."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "http://mixed.example.com/start"
        handler = self._handler_resolving(
            snap,
            monkeypatch,
            {"mixed.example.com": [self.ATTACKER_PUBLIC, self.ATTACKER_INTERNAL]},
            original,
        )
        assert handler._inherited() == frozenset()

        verdict, detail = self._verdict(
            snap, handler, original, f"http://{self.ATTACKER_INTERNAL}/admin"
        )
        assert verdict == "BLOCKED", detail

    def test_scheme_upgrade_same_address_80_to_443_is_allowed(
        self, tmp_path, monkeypatch
    ):
        """THE named exception, and the only one: an ordinary http->https
        upgrade bounce on the SAME address (default ports 80 -> 443) must
        still be followed even though the port changes."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "http://127.0.0.1/threads"  # default port 80
        handler = self._handler_resolving(snap, monkeypatch, {}, original)

        verdict, detail = self._verdict(
            snap, handler, original, "https://127.0.0.1/threads"  # default 443
        )
        assert verdict == "FOLLOWED", detail
        assert detail == "https://127.0.0.1/threads"

    def test_https_to_non_443_port_same_address_is_blocked(
        self, tmp_path, monkeypatch
    ):
        """Proves the scheme-upgrade exception is NARROW, not a general
        cross-port hole: https to a NON-443 port on the same address is not
        the http->https bounce and must still be refused."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "http://127.0.0.1/threads"  # port 80
        handler = self._handler_resolving(snap, monkeypatch, {}, original)

        verdict, detail = self._verdict(
            snap, handler, original, "https://127.0.0.1:8443/threads"
        )
        assert verdict == "BLOCKED", detail

    def test_https_to_http_same_address_is_NOT_an_upgrade_and_is_blocked(
        self, tmp_path, monkeypatch
    ):
        """The exception is ASYMMETRIC: 80 -> 443 is the named upgrade bounce,
        443 -> 80 is a downgrade and is not exempted by the same rule. Added
        because a plausible-wrong implementation treats the exception as 'a
        scheme change on the same address', which a symmetric http<->https
        swap would also satisfy."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        original = "https://127.0.0.1/threads"  # port 443
        handler = self._handler_resolving(snap, monkeypatch, {}, original)

        verdict, detail = self._verdict(
            snap, handler, original, "http://127.0.0.1/threads"  # default 80
        )
        assert verdict == "BLOCKED", detail


# ---------------------------------------------------------------------------
# Integration: a real loopback HTTP server that redirects.
# ---------------------------------------------------------------------------

class _StubHandler(http.server.BaseHTTPRequestHandler):
    """Serves a per-subclass `routes` map of path -> ("redirect"|"body", value)."""

    routes: dict = {}
    protocol_version = "HTTP/1.0"

    def do_GET(self):
        route = self.routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        kind, value = route
        if kind == "redirect":
            self.send_response(302)
            self.send_header("Location", value)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(value)))
            self.end_headers()
            self.wfile.write(value)

    def log_message(self, *args):  # keep pytest output clean
        pass


@contextlib.contextmanager
def _stub_server(routes):
    """Run a loopback HTTP server serving `routes`; yields its base URL."""
    handler = type("_BoundStubHandler", (_StubHandler,), {"routes": routes})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class TestRedirectIntegration:
    """End-to-end over a real server: the guard fires inside urllib's redirect
    path, and -- critically -- a redirect that violates nothing still works."""

    @pytest.mark.real_dns  # public IP literal, parsed numerically -- no DNS
    def test_ftp_redirect_rejected_and_writes_nothing(self, tmp_path, capsys):
        """A fetch 302'd to ftp:// must be refused. ftp is THE scheme gap:
        urllib's own redirect allowlist is ('http', 'https', 'ftp', '').

        The redirect target deliberately uses a PUBLIC IP literal, so the
        internal-host guard would happily allow it -- this test therefore
        isolates the scheme check rather than passing on the host check's
        coattails.

        The archive assertion is the one that matters: a non-zero exit alone
        passes for the wrong reason if the write happened first.
        """
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        target = "ftp://93.184.216.34/payload"
        assert snap._redirect_host_block_reason(target) is None, (
            "precondition: the host guard must NOT be what rejects this target"
        )

        with _stub_server({"/start": ("redirect", target)}) as base:
            with pytest.raises(SystemExit) as exc_info:
                snap._snapshot_web(f"{base}/start", {})

        assert exc_info.value.code == snap.EXIT_IO
        assert _archive_is_empty(evidence_archive), (
            "a blocked redirect must not leave any file under the archive root"
        )
        err = capsys.readouterr().err
        assert target in err, (
            f"the error should name the rejected target; got: {err}"
        )
        assert "not http" in err, (
            f"the error should say the SCHEME is why it was refused; got: {err}"
        )

    @pytest.mark.real_dns  # the IP literals must resolve for real
    def test_inward_redirect_to_a_DIFFERENT_internal_host_rejected(
        self, tmp_path, capsys
    ):
        """A redirect steering us at an internal address the ORIGINAL url did
        not resolve to must be refused.

        The target is a different internal host on purpose. Intent inheritance
        means the same-address case is now legitimately ALLOWED (the test
        below), so a target on this same loopback server would no longer prove
        anything. This is the direction that shows inheritance is not a
        blanket loosening: the original here IS internal, so a naive 'the
        operator opted into internal' rule would exempt the whole chain.

        No server is stood up on 10.1.2.3: the guard refuses before any
        connect, which is precisely the property under test.
        """
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        with _stub_server({"/start": ("redirect", "http://10.1.2.3/secret")}) as base:
            with pytest.raises(SystemExit) as exc_info:
                snap._snapshot_web(f"{base}/start", {})

        assert exc_info.value.code == snap.EXIT_IO
        assert _archive_is_empty(evidence_archive), (
            "a blocked redirect must not leave any file under the archive root"
        )
        err = capsys.readouterr().err
        assert "private" in err, (
            f"the error should say the target resolved internal; got: {err}"
        )
        assert "10.1.2.3" in err, (
            f"the error should name the refused address; got: {err}"
        )

    @pytest.mark.real_dns  # the IP literals must resolve for real
    def test_cgnat_redirect_rejected_and_writes_nothing(self, tmp_path, capsys):
        """A redirect to RFC 6598 shared address space must be refused.

        This is the range `is_private` deliberately reports False for, so the
        pre-change quartet allowed it -- and it is Tailscale's range, so on a
        tailnet a 302 here reaches a real peer. End-to-end rather than only at
        the predicate, because the gap was in the classification the handler
        calls, and a unit test alone would not show the handler consults it.
        """
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        with _stub_server({"/start": ("redirect", "http://100.64.0.1/x")}) as base:
            with pytest.raises(SystemExit) as exc_info:
                snap._snapshot_web(f"{base}/start", {})

        assert exc_info.value.code == snap.EXIT_IO
        assert _archive_is_empty(evidence_archive)
        err = capsys.readouterr().err
        assert "CGNAT" in err, (
            f"the error should name the shared-address-space label; got: {err}"
        )

    @pytest.mark.real_dns  # a malformed label must reach the REAL getaddrinfo
    def test_malformed_redirect_host_exits_cleanly_without_a_traceback(
        self, tmp_path, capsys
    ):
        """getaddrinfo raises UnicodeError -- not an OSError -- for an
        over-long DNS label. Uncaught it escapes the predicate, _fetch_url_bytes'
        HTTPError/URLError/timeout clauses AND _snapshot_web's `except
        RuntimeError`, surfacing as an unhandled traceback rather than the
        tool's EXIT_IO path.

        Asserting the absence of a traceback as well as the exit code: a bare
        `pytest.raises(SystemExit)` would not distinguish the two, since an
        unhandled UnicodeError never reaches SystemExit at all.
        """
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        bad_host = "a" * 300 + ".com"
        with _stub_server(
            {"/start": ("redirect", f"http://{bad_host}/x")}
        ) as base:
            with pytest.raises(SystemExit) as exc_info:
                snap._snapshot_web(f"{base}/start", {})

        assert exc_info.value.code == snap.EXIT_IO
        assert _archive_is_empty(evidence_archive)
        err = capsys.readouterr().err
        assert "Traceback" not in err, (
            f"the operator should get a message, not a stack; got: {err}"
        )
        assert "resolve" in err, (
            f"the error should say the host could not be resolved; got: {err}"
        )

    @pytest.mark.real_dns  # the 127.0.0.1 literals must resolve for real
    def test_permitted_redirect_succeeds_and_archives_final_body(self, tmp_path):
        """THE CONTROL. Without this, every test above also passes against an
        implementation that simply refuses ALL redirects, and a working guard
        is indistinguishable from a broken fetcher.

        It used to monkeypatch _redirect_host_block_reason away, because both
        stub servers sit on loopback and the unconditional host guard would
        have refused the hop. That stub was the reason a block-EVERYTHING
        mutation of the host guard left this control GREEN -- it exercised the
        scheme half only. Intent inheritance removes the need for it: both
        hops resolve to 127.0.0.1, which is the address the input url already
        named, so the redirect is legitimately allowed by the REAL predicate.

        So this control now reaches the host half as well as the scheme
        half -- but it detects over-blocking at the PREDICATE level only, not
        at the GATE level, and intent inheritance is the difference. A
        gate-level over-block has to be caught by TestInternalAddressGate and
        the predicate tests, not here.

        BOTH ROUTES ARE SERVED BY THE SAME STUB SERVER -- same address AND
        same port -- unlike the two-separate-servers shape this test used to
        have. #2691 round N keys intent inheritance on (address,
        effective_port), not address alone, so a same-address DIFFERENT-port
        hop -- which is what the old two-server shape exercised, deliberately
        -- is now refused by design; that scenario moved to
        test_same_address_different_port_is_blocked. This control keeps to
        its original job -- a legitimate redirect must still succeed -- on an
        authority the new key actually grants. (Stale mutation counts from
        the pre-round-N implementation are not reproduced here: the function
        shape changed enough that they would need remeasuring, and the
        replacement coverage for both directions -- over-blocking and
        under-blocking -- now lives across this class plus
        TestInternalAddressGate.)
        """
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        final_body = b"<html><body>the document after the redirect</body></html>"
        routes = {
            "/start": ("redirect", "/final"),
            "/final": ("body", final_body),
        }
        with _stub_server(routes) as base:
            result = snap._snapshot_web(f"{base}/start", {})

        assert result["archive_status"] == "archived"
        assert result["content_hash"] == _sha256(final_body), (
            "the archived hash must be of the FINAL body, not the 302"
        )
        archive_path = Path(result["file_url"][len("file://"):])
        assert archive_path.exists()
        assert archive_path.read_bytes() == final_body

    @pytest.mark.real_dns  # the 127.0.0.1 literal must resolve for real
    def test_lan_host_redirecting_to_ITSELF_succeeds_and_archives(self, tmp_path):
        """THE REGRESSION THIS ROUND FIXES, end to end.

        An operator-chosen internal host that normalises `/threads` to
        `/threads/` -- the standard trailing-slash 301, and the same shape as
        slash merging, an HTTP->HTTPS upgrade and a /login bounce. Before
        intent inheritance the host guard refused this, breaking the exact
        workflow the input exemption was argued to preserve.

        One server, so the redirect is to the same address AND the same port:
        'itself' in the strictest sense, not merely a same-address sibling.
        """
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        body = b"<html><body>the thread, after normalisation</body></html>"
        routes = {
            "/threads": ("redirect", "/threads/"),   # relative, as werkzeug sends
            "/threads/": ("body", body),
        }
        with _stub_server(routes) as base:
            result = snap._snapshot_web(f"{base}/threads", {})

        assert result["archive_status"] == "archived"
        assert result["content_hash"] == _sha256(body)
        assert Path(result["file_url"][len("file://"):]).read_bytes() == body


class TestMalformedRedirectAuthority:
    """A redirect target whose authority carries an unusable port.

    This is the ATTACKER-CONTROLLED half of the malformed-port escape covered
    by TestMalformedAuthorityFailsLoudly: the ``Location:`` header is chosen
    by the remote server, not by the operator, so "the operator can see the
    typo" does not apply here at all.

    Three properties, one per test:

      1. it is REFUSED loudly, by the same fail-closed path as every other
         bad redirect target (handler level), and the 30x response's socket
         is CLOSED on the way out -- the escaping ValueError used to skip the
         ``fp.close()`` that redirect_request's blocked path performs, which
         is a leaked socket (a ResourceWarning under -W error), not merely an
         ugly message;
      2. a WELL-FORMED port is still judged by the ordinary inheritance rule,
         so the new except clause is not swallowing normal verdicts;
      3. the malformed case reaches that path AT ALL through real urllib over
         a real server (integration) -- without which test 1 could be
         guarding a branch urllib's own redirect handling never delivers to.
    """

    ORIGINAL = "http://127.0.0.1/start"

    @pytest.mark.parametrize(
        "target, fragment",
        [
            ("http://127.0.0.1:99999/x", "99999"),
            ("http://127.0.0.1:abc/x", "abc"),
        ],
        ids=["out-of-range", "non-numeric"],
    )
    def test_blocked_and_the_30x_socket_is_closed(self, tmp_path, target, fragment):
        """Handler level: RuntimeError in the existing blocked-redirect shape,
        naming the TARGET, with fp closed."""
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        handler = snap._SafeRedirectHandler(self.ORIGINAL)
        req = snap.urllib.request.Request(self.ORIGINAL)
        fp = _FakeRedirectFP()

        with pytest.raises(RuntimeError) as exc_info:
            handler.redirect_request(req, fp, 302, "Found", {}, target)

        message = str(exc_info.value)
        assert "blocked redirect" in message, (
            "an unusable authority must be refused by the SAME path as every "
            f"other bad target, not a new one; got: {message}"
        )
        assert target in message, (
            "the reason must name the redirect TARGET, not only the original "
            f"URL the operator typed; got: {message}"
        )
        assert fragment in message, message
        assert fp.closed, (
            "the 30x response's socket must be closed before raising -- the "
            "uncaught ValueError used to skip redirect_request's fp.close()"
        )

    def test_CONTROL_a_well_formed_port_still_reaches_the_ordinary_verdict(
        self, tmp_path
    ):
        """THE CONTROL for the handler-level test above.

        A well-formed but non-inherited port on the same address is refused
        too -- but by the PORT-INHERITANCE rule, with its own distinct
        message. If this row produced the unusable-authority message instead,
        the new except clause would be swallowing ordinary verdicts, and the
        row above would be passing for the wrong reason.
        """
        snap = _load_snapshot(tmp_path / "engram", tmp_path / "archive")
        handler = snap._SafeRedirectHandler(self.ORIGINAL)
        req = snap.urllib.request.Request(self.ORIGINAL)
        fp = _FakeRedirectFP()

        with pytest.raises(RuntimeError) as exc_info:
            handler.redirect_request(
                req, fp, 302, "Found", {}, "http://127.0.0.1:5001/x"
            )

        message = str(exc_info.value)
        assert "different port" in message, (
            "a well-formed port must still be judged by the inheritance rule; "
            f"got: {message}"
        )
        assert "unusable authority" not in message, message

    @pytest.mark.real_dns  # the 127.0.0.1 literals must resolve for real
    def test_integration_malformed_Location_exits_cleanly_and_writes_nothing(
        self, tmp_path, capsys
    ):
        """End to end over a real loopback server: a 302 whose Location has an
        out-of-range port must reach the tool's EXIT_IO path with a message,
        not a stack.

        Asserting the absence of a traceback as well as the exit code: an
        unhandled ValueError never reaches SystemExit at all, because
        _snapshot_web catches RuntimeError only -- so a bare
        `pytest.raises(SystemExit)` would not distinguish the two.
        """
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        with _stub_server(
            {"/start": ("redirect", "http://127.0.0.1:99999/x")}
        ) as base:
            with pytest.raises(SystemExit) as exc_info:
                snap._snapshot_web(f"{base}/start", {})

        assert exc_info.value.code == snap.EXIT_IO
        assert _archive_is_empty(evidence_archive), (
            "a blocked redirect must not leave any file under the archive root"
        )
        err = capsys.readouterr().err
        assert "Traceback" not in err, (
            f"the operator should get a message, not a stack; got: {err}"
        )
        assert "99999" in err, (
            f"the error should name the refused authority; got: {err}"
        )


def test_root_copy_is_symlink_to_shipped_copy():
    """tools/engram-snapshot must be a symlink resolving to
    src/engram/tools/engram-snapshot -- NOT a second, independently-editable
    copy.

    This is the structural fix for the drift class that used to require a
    dual-copy content-sync guard: src/engram/tools/engram-snapshot is the one
    authoritative copy (`src/` is authoritative per #2781), and
    tools/engram-snapshot is a symlink onto it, matching its five peers
    (tools/baton.py, tools/ia.py, tools/engram-pkg, tools/forum.py,
    tools/verify_quote.py -- all mode 120000 pointing at
    ../src/engram/tools/<name>). A symlink cannot drift from its target by
    definition, so there is no content to keep "in sync" -- what this test
    guards against instead is someone converting the symlink back into a
    regular file (accidentally, e.g. via a sed -i that follows the link and
    rewrites through it) and thereby reintroducing the second copy this fix
    eliminated.

    A note on what the symlink is NOT protecting: the build never reads
    the repo-root tools/ path at all. The manifest entry for
    tools/engram-snapshot (src/build/packaging/tiers.json) carries no
    "source" override, so _copy_manifest_entry() (src/build/engine/
    build.py) resolves it under source_root ("src/engram") -- landing on
    src/engram/tools/engram-snapshot, the same authoritative copy named
    above, not on tools/engram-snapshot itself. There is no shutil.copy2
    dereference of the root copy to reason about, because the root copy
    was never read in the first place -- the dual-copy drift this fix
    eliminates was never a build-shipping risk. What the symlink buys is
    dev-ergonomics and consistency instead: anyone invoking
    tools/engram-snapshot directly, or any tooling that hardcodes that
    path, now sees current content rather than a copy that could silently
    diverge -- the same guarantee the five peers above already give their
    own direct callers.

    This is also why a symlink assertion is a strictly stronger guard than
    the byte-equality check it replaced: swap the symlink for a
    byte-identical regular file and root_copy.is_symlink() still fails,
    where byte equality would have passed it.
    """
    repo_root = Path(__file__).resolve().parents[1]
    root_copy = repo_root / "tools" / "engram-snapshot"
    shipped_copy = repo_root / "src" / "engram" / "tools" / "engram-snapshot"

    assert shipped_copy.exists(), f"shipped copy missing: {shipped_copy}"
    assert root_copy.is_symlink(), (
        "tools/engram-snapshot must be a symlink, not a regular file -- "
        "a regular file here re-creates the dual-copy drift class this "
        "test exists to make structurally impossible."
    )
    assert root_copy.resolve() == shipped_copy.resolve(), (
        f"tools/engram-snapshot resolves to {root_copy.resolve()}, "
        f"expected {shipped_copy.resolve()}"
    )
