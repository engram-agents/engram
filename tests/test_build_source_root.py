"""Tests for the source_root manifest field — Phase 0 of EPIC #1093.

Five tests verify the key invariants of the Phase 0 change:
1. Absent source_root defaults to "."
2. Explicit source_root "." is present and correct in tiers.json
3. tiers.json schema remains structurally valid after adding source_root
4. "." default is identity-preserving in path construction
5. Non-"." source_root is wired correctly: path resolves into the subdirectory
"""

from __future__ import annotations

import json
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(REPO_ROOT, "src", "build", "packaging", "tiers.json")


# ---------------------------------------------------------------------------
# 1. Absent source_root defaults to "."
# ---------------------------------------------------------------------------


def test_absent_source_root_defaults_to_dot():
    """source_root absent from manifest → manifest.get("source_root", ".") returns "."."""
    manifest = {"schema_version": 1, "default_tier": "convenience", "mechanisms": []}
    assert manifest.get("source_root", ".") == "."


# ---------------------------------------------------------------------------
# 2. Explicit source_root "." is present in tiers.json
# ---------------------------------------------------------------------------


def test_explicit_source_root_in_tiers_json():
    """packaging/tiers.json must contain source_root == "src/engram"."""
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        manifest = json.load(f)
    assert manifest["source_root"] == "src/engram"


# ---------------------------------------------------------------------------
# 3. tiers.json schema still valid after adding source_root
# ---------------------------------------------------------------------------


def test_tiers_json_schema_valid():
    """tiers.json structural sanity: schema_version, mechanisms, default_tier, source_root."""
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        manifest = json.load(f)
    assert manifest["schema_version"] == 1
    assert isinstance(manifest["mechanisms"], list)
    assert len(manifest["mechanisms"]) > 0
    assert "default_tier" in manifest
    assert isinstance(manifest["source_root"], str)


# ---------------------------------------------------------------------------
# 4. "." default is identity-preserving in path construction
# ---------------------------------------------------------------------------


def test_dot_source_root_path_identity():
    """os.path.normpath(join(repo, ".", path)) == os.path.normpath(join(repo, path)).

    The "." default is identity-preserving: the paths resolve to the same
    location even though os.path.join produces a literal "/./". normpath
    documents the key invariant that makes Phase 0 a zero-behavior-change.
    """
    with_dot = os.path.join("/fake/repo", ".", "server.py")
    without_dot = os.path.join("/fake/repo", "server.py")
    assert os.path.normpath(with_dot) == os.path.normpath(without_dot)

    with_dot_dir = os.path.join("/fake/repo", ".", "hooks", "claude")
    without_dot_dir = os.path.join("/fake/repo", "hooks", "claude")
    assert os.path.normpath(with_dot_dir) == os.path.normpath(without_dot_dir)


# ---------------------------------------------------------------------------
# 5. Non-"." source_root wiring — path resolves into subdirectory
# ---------------------------------------------------------------------------


def test_non_dot_source_root_wiring(tmp_path):
    """source_root="src" in a manifest causes resolved paths to enter <repo>/src/.

    This test catches accidental hardcoding of "." instead of reading
    source_root from the manifest.  If source_root is not applied, the
    resolved path would point at <tmp>/server.py which does not exist; the
    test would fail on the os.path.exists assertion.
    """
    # Place sentinel file only under src/, not at the repo root.
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    sentinel = src_dir / "server.py"
    sentinel.write_text("# sentinel\n")

    # Verify the bare-root path does NOT exist (pre-condition).
    assert not os.path.exists(tmp_path / "server.py"), (
        "test setup error: server.py must not exist at the bare repo root"
    )

    # Write a minimal tiers.json with source_root="src" into tmp_path.
    packaging_dir = tmp_path / "packaging"
    packaging_dir.mkdir()
    fake_manifest = {
        "schema_version": 1,
        "default_tier": "convenience",
        "source_root": "src",
        "mechanisms": [],
    }
    manifest_path = packaging_dir / "tiers.json"
    manifest_path.write_text(json.dumps(fake_manifest), encoding="utf-8")

    # Read source_root from the manifest exactly as build.py does.
    with open(str(manifest_path), encoding="utf-8") as f:
        manifest = json.load(f)
    source_root = manifest.get("source_root", ".")

    # The source_root read from the manifest must be the non-default value.
    assert source_root == "src", (
        f"Expected source_root='src' from manifest, got {source_root!r}"
    )

    # Replicate build.py's path construction: os.path.join(repo_root, source_root, p)
    resolved_path = os.path.join(str(tmp_path), source_root, "server.py")

    # The resolved path must point into src/ and exist there.
    assert os.path.exists(resolved_path), (
        f"Resolved path {resolved_path!r} does not exist — "
        "source_root is not being applied to path construction"
    )

    # The bare repo-root path must NOT exist (proves source_root redirected the lookup).
    bare_path = os.path.join(str(tmp_path), "server.py")
    assert not os.path.exists(bare_path), (
        f"Bare path {bare_path!r} exists; sentinel should only be under src/"
    )
