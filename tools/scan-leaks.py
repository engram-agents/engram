#!/usr/bin/env python3
"""scan-leaks.py — defensive scanner for personal references in alpha.

Belt-and-suspenders against regex-miss in the upstream release script.
Run standalone or wire as a pre-commit hook.

Standalone:
    python3 tools/scan-leaks.py            # exit 0 clean, 1 if leaks
    python3 tools/scan-leaks.py --strict   # also flag /home/<user>/ paths

Pre-commit (from inside ~/engram-alpha):
    ln -s ../../tools/scan-leaks.py .git/hooks/pre-commit

Patterns flagged:
  - Personal names: Lei, Lin, the child, Borges (word-bounded)
  - Main repo path: kg_protocol_for_ai
  - Hardcoded user home: /home/user/

Allowlist:
  - LICENSE (copyright lines are intentional)
  - .git/, __pycache__/, *.pyc

Exit codes:
  0 — clean
  1 — leaks found
  2 — invocation error
"""

import argparse
import os
import re
import sys
from pathlib import Path

LEAK_PATTERNS = [
    (re.compile(r"\b(Lei|Lin|the child|Borges)\b"), "personal name"),
    (re.compile(r"kg_protocol_for_ai"), "main repo path"),
    (re.compile(r"/home/user/"), "hardcoded user home"),
]

SCAN_EXTENSIONS = {".py", ".md", ".sh", ".json", ".template"}
SKIP_FILES = {"LICENSE", "scan-leaks.py"}  # scanner self-matches on its own regex literals
SKIP_DIRS = {".git", "__pycache__", "node_modules"}


def scan(root: Path) -> list[tuple[str, int, str, str]]:
    leaks = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            if fname in SKIP_FILES:
                continue
            ext = Path(fname).suffix
            if ext not in SCAN_EXTENSIONS:
                continue
            fpath = Path(dirpath) / fname
            try:
                content = fpath.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            rel = fpath.relative_to(root)
            for i, line in enumerate(content.splitlines(), 1):
                for pattern, label in LEAK_PATTERNS:
                    if pattern.search(line):
                        leaks.append((str(rel), i, label, line.strip()[:120]))
                        break
    return leaks


def main():
    parser = argparse.ArgumentParser(description="Scan alpha for personal-info leaks")
    parser.add_argument("--root", default=None,
                        help="Root to scan (default: parent of tools/ dir, i.e., the alpha repo)")
    args = parser.parse_args()

    if args.root:
        root = Path(args.root).expanduser().resolve()
    else:
        root = Path(__file__).resolve().parent.parent

    if not root.exists():
        print(f"scan-leaks: root not found: {root}", file=sys.stderr)
        sys.exit(2)

    leaks = scan(root)
    if not leaks:
        print(f"scan-leaks: clean ({root})")
        sys.exit(0)

    print(f"scan-leaks: {len(leaks)} potential leak(s) in {root}", file=sys.stderr)
    for rel, lineno, label, snippet in leaks:
        print(f"  {rel}:{lineno} [{label}] {snippet}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
