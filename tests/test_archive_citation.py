"""Tests for the #1253 deferred-commit relaxation in _capture_file_version.

An UNCOMMITTED file in the content-addressed evidence archive is citable when
its name matches its content hash (content-addressing provides immutability
without a commit). Every OTHER uncommitted file:// in a git repo still hits the
committed-before-cite guard. This is the server-side P1 dependency that lets an
engram-snapshot file:// archive URL be cited before its nap-time git commit.
"""

import hashlib
import os
import subprocess

import pytest

import engram_observation as eo


def _git(repo, *args):
    subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    )


def _make_archive_file(archive_root, content: bytes, ext="txt", correct_name=True):
    """Write a content-addressed file into a git-backed archive (file untracked).

    Returns (file_path, sha). `correct_name=False` writes it under a
    non-matching name to exercise the fall-through-to-guard path.
    """
    sha = hashlib.sha256(content).hexdigest()
    stem = sha if correct_name else ("0" * 64)
    bucket = archive_root / stem[:2]
    bucket.mkdir(parents=True, exist_ok=True)
    fp = bucket / f"{stem}.{ext}"
    fp.write_bytes(content)
    return fp, sha


@pytest.fixture
def archive_root(monkeypatch, tmp_path):
    """A git-backed archive dir wired as EVIDENCE_ARCHIVE_ROOT (mirrors prod:
    ~/.engram is git-backed WITH history). The constant is module-level +
    computed at import, so monkeypatch the attribute the helper reads at call
    time. An initial commit is made so HEAD exists — production-realistic, and
    it lets the cat-file commit-guard fire for non-content-addressed files."""
    root = tmp_path / "evidence-archive"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    (root / ".keep").write_text("seed\n")
    _git(root, "add", ".keep")
    _git(root, "commit", "-q", "-m", "seed")
    monkeypatch.setattr(eo, "EVIDENCE_ARCHIVE_ROOT", str(root))
    return root


def test_uncommitted_content_addressed_file_accepted(archive_root):
    content = b"forum thread 119 rendered text\n"
    fp, sha = _make_archive_file(archive_root, content)
    res = eo._capture_file_version(f"file://{fp}")
    assert res["ok"] is True, res
    assert res["content_hash"] == sha
    assert res["git_sha"] == ""  # deferred — git_sha backfills at nap


def test_jsonl_archive_extension_accepted(archive_root):
    content = b'{"type":"user","text":"hi"}\n'
    fp, sha = _make_archive_file(archive_root, content, ext="jsonl")
    res = eo._capture_file_version(f"file://{fp}")
    assert res["ok"] is True, res
    assert res["content_hash"] == sha


def test_archive_file_with_wrong_name_falls_through_to_guard(archive_root):
    # Name does not match content → not content-addressed → commit guard fires
    # (the file is untracked). This is the integrity gate: a tampered/mis-named
    # archive file is NOT silently accepted.
    content = b"some bytes that do not hash to the filename\n"
    fp, _ = _make_archive_file(archive_root, content, correct_name=False)
    res = eo._capture_file_version(f"file://{fp}")
    assert res["ok"] is False


def test_tampered_archive_file_rejected(archive_root):
    # Correctly named at write time, then bytes mutated → hash no longer matches
    # name → relaxation declines → guard fires. Proves content-addressing is the
    # real integrity check, not just the path prefix.
    content = b"original evidence\n"
    fp, _ = _make_archive_file(archive_root, content)
    fp.write_bytes(b"tampered evidence\n")
    res = eo._capture_file_version(f"file://{fp}")
    assert res["ok"] is False


def test_non_archive_uncommitted_file_still_rejected(monkeypatch, tmp_path):
    # A working-tree file in a git repo, untracked, NOT under the archive root:
    # the commit guard must remain in force (its bytes can still mutate).
    archive = tmp_path / "evidence-archive"
    archive.mkdir()
    monkeypatch.setattr(eo, "EVIDENCE_ARCHIVE_ROOT", str(archive))
    repo = tmp_path / "some-repo"
    repo.mkdir()
    _git(repo, "init")
    fp = repo / "notes.txt"
    fp.write_text("mutable working-tree file\n")
    res = eo._capture_file_version(f"file://{fp}")
    assert res["ok"] is False


def test_sha_named_file_outside_archive_not_accepted(monkeypatch, tmp_path):
    # A file whose name validly matches its content hash but which lives OUTSIDE
    # the archive root must NOT be accepted via the relaxation (scope guard);
    # it falls through to the commit guard.
    content = b"pretend evidence\n"
    sha = hashlib.sha256(content).hexdigest()
    archive = tmp_path / "evidence-archive"
    archive.mkdir()
    monkeypatch.setattr(eo, "EVIDENCE_ARCHIVE_ROOT", str(archive))
    other = tmp_path / "elsewhere"
    other.mkdir()
    _git(other, "init")
    fp = other / f"{sha}.txt"
    fp.write_bytes(content)
    res = eo._capture_file_version(f"file://{fp}")
    assert res["ok"] is False


def test_symlink_in_archive_pointing_outside_rejected(archive_root, tmp_path):
    # Defense-in-depth (realpath, not abspath): a symlink planted INSIDE the
    # archive root that points to a file OUTSIDE it must be rejected by the
    # scope check, even though its name matches the target's content hash and
    # its lexical path is under the archive root. realpath resolves the link to
    # its real (outside) location, which fails commonpath. Without realpath
    # (abspath only), the lexical in-archive path would pass scope and accept.
    content = b"bytes that live outside the archive\n"
    sha = hashlib.sha256(content).hexdigest()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(content)
    bucket = archive_root / sha[:2]
    bucket.mkdir(parents=True, exist_ok=True)
    link = bucket / f"{sha}.txt"
    link.symlink_to(outside)
    assert eo._content_addressed_archive_hash(str(link)) == ""


def test_helper_returns_hash_only_for_valid_archive_file(archive_root):
    # Direct unit check of the helper's contract.
    content = b"unit-check bytes\n"
    fp, sha = _make_archive_file(archive_root, content)
    assert eo._content_addressed_archive_hash(str(fp)) == sha
    # Non-hex / short stem → "".
    bad = archive_root / "zz" / "not-a-hash.txt"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(content)
    assert eo._content_addressed_archive_hash(str(bad)) == ""


# ---------------------------------------------------------------------------
# #1255 — _capture_file_version status pathspec must be absolute
# ---------------------------------------------------------------------------

def _make_git_repo(path):
    """Init a git repo with a seed commit so HEAD exists."""
    _git(path, "init")
    _git(path, "config", "user.email", "t@t.t")
    _git(path, "config", "user.name", "t")
    seed = path / ".keep"
    seed.write_text("seed\n")
    _git(path, "add", ".keep")
    _git(path, "commit", "-q", "-m", "seed")


def test_subdir_modified_file_guard_fires(monkeypatch, tmp_path):
    """#1255 regression: modified file in a subdirectory must return ok=False.

    Before the fix, `git status --porcelain -- <repo-root-relative-rel-path>`
    was passed with cwd=file_dir, so git resolved the pathspec relative to
    file_dir — a sub-dir — and found nothing, silently returning ok=True.
    The fix passes abs_file instead, which git resolves correctly regardless
    of cwd.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    # Create and commit a file in a subdirectory
    subdir = repo / "subdir"
    subdir.mkdir()
    fp = subdir / "evidence.md"
    fp.write_text("original content\n")
    _git(repo, "add", str(fp))
    _git(repo, "commit", "-q", "-m", "add evidence")
    # Now modify it without committing
    fp.write_text("modified content\n")
    # No archive root involvement; wire a separate archive dir
    archive = tmp_path / "evidence-archive"
    archive.mkdir()
    monkeypatch.setattr(eo, "EVIDENCE_ARCHIVE_ROOT", str(archive))

    res = eo._capture_file_version(f"file://{fp}")
    assert res["ok"] is False, f"Expected guard to fire; got: {res}"
    assert "modified" in res["error"]


def test_root_level_modified_file_guard_still_fires(monkeypatch, tmp_path):
    """Regression check: root-level modified files still caught (no regression)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    fp = repo / "notes.txt"
    fp.write_text("original\n")
    _git(repo, "add", str(fp))
    _git(repo, "commit", "-q", "-m", "add notes")
    fp.write_text("modified\n")
    archive = tmp_path / "evidence-archive"
    archive.mkdir()
    monkeypatch.setattr(eo, "EVIDENCE_ARCHIVE_ROOT", str(archive))

    res = eo._capture_file_version(f"file://{fp}")
    assert res["ok"] is False, f"Expected guard to fire; got: {res}"


def test_subdir_clean_committed_file_accepted(monkeypatch, tmp_path):
    """Clean committed subdir file passes the guard (no regression on ok path)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)
    subdir = repo / "subdir"
    subdir.mkdir()
    fp = subdir / "clean.md"
    fp.write_text("content\n")
    _git(repo, "add", str(fp))
    _git(repo, "commit", "-q", "-m", "add clean")
    archive = tmp_path / "evidence-archive"
    archive.mkdir()
    monkeypatch.setattr(eo, "EVIDENCE_ARCHIVE_ROOT", str(archive))

    res = eo._capture_file_version(f"file://{fp}")
    assert res["ok"] is True, f"Expected ok; got: {res}"


# ---------------------------------------------------------------------------
# #1256 — _verify_quote_in_source rejects relative file:// paths
# ---------------------------------------------------------------------------

def test_verify_quote_rejects_relative_file_url():
    """#1256: relative file:// path returns the must-be-absolute error."""
    result = eo._verify_quote_in_source("file://./rel/x.md", "some text")
    assert result is not None
    assert "must be an absolute path" in result
    assert "./rel/x.md" in result


def test_verify_quote_rejects_relative_no_dot_prefix():
    """#1256: relative path without leading dot is also rejected."""
    result = eo._verify_quote_in_source("file://rel/path/x.md", "some text")
    assert result is not None
    assert "must be an absolute path" in result


def test_verify_quote_absolute_nonexistent_returns_none():
    """#1256: absolute file:// with non-existent file returns None (file existence
    is checked separately — the relative-path guard does not intercept this path)."""
    result = eo._verify_quote_in_source("file:///nonexistent/absolute/x.md", "text")
    assert result is None


def test_verify_quote_absolute_existing_file_verifies(tmp_path):
    """#1256: absolute file:// with real content still verifies normally."""
    fp = tmp_path / "source.md"
    fp.write_text("The quick brown fox\n")
    result = eo._verify_quote_in_source(f"file://{fp}", "quick brown fox")
    assert result is None  # found → no error


# ---------------------------------------------------------------------------
# #1256 round-2 — _capture_file_version rejects relative file:// paths
# ---------------------------------------------------------------------------

def test_capture_file_version_rejects_relative_dotslash(monkeypatch, tmp_path):
    """Relative file://./rel/x.md must return ok=False with the absolute-path error."""
    # Wire a harmless archive root so we don't accidentally exercise archive logic
    archive = tmp_path / "evidence-archive"
    archive.mkdir()
    monkeypatch.setattr(eo, "EVIDENCE_ARCHIVE_ROOT", str(archive))

    res = eo._capture_file_version("file://./rel/x.md")
    assert res["ok"] is False, f"Expected guard to fire; got: {res}"
    assert "must be an absolute path" in res["error"]
    assert "./rel/x.md" in res["error"]
    assert res["content_hash"] == ""
    assert res["git_sha"] == ""


def test_capture_file_version_rejects_relative_no_dot(monkeypatch, tmp_path):
    """Relative file://rel.md (no leading dot) must also return ok=False."""
    archive = tmp_path / "evidence-archive"
    archive.mkdir()
    monkeypatch.setattr(eo, "EVIDENCE_ARCHIVE_ROOT", str(archive))

    res = eo._capture_file_version("file://rel.md")
    assert res["ok"] is False, f"Expected guard to fire; got: {res}"
    assert "must be an absolute path" in res["error"]


def test_capture_file_version_absolute_path_not_blocked(monkeypatch, tmp_path):
    """Absolute file:// path still passes the relative-path guard (no regression).

    Uses an existing clean committed file from the test fixture so the overall
    function can reach ok=True — confirming the guard doesn't intercept it.
    """
    # Re-use a git-backed archive file (absolute path, content-addressed) so
    # _capture_file_version returns ok=True end-to-end.
    archive = tmp_path / "evidence-archive"
    archive.mkdir()
    _git(archive, "init")
    _git(archive, "config", "user.email", "t@t.t")
    _git(archive, "config", "user.name", "t")
    (archive / ".keep").write_text("seed\n")
    _git(archive, "add", ".keep")
    _git(archive, "commit", "-q", "-m", "seed")
    monkeypatch.setattr(eo, "EVIDENCE_ARCHIVE_ROOT", str(archive))

    import hashlib
    content = b"absolute path regression check\n"
    sha = hashlib.sha256(content).hexdigest()
    bucket = archive / sha[:2]
    bucket.mkdir(parents=True, exist_ok=True)
    fp = bucket / f"{sha}.txt"
    fp.write_bytes(content)

    res = eo._capture_file_version(f"file://{fp}")
    assert res["ok"] is True, f"Absolute path should not be blocked; got: {res}"
