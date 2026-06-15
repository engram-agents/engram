#!/usr/bin/env python3
"""check-claude-md-drift — rendered CLAUDE.md drift detector.

Compares the RENDERED CLAUDE.md output between two git refs by folding
compact-instructions.md into template.CLAUDE.md for each ref, then diffing
the results.  Unlike diffing the raw template files, this catches changes to
ANY source that contributes to the rendered CLAUDE.md (e.g. a
compact-instructions.md-only change is invisible to a template-only diff).

Usage
-----
    python tools/check-claude-md-drift.py --repo <SRC_DIR> --base <BASE_REF> --head <HEAD_REF>

Arguments
---------
--repo    Path to the engram-alpha source tree (used for git show calls).
--base    Git ref for the "before" state (e.g. the commit before a PR merge).
--head    Git ref for the "after" state (e.g. origin/dev, HEAD, a branch name).

Exit codes
----------
0   No drift — the two refs render identically.
1   Drift detected — a unified diff is printed to stdout.
2   Usage / argument error.

Source files checked
--------------------
The set of files folded into the render is declared in
``src/engram/template_render.CLAUDE_RENDER_SOURCES`` (canonical SSoT).  A
source file absent at the base ref is treated as empty (so an extraction
commit correctly shows the extracted content as drift rather than an error).

Design constraints
------------------
* stdlib-only: subprocess (git), difflib (diff), tempfile, shutil.
* No import of bootstrap.py (side-effects at module level — reads env vars,
  imports server.py which reads ENGRAM_HOME).  Imports template_render only
  (side-effect-free).
* Follows the verb-first, exit-code-driven style of tools/baton.py and
  tools/forum.py.
"""

from __future__ import annotations

import argparse
import difflib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — import template_render from src/engram/ (side-effect-free).
#
# This script lives in src/engram/tools/ (with a root-level tools/ symlink).
# __file__ resolves to the canonical src/engram/tools/check-claude-md-drift.py
# path, so:
#   parent     = src/engram/tools/
#   parent.parent = src/engram/          ← where template_render.py lives
# ---------------------------------------------------------------------------

_TOOLS_DIR = Path(__file__).resolve().parent          # src/engram/tools/
_SRC_ENGRAM = _TOOLS_DIR.parent                       # src/engram/

sys.path.insert(0, str(_SRC_ENGRAM))
from template_render import CLAUDE_RENDER_SOURCES, render_identity_surface  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_show(repo: Path, ref: str, rel_path: str) -> str | None:
    """Return the file content at ``ref:src/engram/templates/<rel_path>``.

    Returns None if the file does not exist at that ref (e.g. before the
    compact-instructions.md extraction commit).  Raises SystemExit on
    unexpected git errors.
    """
    git_path = f"src/engram/templates/{rel_path}"
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{git_path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout
    # Exit code 128 with "does not exist" or path-related message = file absent
    # at that ref.  Any other error is unexpected and should surface.
    stderr_lower = result.stderr.lower()
    if (
        "does not exist" in stderr_lower
        or "exists on disk" in stderr_lower
        or "path not found" in stderr_lower
    ):
        return None
    # Unknown git error — surface it on stderr and exit with a DISTINCT code (3),
    # so an upgrading agent / CI never mistakes a git-plumbing failure for a real
    # drift signal (drift is exit 1; no-drift is exit 0).
    print(
        f"git show failed for {ref}:{git_path}\n"
        f"  returncode: {result.returncode}\n"
        f"  stderr: {result.stderr.strip()}",
        file=sys.stderr,
    )
    raise SystemExit(3)


def _render_ref(repo: Path, ref: str) -> str:
    """Render the CLAUDE.md identity surface for a git ref.

    Uses a temp directory populated with the source files from the ref.
    A source file absent at that ref is written as an empty file so
    render_identity_surface() can still be called with a consistent dir.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="engram_drift_"))
    try:
        for src_file in CLAUDE_RENDER_SOURCES:
            content = _git_show(repo, ref, src_file)
            dest = tmpdir / src_file
            dest.write_text(content if content is not None else "", encoding="utf-8")
        return render_identity_surface(tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Entry point.  Returns an exit code (0 = no drift, 1 = drift)."""
    parser = argparse.ArgumentParser(
        prog="check-claude-md-drift",
        description=(
            "Compare the RENDERED CLAUDE.md between two git refs. "
            "Exits 0 if identical, 1 if drift detected."
        ),
    )
    parser.add_argument(
        "--repo",
        required=True,
        metavar="SRC_DIR",
        help="Path to the engram-alpha source tree.",
    )
    parser.add_argument(
        "--base",
        required=True,
        metavar="BASE_REF",
        help="Git ref for the 'before' state.",
    )
    parser.add_argument(
        "--head",
        required=True,
        metavar="HEAD_REF",
        help="Git ref for the 'after' state.",
    )
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        parser.error(f"--repo {repo} does not look like a git repository (no .git dir)")

    base_ref = args.base
    head_ref = args.head

    # Render both refs.
    base_rendered = _render_ref(repo, base_ref)
    head_rendered = _render_ref(repo, head_ref)

    if base_rendered == head_rendered:
        return 0

    # Produce a unified diff.
    diff_lines = list(
        difflib.unified_diff(
            base_rendered.splitlines(keepends=True),
            head_rendered.splitlines(keepends=True),
            fromfile=f"CLAUDE.md @ {base_ref}",
            tofile=f"CLAUDE.md @ {head_ref}",
        )
    )
    sys.stdout.writelines(diff_lines)
    return 1


if __name__ == "__main__":
    sys.exit(main())
