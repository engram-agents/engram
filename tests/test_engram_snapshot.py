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

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import sys
import types
import unittest
import unittest.mock as mock
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"


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

class _FakeHTTPResponse:
    """Minimal file-like context manager mimicking urllib's response object,
    enough to exercise _fetch_url_bytes's chunked-read loop."""

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


class TestSnapshotWebPdfHappyPath:
    """_snapshot_web / _snapshot_pdf archive raw fetched bytes and return the
    correct JSON shape, urllib.request.urlopen mocked so no real network
    call occurs."""

    def test_archives_web_page_raw_bytes(self, tmp_path, monkeypatch):
        engram_home = tmp_path / "engram"
        evidence_archive = tmp_path / "archive"
        snap = _load_snapshot(engram_home, evidence_archive)

        body = b"<html><body>hello world</body></html>"

        def _fake_urlopen(req, timeout=None):
            return _FakeHTTPResponse(body, status=200)

        monkeypatch.setattr(snap.urllib.request, "urlopen", _fake_urlopen)

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

        monkeypatch.setattr(snap.urllib.request, "urlopen", _fake_urlopen)

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

        monkeypatch.setattr(snap.urllib.request, "urlopen", _fake_urlopen)

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

        monkeypatch.setattr(snap.urllib.request, "urlopen", _raise_http_error)

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

        monkeypatch.setattr(snap.urllib.request, "urlopen", _raise_timeout)

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

        monkeypatch.setattr(snap.urllib.request, "urlopen", _raise_url_error)

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

        monkeypatch.setattr(snap.urllib.request, "urlopen", _fake_urlopen)

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

        monkeypatch.setattr(snap.urllib.request, "urlopen", _raise_http_error)

        with pytest.raises(SystemExit) as exc_info:
            snap._snapshot_pdf("https://example.com/paper.pdf", {})
        assert exc_info.value.code == snap.EXIT_IO
        assert _archive_is_empty(evidence_archive), (
            "a failed fetch must not leave any file under the archive root"
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
