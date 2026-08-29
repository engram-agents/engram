#!/usr/bin/env python3
"""stray_baton_path_check.py — warn-only scanner for the stale pre-2026-06-27
baton/project directory path (#2316).

`/home/agents-shared/projects` (no `/forum/` segment) was the baton-store
location before the 2026-06-27 UCS cutover, when it moved to
`/home/agents-shared/forum/projects`. The old directory was never removed —
it sits frozen and populated (502 files as of #2316's filing), so a stale
reference to it does not fail loudly: it silently resolves to a real-looking
but incomplete/wrong directory. #2316 found 13 files still carrying the
stale path, three of them live runtime defaults (not merely documentation),
one of which (`pre-dispatch-check.sh`) was measured to miss 56% of real
in-flight batons as a result.

This scanner exists so that fix does not silently regress: a future PR that
copy-pastes an old doc snippet, or hand-writes a new default before checking
this file, gets caught here rather than by another multi-week silent-miss
investigation.

**Advisory, never blocks** — same posture as `name_leak_warn.py` and
`stray_master_ref_check.py`. A small number of references to the stale path
are legitimate and deliberate: `baton.py`'s own migration note, and the two
comments this fix itself added explaining WHY the runtime defaults changed.
Those are allowlisted by (relative path, line substring) below rather than
excluded by directory, since — unlike the master->main rename this script's
sibling covers — there is no single "prose is fine, code is not" split here:
the same file (`pre-dispatch-check.sh`) legitimately carries both a stale
reference (in a comment, explaining the fix) and would illegitimately carry
one (in the actual default value).

Usage:
    python3 tools/stray_baton_path_check.py [--repo-root PATH] [--format text|json]

Exit codes:
    0 — scan completed (matches found or not — this is warn-only)
    1 — usage/IO error
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

STALE_PATH_RE = re.compile(r"/home/agents-shared/projects\b")

# (relative path, line-content substring) — deliberate historical notes,
# each explaining the stale path rather than reintroducing it as a live
# default. Keep this list exact and short; a broad allowlist here would
# defeat the whole point of the scanner.
ALLOWLIST = [
    ("src/engram/tools/baton.py", "Note: the old path /home/agents-shared/projects/"),
    ("src/engram/tools/pre-dispatch-check.sh", "#2316: the pre-2026-06-27 path (/home/agents-shared/projects"),
    ("src/engram/tools/_status_derive.py", "(``/home/agents-shared/projects``, no ``/forum/`` segment)"),
]

# This scanner's OWN source and its own test fixture file are excluded
# wholesale, not line-allowlisted -- reviewer-fairy finding on this PR's
# own first cut: once these two files were `git add`ed (tracked), the
# scanner started reporting itself as 10 matches (its module docstring,
# the STALE_PATH_RE literal, every ALLOWLIST tuple, and every synthetic
# stale-path string the test fixtures deliberately write). They were
# NEVER in scope during local pre-commit verification, because `git
# ls-files` only sees tracked files and these two were still untracked
# at that point -- a hygiene test that only ever ran against its own
# pre-tracked state gave false assurance. Both files inherently need the
# literal string throughout (as regex source, as documentation, and as
# test data), so excluding them by path is correct, not a workaround --
# unlike ALLOWLIST's targeted per-line suppression, which exists for
# files that are legitimate MOST of the time and stale-by-omission the
# rest.
_SELF_EXCLUDED_PATHS = frozenset(
    {"tools/stray_baton_path_check.py", "tests/test_stray_baton_path_check.py"}
)


def _is_allowlisted(rel_path: str, line: str) -> bool:
    return any(rel_path == p and frag in line for p, frag in ALLOWLIST)


def find_target_files(repo_root: Path) -> list[Path]:
    """Every TRACKED regular file in the repo, sorted for deterministic
    output. Whole-tree by design — docs, skills, and tests are exactly
    where #2316's stale references were found; scoping this to a
    "load-bearing surfaces only" subset (stray_master_ref_check.py's
    approach) would have missed most of what #2316 actually found.

    **Deliberately `git ls-files`, not a raw filesystem walk** (an earlier
    draft used `Path.rglob` with a hand-maintained excluded-dir-name list —
    caught by this module's own test suite, not reasoned past: a raw walk
    picks up `.claude/worktrees/*` (live fairy-dispatch checkouts, each a
    full repo clone that can carry an UNMERGED pre-fix copy of any file
    this scanner cares about) and `.claude/agent-scratch/*` (session
    artifacts), both gitignored but very much present on disk during normal
    work. Scanning untracked/gitignored content would make this scanner's
    result depend on which fairies happen to be checked out on the runner
    at scan time — nondeterministic and meaningless for a repo-content
    check. `git ls-files` is exactly "what actually ships," matching every
    other scrub in this repo's own pr-checks job."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        capture_output=True,
        check=True,
    )
    names = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    # Skip tracked SYMLINK entries (e.g. this repo's own tools/*.py ->
    # ../src/engram/tools/*.py) -- git tracks a symlink as its own path,
    # separately from the real file it points at, and Path.is_file()/
    # read_text() both transparently follow it to the SAME content. Without
    # this, every symlinked file would be scanned twice under two different
    # reported paths, and the allowlist below would need a duplicate entry
    # per symlink alias to stay correct -- caught by this module's own test
    # suite, not reasoned past.
    return sorted(
        (repo_root / name)
        for name in names
        if name
        and name not in _SELF_EXCLUDED_PATHS
        and not (repo_root / name).is_symlink()
        and (repo_root / name).is_file()
    )


def scan_file(path: Path, repo_root: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        # Binary or unreadable file — not a text surface this check covers.
        return []

    rel = str(path.relative_to(repo_root))
    matches = []
    for i, raw_line in enumerate(text.splitlines()):
        if STALE_PATH_RE.search(raw_line) and not _is_allowlisted(rel, raw_line):
            matches.append({"file": rel, "line_number": i + 1, "line": raw_line.strip()})
    return matches


def format_text(matches: list[dict]) -> str:
    if not matches:
        return "No stray pre-cutover baton-path references found. ✓"
    lines = [f"{len(matches)} stray pre-cutover baton-path reference(s) found (see #2316):"]
    for m in matches:
        lines.append(f"  {m['file']}:{m['line_number']}: {m['line']}")
    return "\n".join(lines)


def format_json(matches: list[dict]) -> str:
    return json.dumps({"matches": matches}, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Warn-only scanner for the stale pre-cutover baton path (#2316)."
    )
    parser.add_argument(
        "--repo-root",
        metavar="PATH",
        default=".",
        help="Repository root to scan from (default: current directory).",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text).",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    if not repo_root.is_dir():
        print(f"stray_baton_path_check: error — {repo_root} is not a directory", file=sys.stderr)
        sys.exit(1)

    matches = []
    for f in find_target_files(repo_root):
        matches.extend(scan_file(f, repo_root))

    if args.format == "json":
        print(format_json(matches))
    else:
        print(format_text(matches))

    # Always exit 0 on scan completion (warn-only, same posture as
    # name_leak_warn.py / stray_master_ref_check.py).
    sys.exit(0)


if __name__ == "__main__":
    main()
