"""Full-directory backup tool — mirror ~/.claude/projects/ to a persistent archive.

Scope: ~/.claude/projects/ only. Does not back up ~/.engram/ (knowledge.db is
covered by the dedicated SQL dump in Step 4.5). shutil.copy2 preserves source
mtime -- --retain prunes by when the source file was last modified.

Mirrors the complete ~/.claude/projects/ tree (all file types) to
~/.engram/claude-projects-archive/, preserving directory structure.  Complements
backup_session_logs.py (which archives .jsonl logs to a separate
retention-independent location); this tool captures configs, subagent transcripts,
and any other artifacts for disaster-recovery and migration-safety.

Deduplication:
  Uncompressed — skip if destination exists AND sizes match AND mtime within 1s.
  Compressed   — skip if .gz destination exists AND mtime within 1s.  After writing
                 compressed, dest mtime is set to source mtime via os.utime so that
                 subsequent runs detect unchanged files correctly.

No .db files exist in ~/.claude/projects/ as of 2026-06-24; this tool backs up the
directory as-is. If .db files are added, WAL-checkpoint before copying.

Usage:
    python3 backup_claude_projects.py [--claude-projects-dir DIR]
                                       [--archive-dir DIR]
                                       [--dry-run] [--compress]
                                       [--retain DAYS]
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import os
import shutil
import sys
from pathlib import Path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="backup_claude_projects",
        description="Mirror ~/.claude/projects/ to a persistent archive directory.",
    )
    p.add_argument(
        "--claude-projects-dir",
        default="~/.claude/projects",
        help="Source directory to mirror (default: ~/.claude/projects).",
    )
    p.add_argument(
        "--archive-dir",
        default="~/.engram/claude-projects-archive",
        help="Destination archive directory (default: ~/.engram/claude-projects-archive).",
    )
    p.add_argument(
        "--compress",
        action="store_true",
        help="Gzip-compress each file in the archive (appends .gz suffix).",
    )
    p.add_argument(
        "--retain",
        type=int,
        default=0,
        help=(
            "If > 0, prune archive files older than DAYS days by mtime. "
            "Default: 0 (keep all)."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen; do not create or modify any files.",
    )
    return p.parse_args(argv)


def _prune_old_entries(archive_dir: str, retain_days: int, dry_run: bool) -> int:
    """Delete archive files whose mtime is older than retain_days days.

    shutil.copy2 preserves source mtime, so the archive entry's mtime reflects
    when the source file was last modified -- intentional, so --retain prunes by
    source-file age.

    Returns the count of files pruned (or would-prune in dry_run).
    """
    cutoff_ts = (
        datetime.datetime.now() - datetime.timedelta(days=retain_days)
    ).timestamp()

    pruned = 0
    for dirpath, _dirnames, filenames in os.walk(archive_dir):
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            try:
                mtime = os.path.getmtime(fpath)
            except OSError:
                continue
            if mtime < cutoff_ts:
                if dry_run:
                    print(f"[dry-run] would prune: {fpath}")
                else:
                    os.unlink(fpath)
                pruned += 1
    return pruned


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    src_root = os.path.expanduser(args.claude_projects_dir)
    archive_dir = os.path.expanduser(args.archive_dir)

    # Source directory absent or empty: nothing to do.
    if not os.path.exists(src_root):
        print("0 files copied, 0 skipped")
        return 0

    # Check emptiness: os.walk yields nothing useful for an empty dir.
    has_any = False
    for _dirpath, _dirnames, filenames in os.walk(src_root):
        if filenames:
            has_any = True
            break
    if not has_any:
        print("0 files copied, 0 skipped")
        return 0

    copied = 0
    skipped = 0

    for dirpath, _dirnames, filenames in os.walk(src_root):
        for fname in filenames:
            src_path = os.path.join(dirpath, fname)

            # Relative path from src_root (preserves subdirectory structure).
            rel = os.path.relpath(src_path, src_root)

            if args.compress:
                dest_path = os.path.join(archive_dir, rel + ".gz")
            else:
                dest_path = os.path.join(archive_dir, rel)

            # Ensure destination parent directory exists.
            if not args.dry_run:
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)

            # ------ Deduplication ------
            try:
                src_mtime = os.path.getmtime(src_path)
                src_atime = os.path.getatime(src_path)
                src_size = os.path.getsize(src_path)
            except OSError as exc:
                print(f"warning: could not stat {src_path}: {exc}", file=sys.stderr)
                continue

            if os.path.exists(dest_path):
                try:
                    dest_mtime = os.path.getmtime(dest_path)
                except OSError:
                    dest_mtime = -1.0

                if args.compress:
                    # Compressed: skip if mtime within 1s.
                    if abs(src_mtime - dest_mtime) < 1.0:
                        skipped += 1
                        continue
                else:
                    # Uncompressed: skip if size matches AND mtime within 1s.
                    try:
                        dest_size = os.path.getsize(dest_path)
                    except OSError:
                        dest_size = -1
                    if dest_size == src_size and abs(src_mtime - dest_mtime) < 1.0:
                        skipped += 1
                        continue

            # ------ Copy ------
            if args.dry_run:
                print(f"[dry-run] would copy: {src_path} -> {dest_path}")
                copied += 1
                continue

            if args.compress:
                try:
                    with open(src_path, "rb") as f_in:
                        data = f_in.read()
                    with gzip.open(dest_path, "wb") as f_out:
                        f_out.write(data)
                    # Set dest mtime to src mtime so dedup works on next run.
                    os.utime(dest_path, (src_atime, src_mtime))
                    copied += 1
                except OSError as exc:
                    print(
                        f"error: could not compress {src_path} -> {dest_path}: {exc}",
                        file=sys.stderr,
                    )
            else:
                try:
                    shutil.copy2(src_path, dest_path)  # preserves mtime
                    copied += 1
                except OSError as exc:
                    print(
                        f"error: could not copy {src_path} -> {dest_path}: {exc}",
                        file=sys.stderr,
                    )

    # ------ Retention pruning ------
    pruned = 0
    if args.retain > 0 and os.path.exists(archive_dir):
        pruned = _prune_old_entries(archive_dir, args.retain, args.dry_run)

    # ------ Summary ------
    summary = f"{copied} files copied, {skipped} skipped"
    if pruned:
        summary += f", {pruned} pruned"
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
