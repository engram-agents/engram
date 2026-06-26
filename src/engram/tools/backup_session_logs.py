"""Session-log backup tool — copy main session logs to a retention-independent archive.

Backs up top-level Claude session logs (*.jsonl) from ~/.claude/projects/ to
~/.engram/session-logs-archive/, skipping subagent logs (paths containing /subagents/).

Top-level logs are cited as source_url evidence in ENGRAM nodes; subagent logs are
ephemeral fairy/sub-agent transcripts that are never cited.  The archive gives the
cited top-level logs a retention-independent home that survives regardless of
Claude Code's cleanupPeriodDays setting.

Deduplication: skip if destination already exists AND file sizes match.  Re-copy if
size differs (the source file may have grown in a resumed session).

Basename collision handling: when two source files from different project directories
share the same basename, both are archived — the second is prefixed with an 8-char
hex hash of its source directory path to avoid silent overwrites.

Stdlib only; no external packages required.

Usage:
    python3 backup_session_logs.py [--claude-projects-dir PATH] [--archive-dir PATH]
                                   [--retain DAYS] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import shutil
import sys


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="backup_session_logs",
        description="Back up main Claude session logs (skip subagents/) to a persistent archive.",
    )
    p.add_argument(
        "--claude-projects-dir",
        default="~/.claude/projects",
        help="Root directory to scan for .jsonl session logs (default: ~/.claude/projects).",
    )
    p.add_argument(
        "--archive-dir",
        default="~/.engram/session-logs-archive",
        help="Destination archive directory (default: ~/.engram/session-logs-archive).",
    )
    p.add_argument(
        "--retain",
        type=int,
        default=0,
        help="If > 0, prune archive entries older than DAYS days by mtime. Default: 0 (keep all).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen; do not create or delete files.",
    )
    return p.parse_args(argv)


def _is_subagent_path(path: str) -> bool:
    """Return True if the path contains /subagents/ anywhere in it."""
    # Normalise separators so the check is platform-consistent.
    norm = path.replace(os.sep, "/")
    return "/subagents/" in norm


def _source_dir_hash(src_dir: str) -> str:
    """Return an 8-char hex hash of the source directory path for collision avoidance."""
    return hashlib.sha256(src_dir.encode()).hexdigest()[:8]


def _collect_candidates(projects_dir: str) -> list[tuple[str, str]]:
    """Walk projects_dir recursively; return list of (src_path, basename) for top-level logs."""
    candidates: list[tuple[str, str]] = []
    for dirpath, _dirnames, filenames in os.walk(projects_dir):
        for fname in filenames:
            if not fname.endswith(".jsonl"):
                continue
            full_path = os.path.join(dirpath, fname)
            if _is_subagent_path(full_path):
                continue
            candidates.append((full_path, fname))
    return candidates


def _build_dest_map(
    candidates: list[tuple[str, str]],
    archive_dir: str,
) -> list[tuple[str, str]]:
    """Map each (src_path, basename) to a unique destination path inside archive_dir.

    Collision rule: the first file claiming a basename wins it bare.  Subsequent
    files with the same basename get an 8-char hash prefix derived from their
    source directory.
    """
    seen: dict[str, str] = {}  # basename → first src_path that claimed it
    mapping: list[tuple[str, str]] = []  # (src_path, dest_path)

    for src_path, basename in candidates:
        src_dir = os.path.dirname(src_path)
        if basename not in seen:
            seen[basename] = src_path
            dest_path = os.path.join(archive_dir, basename)
        else:
            # Collision: prefix with hash of source dir.
            prefix = _source_dir_hash(src_dir)
            dest_path = os.path.join(archive_dir, f"{prefix}-{basename}")

        mapping.append((src_path, dest_path))

    return mapping


def _prune_old_entries(archive_dir: str, retain_days: int, dry_run: bool) -> None:
    """Delete archive .jsonl files whose mtime is older than retain_days days.

    shutil.copy2 preserves source mtime, so the archive entry's mtime reflects
    when the session occurred — intentional, so --retain prunes by session age.
    """
    cutoff_ts = (
        datetime.datetime.now() - datetime.timedelta(days=retain_days)
    ).timestamp()

    for fname in os.listdir(archive_dir):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(archive_dir, fname)
        try:
            mtime = os.path.getmtime(fpath)
        except OSError:
            continue
        if mtime < cutoff_ts:
            if dry_run:
                print(f"[dry-run] would prune: {fpath}")
            else:
                os.unlink(fpath)
                print(f"pruned: {fpath}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    projects_dir = os.path.expanduser(args.claude_projects_dir)
    archive_dir = os.path.expanduser(args.archive_dir)

    # Source directory must exist (but may be empty — that's fine).
    if not os.path.exists(projects_dir):
        print(
            f"Backed up 0 session logs to {archive_dir} (0 skipped, archive total 0 MB)"
        )
        return 0

    # Create archive dir if absent.
    if not os.path.exists(archive_dir):
        if args.dry_run:
            print(f"[dry-run] would create directory: {archive_dir}")
        else:
            os.makedirs(archive_dir, exist_ok=True)

    candidates = _collect_candidates(projects_dir)

    if not candidates:
        print(
            f"Backed up 0 session logs to {archive_dir} (0 skipped, archive total 0 MB)"
        )
        return 0

    mapping = _build_dest_map(candidates, archive_dir)

    copied = 0
    skipped = 0

    for src_path, dest_path in mapping:
        try:
            src_size = os.path.getsize(src_path)
        except OSError as exc:
            print(f"warning: could not stat {src_path}: {exc}", file=sys.stderr)
            continue

        # Skip if destination exists AND sizes match.
        if os.path.exists(dest_path):
            try:
                dest_size = os.path.getsize(dest_path)
            except OSError:
                dest_size = -1  # force re-copy

            if dest_size == src_size:
                skipped += 1
                continue
            # Size differs — fall through to re-copy.

        if args.dry_run:
            print(f"[dry-run] would copy: {src_path} → {dest_path}")
            copied += 1
        else:
            try:
                shutil.copy2(src_path, dest_path)
                copied += 1
            except OSError as exc:
                print(f"error: could not copy {src_path} → {dest_path}: {exc}", file=sys.stderr)

    # Retention pruning.
    if args.retain > 0 and os.path.exists(archive_dir):
        _prune_old_entries(archive_dir, args.retain, args.dry_run)

    # Compute archive total size in MB.
    total_bytes = 0
    if os.path.exists(archive_dir) and not args.dry_run:
        for fname in os.listdir(archive_dir):
            if fname.endswith(".jsonl"):
                fpath = os.path.join(archive_dir, fname)
                try:
                    total_bytes += os.path.getsize(fpath)
                except OSError:
                    pass

    total_mb = total_bytes / (1024 * 1024)
    print(
        f"Backed up {copied} session logs to {archive_dir} "
        f"({skipped} skipped, archive total {total_mb:.1f} MB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
