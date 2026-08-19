#!/usr/bin/env python3
"""stray_master_ref_check.py — warn-only scanner for load-bearing 'master'
branch references left behind by the 2026-07-03 master->main rename (#1645, #1884).

A rename's fan-out is invisible in the renaming PR's own diff: three separate
stray `master` references survived five weeks in live workflow/ops surfaces,
each found by accident rather than by tooling. One of them (a workflow
`branches:` filter) failed *silently* — the workflow existed, looked armed,
and simply had no event to hear on the release branch.

Deliberately scoped to surfaces where the name is load-bearing, not free
prose — docs/** and CHANGELOG.md commonly (and correctly) narrate `master` as
history (e.g. docs/RELEASING.md's account of the 2026-07-02 dev->master
reconciliation). A naive repo-wide grep would drown the real signal in that
legitimate noise, which is how the third stray instance survived unnoticed.

Scanned surfaces:
  - .github/workflows/*.yml   — `branches:`/`base:` filter values, `origin/master`
  - src/engram/tools/**/*.sh  — `git checkout/fetch/push ... master`, `origin/master`
  - src/forum/deploy/**/*.sh  — same as above

Usage:
    python3 tools/stray_master_ref_check.py [--repo-root PATH] [--format text|json]

Exit codes:
    0 — scan completed (matches found or not — this is warn-only, same
        posture as name_leak_warn.py)
    1 — usage/IO error
"""

import argparse
import json
import re
import sys
from pathlib import Path

WORKFLOW_GLOB_DIRS = [".github/workflows"]
SHELL_GLOB_DIRS = ["src/engram/tools", "src/forum/deploy"]

BRANCH_KEY_RE = re.compile(r"^(\s*)(branches|base)\s*:\s*(.*)$")
LIST_ITEM_RE = re.compile(r"^(\s*)-\s*(.+?)\s*$")
MASTER_WORD_RE = re.compile(r"\bmaster\b")
ORIGIN_MASTER_RE = re.compile(r"\borigin/master\b")
GIT_CMD_MASTER_RE = re.compile(r"\bgit\s+(?:checkout|fetch|push)\b[^\n]*\bmaster\b")
# Note: GIT_CMD_MASTER_RE matches within a single line. A `git checkout \`
# / `  master` split across a shell line-continuation is not detected —
# advisory-only, so a false negative here costs a missed warning rather
# than a wrong one; not currently observed in this repo's ops scripts.


def find_target_files(repo_root: Path) -> list[Path]:
    """Return every file in scope, sorted for deterministic output."""
    files: list[Path] = []
    for rel_dir in WORKFLOW_GLOB_DIRS:
        files.extend((repo_root / rel_dir).glob("*.yml"))
        files.extend((repo_root / rel_dir).glob("*.yaml"))
    for rel_dir in SHELL_GLOB_DIRS:
        d = repo_root / rel_dir
        if d.is_dir():
            files.extend(d.rglob("*.sh"))
    return sorted(set(f for f in files if f.is_file()))


def scan_workflow_yaml(text: str) -> list[dict]:
    """Scan a GitHub Actions workflow file for load-bearing `master` refs.

    Catches both the inline form (`branches: [dev, master]`, `base: master`)
    and the block-list form (`branches:` followed by `- master` at a deeper
    indent), plus any bare `origin/master` mention anywhere in the file.
    """
    matches = []
    lines = text.splitlines()
    block_key = None  # (key_name, key_indent) while inside a branches:/base: list
    for i, raw_line in enumerate(lines):
        line_no = i + 1

        if ORIGIN_MASTER_RE.search(raw_line):
            matches.append({"line_number": line_no, "line": raw_line, "reason": "origin/master reference"})

        key_m = BRANCH_KEY_RE.match(raw_line)
        if key_m:
            indent, key_name, inline_value = key_m.groups()
            if inline_value:
                if MASTER_WORD_RE.search(inline_value):
                    matches.append({
                        "line_number": line_no, "line": raw_line,
                        "reason": f"{key_name}: filter value contains master",
                    })
                block_key = None
            else:
                block_key = (key_name, len(indent))
            continue

        if block_key is not None:
            key_name, key_indent = block_key
            item_m = LIST_ITEM_RE.match(raw_line)
            if item_m and len(item_m.group(1)) > key_indent:
                if MASTER_WORD_RE.search(item_m.group(2)):
                    matches.append({
                        "line_number": line_no, "line": raw_line,
                        "reason": f"{key_name}: list item is master",
                    })
                continue
            stripped = raw_line.strip()
            if stripped == "":
                continue  # Blank line inside the list doesn't end the block.
            if stripped.startswith("#"):
                # A comment nested deeper than the key is interleaved between
                # list items (a common, valid YAML idiom) and must not end
                # the block — otherwise list items after it go unscanned,
                # which is exactly the silent-miss failure class #1884 exists
                # to catch. A comment at or above the key's own indent (e.g.
                # a comment introducing the next key) genuinely ends it.
                line_indent = len(raw_line) - len(raw_line.lstrip(" "))
                if line_indent > key_indent:
                    continue
            # Genuine dedent or other content ends the block.
            block_key = None

    return matches


def scan_shell_script(text: str) -> list[dict]:
    """Scan an ops shell script for git commands targeting the old branch name."""
    matches = []
    for i, raw_line in enumerate(text.splitlines()):
        line_no = i + 1
        if ORIGIN_MASTER_RE.search(raw_line):
            matches.append({"line_number": line_no, "line": raw_line, "reason": "origin/master reference"})
        elif GIT_CMD_MASTER_RE.search(raw_line):
            matches.append({"line_number": line_no, "line": raw_line, "reason": "git checkout/fetch/push targets master"})
    return matches


def scan_file(path: Path, repo_root: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"stray_master_ref_check: warning — could not read {path}: {exc}", file=sys.stderr)
        return []

    if path.suffix in (".yml", ".yaml"):
        raw_matches = scan_workflow_yaml(text)
    else:
        raw_matches = scan_shell_script(text)

    rel = str(path.relative_to(repo_root))
    for m in raw_matches:
        m["file"] = rel
    return raw_matches


def format_text(matches: list[dict]) -> str:
    if not matches:
        return "No stray 'master' references found in scanned surfaces. ✓"
    lines = [f"{len(matches)} stray 'master' reference(s) found:"]
    for m in matches:
        lines.append(f"  {m['file']}:{m['line_number']}: [{m['reason']}] {m['line'].strip()}")
    return "\n".join(lines)


def format_json(matches: list[dict]) -> str:
    return json.dumps({"matches": matches}, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="Warn-only scanner for stray 'master' branch references (#1884)."
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
        print(f"stray_master_ref_check: error — {repo_root} is not a directory", file=sys.stderr)
        sys.exit(1)

    matches = []
    for f in find_target_files(repo_root):
        matches.extend(scan_file(f, repo_root))

    if args.format == "json":
        print(format_json(matches))
    else:
        print(format_text(matches))

    # Always exit 0 on scan completion (warn-only, same posture as name-leak-warn).
    sys.exit(0)


if __name__ == "__main__":
    main()
