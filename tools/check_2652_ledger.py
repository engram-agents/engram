#!/usr/bin/env python3
"""Completeness guard for docs/2652-migration-ledger.md [part of #2652].

The ledger's Section 2 holds one row per line matching `sqlite3\\.connect\\(` in
TRACKED files under `src/engram`. This check compares, PER FILE, the number of
Section-2 rows naming the file with the number of matching lines in it. It is
keyed on counts, not file:line, so unrelated edits that shift line numbers do
not turn it red.

This is a COUNT-COMPLETENESS guard and nothing more. It does NOT check that a
row's contents (target, state, function, line) are right, and it does not check
Section 1's tallies. A same-file swap (one connect line removed and another
added in a different function of the same file) leaves the per-file count
unchanged and so passes by design: that is the price of tolerating line drift.

Exit 0 when consistent. Exit 1 with one `file: ledger N rows, tree M lines`
line per differing file and a remedy line; also exit 1 ("could not parse") when
Section 2 yields no rows, so zero parsed rows can never read as consistent, and
exit 1 ("could not read <path>: <error>", one per file) when a tracked file
cannot be read, so an unread file can never read as consistent. Outside a
checkout it exits 1 with the single line "not a git checkout: <root>".

Usage: check_2652_ledger.py [--root DIR]   (DIR defaults to the repo root)
"""

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

LEDGER = "docs/2652-migration-ledger.md"
SRC = "src/engram"
CONNECT = re.compile(r"sqlite3\.connect\(")
# A Section-2 row: `| 12 | `path/to/file.py:345` (function) | ...`
ROW = re.compile(r"^\|\s*\d+\s*\|\s*`([^`]+):\d+`")
SECTION2 = re.compile(r"^##\s+2\.")
ANY_SECTION = re.compile(r"^##\s")

REMEDY = (
    "add/remove a Section-2 row in docs/2652-migration-ledger.md "
    "for each connect line you added/removed"
)


def ledger_counts(text):
    """Rows per file (path relative to src/engram) in Section 2, or None."""
    counts = Counter()
    in_section = False
    seen_section = False
    for line in text.splitlines():
        if SECTION2.match(line):
            in_section = seen_section = True
            continue
        if in_section and ANY_SECTION.match(line):
            break
        if in_section:
            m = ROW.match(line)
            if m:
                counts[m.group(1)] += 1
    if not seen_section or not counts:
        return None
    return counts


def tree_counts(root):
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", SRC],
        cwd=root, check=True, capture_output=True,
    ).stdout.decode("utf-8", "replace")
    counts = Counter()
    unreadable = []
    for rel in filter(None, out.split("\0")):
        path = Path(root) / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            # Fail loud: an unread file may hold connect lines, and skipping
            # it would let the guard report consistent.
            unreadable.append(f"could not read {rel}: {e}")
            continue
        n = sum(1 for line in text.splitlines() if CONNECT.search(line))
        if n:
            counts[rel[len(SRC) + 1:]] = n
    return counts, unreadable


def check(root):
    """Return (exit_code, output_lines)."""
    ledger = Path(root) / LEDGER
    try:
        text = ledger.read_text(encoding="utf-8")
    except OSError as e:
        return 1, [f"could not parse {LEDGER}: {e}"]
    rows = ledger_counts(text)
    if rows is None:
        return 1, [f"could not parse Section 2 of {LEDGER}: no ledger rows found"]
    try:
        tree, unreadable = tree_counts(root)
    except subprocess.CalledProcessError:
        return 1, [f"not a git checkout: {root}"]
    if unreadable:
        return 1, unreadable
    bad = [
        f"{f}: ledger {rows.get(f, 0)} rows, tree {tree.get(f, 0)} lines"
        for f in sorted(set(rows) | set(tree))
        if rows.get(f, 0) != tree.get(f, 0)
    ]
    if not bad:
        return 0, []
    return 1, bad + [REMEDY]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args(argv)
    code, lines = check(args.root)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
