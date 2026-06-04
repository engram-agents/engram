"""Golden equivalence tests for tools.engine vs tools/build-plugin-legacy.sh.

For four build configurations:
  (1) essential
  (2) convenience (default)
  (3) convenience + multi-agent
  (4) dev + multi-agent

Runs the LEGACY script (tools/build-plugin-legacy.sh) and the ENGINE CLI
(python3 -m tools.engine.cli build) into separate temp directories, then
performs a recursive comparison of:
  - File sets (relative paths)
  - File contents (bytes)
  - File exec bits (``stat.st_mode & 0o111``)
  - Directory sets (catches empty dirs created by one side and not the other)

Normalization rationale
-----------------------
The only documented nondeterminism is in .engram-build-manifest.json's
``shipped_paths`` list.  The legacy bash script accumulates paths in the order
they are copied, where the skills/claude directory order is determined by bash
glob expansion (for skill_dir in skills/claude/*/), which uses the filesystem's
readdir/hash-tree ordering — not alphabetical Python sorted() order.  On this
machine, for example, bash glob puts ``engram-loop-diagnose`` before
``engram-loop`` (filesystem inode-hash artifact), while Python sorted() gives
the reverse.  This is reproducibly different on the same machine but could
differ on a different machine — so it is nondeterministic across environments.

We normalize .engram-build-manifest.json's shipped_paths by sorting before
comparison, then re-serialize both sides with json.dumps(indent=2).  All other
fields (tier, multi_agent) are compared semantically (re-serialized) via the
same json.dumps round-trip, which masks whitespace-only drift in that one file
by design.

Any normalization beyond this is NOT applied — if a difference is found in
file contents, exec bits, or filenames outside .engram-build-manifest.json, the
test fails and names the differing path.

Deliberate blind spots
----------------------
The following are intentionally NOT compared:
  - mtimes / atimes (build timestamps are inherently non-reproducible)
  - file ownership / uid / gid (not meaningful across build environments)
  - symlinks (neither builder creates any)

Failure reporting
-----------------
Failures name the differing path(s) explicitly, not just the count.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEGACY_SCRIPT = os.path.join(REPO_ROOT, "tools", "build-plugin-legacy.sh")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run_legacy(args: list[str], tmpdir: str) -> None:
    """Run legacy script into REPO_ROOT/build/plugin, then copy tree to tmpdir."""
    cmd = ["bash", LEGACY_SCRIPT] + args
    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"build-plugin-legacy.sh {args} exited {result.returncode}:\n{result.stderr}"
    )
    # Legacy always writes to build/plugin; copy result to tmpdir
    src = os.path.join(REPO_ROOT, "build", "plugin")
    if os.path.exists(tmpdir):
        shutil.rmtree(tmpdir)
    shutil.copytree(src, tmpdir)


def _run_engine(args: list[str], output_dir: str) -> None:
    """Run engine CLI with --output output_dir."""
    cmd = [sys.executable, "-m", "tools.engine.cli", "build"] + args + [
        "--output", output_dir
    ]
    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"tools.engine.cli build {args} exited {result.returncode}:\n{result.stderr}"
    )


def _collect_tree(root: str) -> dict[str, tuple[bytes, bool]]:
    """Recursively collect all files in root → {rel_path: (content_bytes, is_exec)}.

    ``is_exec`` is True when any execute bit is set (``stat.st_mode & 0o111``).
    Capturing the exec bit means an exec-bit regression (e.g. losing the +x on
    launch-engram-server.sh, which is load-bearing for MCP startup) will fail
    the comparison rather than silently passing.
    """
    result: dict[str, tuple[bytes, bool]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root)
            mode = os.stat(full).st_mode
            is_exec = bool(mode & 0o111)
            with open(full, "rb") as f:
                result[rel] = (f.read(), is_exec)
    return result


def _collect_dirs(root: str) -> set[str]:
    """Recursively collect all directory relative paths under root.

    Includes root-relative paths for every subdirectory so that a directory
    created by one builder but absent in the other (even when it contains no
    files) shows up as a set-difference failure.
    """
    dirs: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel != ".":
            dirs.add(rel)
    return dirs


def _normalize_build_manifest(content: bytes) -> bytes:
    """Normalize .engram-build-manifest.json for comparison.

    Sorts ``shipped_paths`` before comparison.  All other fields are compared
    semantically (re-serialized) — both sides go through the same
    json.loads → sort → json.dumps(indent=2) round-trip, which masks
    whitespace-only drift in this one file by design.

    JUSTIFICATION: The legacy bash script accumulates shipped_paths in the order
    files are copied.  For skills/claude entries, the copy order follows bash
    glob expansion (``for skill_dir in skills/claude/*/``), which reflects the
    filesystem's hash-tree directory entry ordering — not lexicographic order.
    On this machine, bash glob gives ``engram-loop-diagnose`` before
    ``engram-loop`` while Python sorted() gives the reverse.  This ordering
    varies by filesystem and is therefore nondeterministic across machines.
    We normalize by sorting shipped_paths so the comparison is content-based
    (what ships) rather than order-based (what order was it accumulated in).
    No other normalization is applied.
    """
    data = json.loads(content.decode("utf-8"))
    data["shipped_paths"] = sorted(data.get("shipped_paths", []))
    # Re-serialize with indent=2 + trailing newline to match both tools' format
    return (json.dumps(data, indent=2) + "\n").encode("utf-8")


BUILD_MANIFEST_FILENAME = ".engram-build-manifest.json"


def _compare_trees(
    legacy_root: str,
    engine_root: str,
    config_label: str,
) -> None:
    """Assert legacy and engine output trees are equivalent.

    Checks:
      1. File sets match (no files only in one side).
      2. File contents match (after manifest normalization).
      3. Exec bits match per file (``stat.st_mode & 0o111``).
      4. Directory sets match (catches empty dirs created by one side only).

    Normalizes .engram-build-manifest.json's shipped_paths (see module docstring).
    All other files are compared verbatim, byte for byte and bit for bit.

    Raises AssertionError naming all differing paths on failure.
    """
    legacy_tree = _collect_tree(legacy_root)
    engine_tree = _collect_tree(engine_root)

    # Apply normalization to build manifests (content only; exec bit unaffected)
    def _normalize(tree: dict[str, tuple[bytes, bool]]) -> dict[str, tuple[bytes, bool]]:
        result = {}
        for rel, (content, is_exec) in tree.items():
            if os.path.basename(rel) == BUILD_MANIFEST_FILENAME:
                result[rel] = (_normalize_build_manifest(content), is_exec)
            else:
                result[rel] = (content, is_exec)
        return result

    legacy_normalized = _normalize(legacy_tree)
    engine_normalized = _normalize(engine_tree)

    legacy_paths = set(legacy_normalized.keys())
    engine_paths = set(engine_normalized.keys())

    only_in_legacy = sorted(legacy_paths - engine_paths)
    only_in_engine = sorted(engine_paths - legacy_paths)

    content_diffs = []
    exec_diffs = []
    for p in sorted(legacy_paths & engine_paths):
        l_content, l_exec = legacy_normalized[p]
        e_content, e_exec = engine_normalized[p]
        if l_content != e_content:
            content_diffs.append(p)
        if l_exec != e_exec:
            exec_diffs.append(
                f"{p}  (legacy exec={l_exec}, engine exec={e_exec})"
            )

    # Directory-set comparison (catches empty dirs created by one side only)
    legacy_dirs = _collect_dirs(legacy_root)
    engine_dirs = _collect_dirs(engine_root)
    only_dirs_in_legacy = sorted(legacy_dirs - engine_dirs)
    only_dirs_in_engine = sorted(engine_dirs - legacy_dirs)

    errors: list[str] = []
    if only_in_legacy:
        errors.append(
            "  Files only in legacy output:\n"
            + "\n".join(f"    {p}" for p in only_in_legacy)
        )
    if only_in_engine:
        errors.append(
            "  Files only in engine output:\n"
            + "\n".join(f"    {p}" for p in only_in_engine)
        )
    if content_diffs:
        errors.append(
            "  Files with content differences:\n"
            + "\n".join(f"    {p}" for p in content_diffs)
        )
    if exec_diffs:
        errors.append(
            "  Files with exec-bit differences:\n"
            + "\n".join(f"    {p}" for p in exec_diffs)
        )
    if only_dirs_in_legacy:
        errors.append(
            "  Directories only in legacy output:\n"
            + "\n".join(f"    {p}" for p in only_dirs_in_legacy)
        )
    if only_dirs_in_engine:
        errors.append(
            "  Directories only in engine output:\n"
            + "\n".join(f"    {p}" for p in only_dirs_in_engine)
        )

    assert not errors, (
        f"Golden equivalence FAILED for config '{config_label}':\n"
        + "\n".join(errors)
    )


# ---------------------------------------------------------------------------
# test fixtures
# ---------------------------------------------------------------------------

# Parametrize over four configs
CONFIGS = [
    pytest.param(
        {"label": "essential", "legacy_args": ["--tier", "essential"], "engine_args": ["--tier", "essential"]},
        id="essential",
    ),
    pytest.param(
        {"label": "convenience", "legacy_args": [], "engine_args": []},
        id="convenience",
    ),
    pytest.param(
        {"label": "convenience+MA", "legacy_args": ["--multi-agent"], "engine_args": ["--multi-agent"]},
        id="convenience_ma",
    ),
    pytest.param(
        {"label": "dev+MA", "legacy_args": ["--tier", "dev", "--multi-agent"], "engine_args": ["--tier", "dev", "--multi-agent"]},
        id="dev_ma",
    ),
]


class TestGoldenEquivalence:
    """Golden equivalence: engine output matches legacy script output for all configs."""

    @pytest.mark.parametrize("config", CONFIGS)
    def test_golden_equivalence(self, config, tmp_path):
        """Engine and legacy script produce equivalent output trees.

        Runs legacy script and engine into separate temp dirs.
        Compares recursively: file sets, file contents, exec bits, and
        directory sets.  Normalizes only .engram-build-manifest.json
        shipped_paths ordering (documented nondeterminism — see module
        docstring).
        """
        label = config["label"]
        legacy_args = config["legacy_args"]
        engine_args = config["engine_args"]

        legacy_dir = str(tmp_path / "legacy")
        engine_dir = str(tmp_path / "engine")

        # Run legacy first, then engine (legacy writes to build/plugin; we copy)
        _run_legacy(legacy_args, legacy_dir)
        _run_engine(engine_args, engine_dir)

        _compare_trees(legacy_dir, engine_dir, label)
