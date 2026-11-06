#!/usr/bin/env python3
"""WS4 delivery merge: one flat span list for the adjudicators (#2753).

The design owner ruled that spans go to the adjudicators as ONE list in
fire-id order, the strata not grouped. extract_spans.py writes the primary
spans to <out>/spans/ and (opt-in) the exploratory sub-agent spans to
<out>/subagent/spans/. This tool copies every fire-*.md from both, for each
--out given, into <dest>/spans/, flat, and writes <dest>/order.txt (the file
names sorted by fire id, one per line).

It writes NOTHING else: no key, flags, stratum, agent or model information.
The copies are byte-identical to the sources.

Refuses (exit 2, before writing anything) when <dest> exists and is not
empty, when an --out is not a directory, or when a fire id appears twice.

Usage:
  merge_spans.py --out OUT [--out OUT ...] --dest DEST
"""
import argparse
import shutil
import sys
from pathlib import Path


class MergeRefused(Exception):
    pass


def _collect(out_dirs: list) -> dict:
    """file name -> source path, over every out's spans/ and subagent/spans/."""
    found = {}
    for out in out_dirs:
        out = Path(out)
        if not out.is_dir():
            raise MergeRefused(f"--out {out} is not a directory")
        for spans_dir in (out / "spans", out / "subagent" / "spans"):
            if not spans_dir.is_dir():
                continue
            for src in sorted(spans_dir.glob("fire-*.md")):
                if src.name in found:
                    raise MergeRefused(
                        f"fire id {src.stem} appears twice: {found[src.name]} and {src}"
                    )
                found[src.name] = src
    return found


def merge(out_dirs: list, dest: str) -> list:
    dest = Path(dest)
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise MergeRefused(f"{dest} already exists and is not empty; use a fresh --dest")
    found = _collect(out_dirs)
    names = sorted(found)  # pure fire-id order, whatever the source directory
    spans_dir = dest / "spans"
    spans_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copyfile(found[name], spans_dir / name)
    (dest / "order.txt").write_text("".join(f"{n}\n" for n in names), encoding="utf-8")
    return names


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", action="append", required=True, help="an extract_spans.py --out directory (repeatable)")
    parser.add_argument("--dest", required=True, help="fresh directory for the merged delivery")
    args = parser.parse_args(argv)
    try:
        names = merge(args.out, args.dest)
    except MergeRefused as exc:
        print(f"merge_spans: {exc}", file=sys.stderr)
        return 2
    print(f"merged {len(names)} spans into {Path(args.dest) / 'spans'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
