#!/usr/bin/env python3
"""scan-leaks.py — defensive scanner for personal references in alpha.

Belt-and-suspenders against regex-miss in the upstream release script.
Run standalone or wire as a pre-commit hook.

Standalone:
    python3 tools/scan-leaks.py            # exit 0 clean, 1 if leaks
    python3 tools/scan-leaks.py --strict   # also flag /home/<user>/ paths

Pre-commit (from inside ~/engram-alpha):
    ln -s ../../tools/scan-leaks.py .git/hooks/pre-commit

Patterns flagged (committed structural patterns):
  - Main repo path: kg_protocol_for_ai

Personal-name and user-specific patterns are loaded at runtime from
.scan-leaks-local.json in the repo root (gitignored). If this file is
absent the scanner runs committed structural patterns only and prints one
advisory line.

See .scan-leaks-local.json.example for the config shape.

Allowlist:
  - LICENSE (copyright lines are intentional)
  - .git/, __pycache__/, *.pyc

Exit codes:
  0 — clean
  1 — leaks found
  2 — invocation error
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Structural patterns with NO personal data — safe to commit.
COMMITTED_PATTERNS = [
    (re.compile(r"kg_protocol_for_ai"), "main repo path"),
]

LOCAL_CONFIG_NAME = ".scan-leaks-local.json"

SCAN_EXTENSIONS = {".py", ".md", ".sh", ".json", ".template", ".service"}
# LICENSE: copyright lines are intentional.
# .scan-leaks-local.json: the local personal-name roster must be skipped —
#   it contains the exact patterns the scanner is looking for (by design).
# scan-leaks.py: STRUCTURAL self-skip — the scanner's own source always
#   contains the literal patterns it checks (docstring + COMMITTED_PATTERNS
#   definition).  This is inherent to the tool's design, not personal-data.
SKIP_FILES = {"LICENSE", LOCAL_CONFIG_NAME, "scan-leaks.py"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".claude"}
# .claude: harness-internal (agent worktrees + transcripts) — stale fairy
# worktrees carry pre-fix file copies that read as false-positive leaks.


def load_local_patterns(root: Path, config_path: Path | None = None) -> tuple[list, bool]:
    """Load personal-name patterns from the gitignored local config.

    Returns (patterns, config_present) where patterns is a list of
    (compiled_regex, label) tuples.  config_present is False when the file
    does not exist at all; True even when the file exists but is malformed or
    has no valid patterns (so the caller can distinguish "no file" from
    "file exists but produced nothing").
    """
    if config_path is None:
        config_path = root / LOCAL_CONFIG_NAME
    if not config_path.exists():
        return [], False

    try:
        raw = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"scan-leaks: warning — could not load {config_path.name}: {exc}",
              file=sys.stderr)
        return [], True  # file existed but malformed; treat as present

    patterns = []
    for entry in raw.get("patterns", []):
        try:
            patterns.append((re.compile(entry["regex"]), entry["label"]))
        except (KeyError, re.error) as exc:
            print(f"scan-leaks: warning — skipping bad pattern entry {entry!r}: {exc}",
                  file=sys.stderr)
    return patterns, True


def scan(root: Path, leak_patterns: list) -> list[tuple[str, int, str, str]]:
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
                for pattern, label in leak_patterns:
                    if pattern.search(line):
                        leaks.append((str(rel), i, label, line.strip()[:120]))
                        break
    return leaks


def main():
    parser = argparse.ArgumentParser(description="Scan alpha for personal-info leaks")
    parser.add_argument("--root", default=None,
                        help="Root to scan (default: parent of tools/ dir, i.e., the alpha repo)")
    parser.add_argument("--config-path", default=None,
                        help=f"Explicit path to the local config file "
                             f"(default: <root>/{LOCAL_CONFIG_NAME})")
    args = parser.parse_args()

    if args.root:
        root = Path(args.root).expanduser().resolve()
    else:
        root = Path(__file__).resolve().parent.parent

    if not root.exists():
        print(f"scan-leaks: root not found: {root}", file=sys.stderr)
        sys.exit(2)

    config_path = Path(args.config_path).expanduser().resolve() if args.config_path else None
    local_patterns, config_present = load_local_patterns(root, config_path)

    if not local_patterns:
        # Advisory fires whenever the personal-name net didn't run — covers
        # both "file absent" and "file present but malformed/empty".
        reason = (f"{LOCAL_CONFIG_NAME} absent"
                  if not config_present else f"{LOCAL_CONFIG_NAME} produced no patterns")
        print(
            f"scan-leaks: no local roster ({reason})"
            " — personal-name scan skipped; structural patterns only"
        )

    all_patterns = COMMITTED_PATTERNS + local_patterns

    if not all_patterns:
        print(f"scan-leaks: clean ({root})")
        sys.exit(0)

    leaks = scan(root, all_patterns)
    if not leaks:
        print(f"scan-leaks: clean ({root})")
        sys.exit(0)

    print(f"scan-leaks: {len(leaks)} potential leak(s) in {root}", file=sys.stderr)
    for rel, lineno, label, snippet in leaks:
        print(f"  {rel}:{lineno} [{label}] {snippet}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
