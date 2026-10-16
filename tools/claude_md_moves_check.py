#!/usr/bin/env python3
"""Nothing-lost check for blocks moved out of CLAUDE.md into docs/ (#3322).

Reads docs/claude-md-moves.json: {"moves": [{"from_section", "to_doc",
"first_sentence", "last_sentence"}, ...]}. For each moved block, its first and
last sentence must appear exactly once in the named doc and zero times in
CLAUDE.md. Stdlib only. Exit 0 = clean, 1 = violations.

Usage: python3 tools/claude_md_moves_check.py [--root PATH]
"""

import argparse
import json
import sys
from pathlib import Path

MAPPING = "docs/claude-md-moves.json"
KEYS = ("from_section", "to_doc", "first_sentence", "last_sentence")


def check(root):
    """Return a list of violation strings (empty = clean)."""
    root = Path(root)
    mapping = root / MAPPING
    if not mapping.is_file():
        return [f"{MAPPING} is missing"]
    try:
        moves = json.loads(mapping.read_text(encoding="utf-8"))["moves"]
    except (ValueError, KeyError, TypeError) as exc:
        return [f"{MAPPING} is unusable: {exc!r}"]
    if not isinstance(moves, list):
        return [f"{MAPPING}: 'moves' must be a list"]
    claude_path = root / "CLAUDE.md"
    if not claude_path.is_file():
        return ["CLAUDE.md is missing"]
    claude = claude_path.read_text(encoding="utf-8")
    errors = []
    docs = {}
    for i, move in enumerate(moves):
        if not isinstance(move, dict) or any(
            not isinstance(move.get(k), str) or not move[k].strip() for k in KEYS
        ):
            errors.append(f"move #{i}: needs non-empty string keys {KEYS}")
            continue
        label = f"move #{i} ({move['from_section']!r} -> {move['to_doc']})"
        doc_path = root / move["to_doc"]
        if not doc_path.is_file():
            errors.append(f"{label}: target doc does not exist")
            continue
        if move["to_doc"] not in docs:
            docs[move["to_doc"]] = doc_path.read_text(encoding="utf-8")
        text = docs[move["to_doc"]]
        for key in ("first_sentence", "last_sentence"):
            sentence = move[key]
            n_doc = text.count(sentence)
            if n_doc != 1:
                errors.append(
                    f"{label}: {key} appears {n_doc} times in doc (want exactly 1): {sentence!r}"
                )
            n_claude = claude.count(sentence)
            if n_claude != 0:
                errors.append(
                    f"{label}: {key} appears {n_claude} times in CLAUDE.md (want 0): {sentence!r}"
                )
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    args = parser.parse_args(argv)
    errors = check(args.root)
    if errors:
        print("claude-md moves check FAILED:", file=sys.stderr)
        for e in errors:
            print("  " + e, file=sys.stderr)
        return 1
    data = json.loads((Path(args.root) / MAPPING).read_text(encoding="utf-8"))
    print(f"claude-md moves check OK: {len(data['moves'])} moved block(s), 2 sentences each")
    return 0


if __name__ == "__main__":
    sys.exit(main())
