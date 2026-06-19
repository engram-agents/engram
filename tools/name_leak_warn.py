#!/usr/bin/env python3
"""name_leak_warn.py — warn-only scanner for personal/agent name mentions in PR diffs.

Reads roster patterns from a roster file (or stdin in roster mode), reads a unified
diff from a diff file (or stdin in diff mode), and reports added lines that match
any pattern.

Usage:
    git diff origin/dev...HEAD | python3 tools/name_leak_warn.py --roster-file ROSTER
    python3 tools/name_leak_warn.py --diff-file DIFF --roster-file ROSTER
    python3 tools/name_leak_warn.py --diff-file DIFF --roster-file ROSTER --format json

Roster file format (newline-separated regex patterns, blank lines and #-comments ignored):
    \\bAlice Example\\b
    \\bagentname-nick\\b

Output (text, default):
    path/to/file.py:42:[pattern_index=0] + the offending line content

Output (JSON, --format json):
    {"matches": [{"file": "...", "line_number": 42, "pattern_index": 0, "line": "..."}]}

Exit codes:
    0 — scan completed (matches found or not — this is warn-only)
    1 — usage/IO error (bad arguments, unreadable file, invalid roster pattern)
"""

import argparse
import json
import re
import sys
from pathlib import Path


def load_roster(roster_source: str | None, roster_file: str | None) -> list[re.Pattern]:
    """Load newline-separated regex patterns from a string or file path.

    Blank lines and lines starting with '#' are skipped.
    Raises SystemExit(1) on invalid regex.

    Returns empty list if the source is None or empty (skip-when-absent).
    """
    if roster_file is not None:
        try:
            text = Path(roster_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"name_leak_warn: error reading roster file: {exc}", file=sys.stderr)
            sys.exit(1)
    elif roster_source is not None:
        text = roster_source
    else:
        return []

    patterns = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line))
        except re.error as exc:
            print(f"name_leak_warn: invalid pattern {line!r}: {exc}", file=sys.stderr)
            sys.exit(1)
    return patterns


def scan_diff(diff_text: str, patterns: list[re.Pattern]) -> list[dict]:
    """Scan added lines in a unified diff for pattern matches.

    Added lines start with '+' but NOT '+++' (which are diff headers).
    Tracks the current file path and line number in the new file.

    Returns a list of match dicts with keys:
        file, line_number, pattern_index, line
    """
    matches = []
    current_file = ""
    new_line_number = 0

    for raw_line in diff_text.splitlines():
        # Track current file from diff header
        if raw_line.startswith("+++ b/"):
            current_file = raw_line[6:]
            continue
        if raw_line.startswith("+++ "):
            # Handle "+++ /dev/null" or other forms
            current_file = raw_line[4:]
            continue
        if raw_line.startswith("@@"):
            # Parse hunk header: @@ -a,b +c,d @@ — extract new-file start line
            # Format: @@ -old_start[,old_count] +new_start[,new_count] @@
            try:
                hunk_info = raw_line.split("+")[1].split("@@")[0].strip()
                new_line_number = int(hunk_info.split(",")[0]) - 1
            except (IndexError, ValueError):
                new_line_number = 0
            continue
        if raw_line.startswith("---"):
            # Old file header — skip
            continue
        if raw_line.startswith("+"):
            # Added line — increment and check
            new_line_number += 1
            line_content = raw_line[1:]  # strip leading '+'
            for idx, pattern in enumerate(patterns):
                if pattern.search(line_content):
                    matches.append({
                        "file": current_file,
                        "line_number": new_line_number,
                        "pattern_index": idx,
                        "line": line_content,
                    })
                    break  # one match per line is enough
        elif raw_line.startswith(" ") or raw_line == "":
            # Context line — counts toward new-file line numbers
            new_line_number += 1
        # Removed lines ('-') do not appear in the new file — no increment

    return matches


def format_text(matches: list[dict]) -> str:
    """Format matches as human-readable text."""
    if not matches:
        return ""
    lines = []
    for m in matches:
        lines.append(
            f"{m['file']}:{m['line_number']}:[pattern_index={m['pattern_index']}] {m['line']}"
        )
    return "\n".join(lines)


def format_json(matches: list[dict]) -> str:
    """Format matches as JSON."""
    return json.dumps({"matches": matches}, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="Warn-only scanner for name mentions in PR diffs."
    )
    parser.add_argument(
        "--roster-file",
        metavar="PATH",
        help="Path to newline-separated regex pattern file. "
             "If absent, reads from stdin when --diff-file is also given.",
    )
    parser.add_argument(
        "--diff-file",
        metavar="PATH",
        help="Path to unified diff file. "
             "If absent, reads diff from stdin (requires --roster-file).",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text).",
    )
    args = parser.parse_args()

    # Validate argument combination: need exactly one stdin source
    if args.roster_file is None and args.diff_file is None:
        print(
            "name_leak_warn: error — provide --roster-file when reading diff from stdin, "
            "or provide both --roster-file and --diff-file.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Load roster
    patterns = load_roster(roster_source=None, roster_file=args.roster_file)

    # Load diff
    if args.diff_file is not None:
        try:
            diff_text = Path(args.diff_file).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"name_leak_warn: error reading diff file: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        diff_text = sys.stdin.read()

    # Scan
    matches = scan_diff(diff_text, patterns)

    # Output
    if args.format == "json":
        print(format_json(matches))
    else:
        if matches:
            print(format_text(matches))

    # Always exit 0 on scan completion (warn-only)
    sys.exit(0)


if __name__ == "__main__":
    main()
