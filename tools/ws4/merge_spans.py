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
empty, when an --out is not a directory, when an --out has neither spans/ nor
subagent/spans/ (e.g. the parent of per-day directories), or when a fire id
appears twice. An --out whose spans directories exist but are empty is fine.
Prints one line per --out with the spans it contributed, then the total.

Usage:
  merge_spans.py --out OUT [--out OUT ...] --dest DEST
"""
import argparse
import shutil
import sys
from pathlib import Path


class MergeRefused(Exception):
    pass


def _collect(out_dirs: list):
    """(file name -> source path, [(out, span count), ...]) over every out's
    spans/ and subagent/spans/. An --out with neither directory is refused; one
    whose directories exist but are empty is fine and counts 0."""
    found = {}
    counts = []
    for out in out_dirs:
        out = Path(out)
        if not out.is_dir():
            raise MergeRefused(f"--out {out} is not a directory")
        spans_dirs = [d for d in (out / "spans", out / "subagent" / "spans") if d.is_dir()]
        if not spans_dirs:
            raise MergeRefused(
                f"--out {out} has neither spans/ nor subagent/spans/; "
                "is it the parent of per-day directories?"
            )
        count = 0
        for spans_dir in spans_dirs:
            for src in sorted(spans_dir.glob("fire-*.md")):
                if src.name in found:
                    raise MergeRefused(
                        f"fire id {src.stem} appears twice: {found[src.name]} and {src}"
                    )
                found[src.name] = src
                count += 1
        counts.append((out, count))
    return found, counts


def merge(out_dirs: list, dest: str) -> list:
    return _merge(out_dirs, dest)[0]


def _merge(out_dirs: list, dest: str):
    dest = Path(dest)
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise MergeRefused(f"{dest} already exists and is not empty; use a fresh --dest")
    found, counts = _collect(out_dirs)
    names = sorted(found)  # pure fire-id order, whatever the source directory
    spans_dir = dest / "spans"
    spans_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copyfile(found[name], spans_dir / name)
    (dest / "order.txt").write_text("".join(f"{n}\n" for n in names), encoding="utf-8")
    return names, counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", action="append", required=True, help="an extract_spans.py --out directory (repeatable)")
    parser.add_argument("--dest", required=True, help="fresh directory for the merged delivery")
    args = parser.parse_args(argv)
    try:
        names, counts = _merge(args.out, args.dest)
    except MergeRefused as exc:
        print(f"merge_spans: {exc}", file=sys.stderr)
        return 2
    for out, count in counts:
        print(f"  {out}: {count} spans")
    print(f"merged {len(names)} spans into {Path(args.dest) / 'spans'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
