#!/usr/bin/env python3
"""release-cutover.py — repeatable two-repo release cutover.

Produces a clean public-release snapshot from the private engram-alpha repo:
  1. git-archive the source ref into a temp dir (tracked files only).
  2. Apply the minus-set (release-minus-set.txt) — drop excluded paths.
  3. Run scan-leaks gate — fail-closed on any leak.
  4. Sanity gates — LICENSE + README disclaimer present.
  5. Publish to the public target repo as one squashed commit + annotated tag.
  6. Optionally push (--push flag; default off).

The minus-set (release-minus-set.txt) is the ONLY hand-maintained input.
Everything else is derived.

NOTE on scan-leaks coverage: scan-leaks skips extensionless files and does not
carry all personal-name patterns unless .scan-leaks-local.json is configured.
This gate is necessary-but-not-sufficient: it catches the roster patterns.
Structural review of the snapshot still matters — this tool surfaces the caveat
in its final summary.

Usage:
    python3 tools/release-cutover.py --version v0.1.0 --target /path/to/public/repo
    python3 tools/release-cutover.py --version v0.1.0 --target /path/to/public/repo --push
"""

import argparse
import fnmatch
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+$")
# The anchor phrase in README that confirms the public-facing disclaimer is present.
README_DISCLAIMER_PHRASE = "This is a personal project"
# Regex to extract author name from LICENSE copyright line.
# Matches: "Copyright YYYY Name Here" — captures "Name Here".
LICENSE_COPYRIGHT_RE = re.compile(r"Copyright\s+\d{4}\s+(.+)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], cwd: str | None = None, check: bool = True,
         capture: bool = False, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a subprocess. Abort with a clear message on nonzero exit if check=True."""
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=capture, text=True,
        env={**os.environ, **(env or {})},
    )
    if check and result.returncode != 0:
        print(f"ERROR: command failed (exit {result.returncode}): {' '.join(cmd)}", file=sys.stderr)
        if capture and result.stdout:
            print(result.stdout, file=sys.stderr)
        if capture and result.stderr:
            print(result.stderr, file=sys.stderr)
        sys.exit(1)
    return result


def _git(args: list[str], cwd: str, capture: bool = True,
         check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    return _run(["git"] + args, cwd=cwd, capture=capture, check=check, env=env)


def _source_short_sha(source: str, ref: str) -> str:
    result = _git(["rev-parse", "--short", ref], cwd=source)
    return result.stdout.strip()


def _source_full_sha(source: str, ref: str) -> str:
    result = _git(["rev-parse", ref], cwd=source)
    return result.stdout.strip()


def _is_git_repo(path: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=path, capture_output=True, text=True,
    )
    return result.returncode == 0


def _is_git_root(path: str) -> bool:
    """Return True iff path is the root of a git repository (not a subdirectory).

    Runs `git rev-parse --show-toplevel` and checks whether it resolves to the
    same filesystem entry as path.  Uses os.path.samefile for inode-level
    identity; falls back to string equality if samefile raises (e.g. the path
    does not exist yet).
    """
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    toplevel = result.stdout.strip()
    try:
        return os.path.samefile(toplevel, path)
    except OSError:
        return toplevel == path


def _is_working_tree_clean(path: str) -> bool:
    result = _git(["status", "--porcelain"], cwd=path)
    return result.stdout.strip() == ""


def _parse_license_author(source: str) -> str | None:
    """Try to read 'Copyright YYYY Name' from LICENSE; return 'Name' or None."""
    lic = Path(source) / "LICENSE"
    if not lic.exists():
        return None
    for line in lic.read_text().splitlines():
        m = LICENSE_COPYRIGHT_RE.search(line)
        if m:
            name = m.group(1).strip()
            if name:
                return name
    return None


def _parse_minus_set(minus_set_path: str) -> list[str]:
    """Parse minus-set file; return list of non-comment, non-blank patterns."""
    entries = []
    for line in Path(minus_set_path).read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            entries.append(stripped)
    return entries


def _apply_minus_set(tmp: Path, entries: list[str]) -> tuple[list[str], list[str]]:
    """Remove paths matching minus-set entries from tmp. Return (removed, warned)."""
    removed = []
    warned = []
    # Collect all files+dirs relative to tmp root.
    all_paths = []
    for root, dirs, files in os.walk(tmp):
        root_p = Path(root)
        for f in files:
            rel = (root_p / f).relative_to(tmp)
            all_paths.append(str(rel.as_posix()))
        for d in dirs:
            rel = (root_p / d).relative_to(tmp)
            all_paths.append(str(rel.as_posix()) + "/")

    for entry in entries:
        is_dir_entry = entry.endswith("/")
        matched_any = False

        # Collect files to remove for this entry.
        to_remove: list[Path] = []
        for relpath in all_paths:
            posix = relpath
            if is_dir_entry:
                # dir-entry: remove the whole subtree — any path whose first component
                # matches the prefix (strip trailing slash from entry for comparison).
                prefix = entry.rstrip("/")
                if posix == prefix + "/" or posix.startswith(prefix + "/"):
                    to_remove.append(tmp / relpath.rstrip("/"))
                    matched_any = True
            else:
                # File pattern: match against the full relative POSIX path.
                # fnmatch handles globs; also check basename-only if no slash.
                if fnmatch.fnmatch(posix, entry) or fnmatch.fnmatch(posix, "**/" + entry):
                    to_remove.append(tmp / posix)
                    matched_any = True
                elif "/" not in entry and fnmatch.fnmatch(posix.split("/")[-1], entry):
                    to_remove.append(tmp / posix)
                    matched_any = True

        for p in to_remove:
            if p.exists():
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                removed.append(str((p.relative_to(tmp)).as_posix()))

        if not matched_any:
            warned.append(entry)

    return removed, warned


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Produce a clean public-release snapshot from the private source repo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1].strip() if "Usage:" in __doc__ else "",
    )
    parser.add_argument("--version", required=True,
                        help="Release version, e.g. v0.1.0. Used for commit message + tag.")
    parser.add_argument("--target", required=True,
                        help="Path to local clone of the public release repo.")
    parser.add_argument("--source", default=None,
                        help="Path to the source repo (default: the repo containing this script).")
    parser.add_argument("--source-ref", default="HEAD",
                        help="Git ref to snapshot (tag/branch/sha). Default: HEAD.")
    parser.add_argument("--minus-set", default=None,
                        help="Path to the minus-set file. Default: <source>/release-minus-set.txt.")
    parser.add_argument("--leak-roster", default=None,
                        help="Path to .scan-leaks-local.json for personal-pattern gate. "
                             "Default: <source>/.scan-leaks-local.json if present.")
    parser.add_argument("--author", default=None,
                        help='Author for the release commit, e.g. "Lei Shi <lei@example.com>". '
                             "Default: parsed from LICENSE copyright line.")
    parser.add_argument("--push", action="store_true",
                        help="Push the branch + tag to target's origin after commit. Default: off.")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the interactive confirm before writing to target.")
    parser.add_argument("--force", action="store_true",
                        help="If the version tag already exists in target, delete it and re-tag. "
                             "Does NOT force-push — operator must push manually after --force.")
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Resolve paths
    # ------------------------------------------------------------------
    script_dir = Path(__file__).resolve().parent
    # Script lives in src/engram/tools/ or tools/ (symlink); repo root is 3 up from script
    # OR 2 up if invoked as tools/release-cutover.py. Resolve via git.
    # Use the realpath of the script to be symlink-safe.
    source = Path(args.source).resolve() if args.source else None
    if source is None:
        # Walk up from script location to find the git root.
        candidate = script_dir
        for _ in range(5):
            if (candidate / ".git").exists():
                source = candidate
                break
            candidate = candidate.parent
        if source is None:
            print("ERROR: could not locate source repo root from script location. "
                  "Pass --source explicitly.", file=sys.stderr)
            sys.exit(1)

    source_str = str(source)
    target_str = str(Path(args.target).resolve())

    # Inode-level identity check: catches symlink/hardlink/bind-mount aliasing
    # that a resolved-path string compare misses. Falls back to string equality
    # if samefile raises (e.g. permission error), so an exception can never
    # bypass the guard.
    _same = False
    try:
        _same = os.path.samefile(source_str, target_str)
    except OSError:
        _same = (source_str == target_str)
    if _same:
        print("ERROR: source and target resolve to the same path — aborting. "
              "The target wipe would destroy the source.", file=sys.stderr)
        sys.exit(1)

    minus_set_path = args.minus_set or str(source / "release-minus-set.txt")
    leak_roster_default = source / ".scan-leaks-local.json"
    scan_leaks_script = source / "tools" / "scan-leaks.py"

    # ------------------------------------------------------------------
    # Step 1: Validate inputs
    # ------------------------------------------------------------------
    print("=== release-cutover.py ===")
    print(f"  source     : {source_str}")
    print(f"  source-ref : {args.source_ref}")
    print(f"  target     : {target_str}")
    print(f"  version    : {args.version}")

    if not VERSION_RE.match(args.version):
        print(f"ERROR: --version must match ^v\\d+\\.\\d+\\.\\d+$, got: {args.version!r}",
              file=sys.stderr)
        sys.exit(1)

    if not _is_git_repo(source_str):
        print(f"ERROR: source is not a git repo: {source_str}", file=sys.stderr)
        sys.exit(1)

    if not Path(target_str).exists():
        print(f"ERROR: target path does not exist: {target_str}", file=sys.stderr)
        sys.exit(1)

    if not _is_git_repo(target_str):
        print(f"ERROR: target is not a git repo: {target_str}", file=sys.stderr)
        sys.exit(1)

    if not _is_git_root(target_str):
        print(f"ERROR: --target must be the git repository ROOT, not a subdirectory: {target_str}",
              file=sys.stderr)
        sys.exit(1)

    if not _is_working_tree_clean(target_str):
        print(f"ERROR: target working tree is dirty. Commit or stash changes first: {target_str}",
              file=sys.stderr)
        sys.exit(1)

    if not Path(minus_set_path).exists():
        print(f"ERROR: minus-set file not found: {minus_set_path}", file=sys.stderr)
        sys.exit(1)

    if not scan_leaks_script.exists():
        print(f"ERROR: scan-leaks.py not found at: {scan_leaks_script}", file=sys.stderr)
        sys.exit(1)

    # Verify origin remote exists when --push is requested.
    if args.push:
        origin_check = _run(
            ["git", "remote", "get-url", "origin"],
            cwd=target_str, check=False, capture=True,
        )
        if origin_check.returncode != 0:
            print("ERROR: --push requested but target has no 'origin' remote. "
                  "Add a remote or omit --push.", file=sys.stderr)
            sys.exit(1)

    # Resolve author.
    if args.author:
        author_str = args.author
    else:
        license_name = _parse_license_author(source_str)
        if license_name:
            # Build a generic noreply email from the name.
            slug = license_name.lower().replace(" ", ".")
            author_str = f"{license_name} <{slug}@users.noreply.github.com>"
            print(f"  author     : {author_str} (parsed from LICENSE)")
        else:
            print("ERROR: could not parse author from LICENSE copyright line. "
                  "Pass --author 'Name <email>' explicitly.", file=sys.stderr)
            sys.exit(1)

    # Parse author name and email for GIT_AUTHOR_* env vars.
    m = re.match(r"^(.+?)\s+<(.+?)>$", author_str)
    if not m:
        print(f"ERROR: --author must be in 'Name <email>' format, got: {author_str!r}",
              file=sys.stderr)
        sys.exit(1)
    author_name, author_email = m.group(1), m.group(2)

    # Check idempotence: does the version tag already exist in target?
    tag_check = _git(["tag", "-l", args.version], cwd=target_str)
    tag_exists = args.version in tag_check.stdout.strip().splitlines()
    if tag_exists:
        if not args.force:
            print(f"ERROR: tag {args.version!r} already exists in target. "
                  "Use --force to delete it and re-tag.", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"  WARNING: --force flag set; existing tag {args.version!r} will be "
                  "replaced after all gates pass.")

    # Resolve source ref to SHA before archiving.
    source_sha = _source_full_sha(source_str, args.source_ref)
    short_sha = _source_short_sha(source_str, args.source_ref)

    # ------------------------------------------------------------------
    # Step 2: Snapshot source ref into temp dir via git-archive
    # ------------------------------------------------------------------
    tmp_dir = tempfile.mkdtemp(prefix="release_cutover_")
    tmp = Path(tmp_dir)
    try:
        print(f"\n[1/6] Archiving {args.source_ref} @ {short_sha} → {tmp_dir} ...")
        archive_proc = subprocess.run(
            ["git", "-C", source_str, "archive", "--format=tar", args.source_ref],
            capture_output=True, check=True,
        )
        # Extract the tar to tmp.
        with tarfile.open(fileobj=io.BytesIO(archive_proc.stdout), mode="r:") as tf:
            tf.extractall(tmp_dir)
        file_count_before = sum(1 for _ in tmp.rglob("*") if _.is_file())
        print(f"    {file_count_before} files in snapshot (before minus-set).")

        # ------------------------------------------------------------------
        # Step 3: Apply the minus-set
        # ------------------------------------------------------------------
        entries = _parse_minus_set(minus_set_path)
        print(f"\n[2/6] Applying minus-set ({len(entries)} entries) ...")
        removed, warned = _apply_minus_set(tmp, entries)
        for r in removed:
            print(f"    REMOVED: {r}")
        for w in warned:
            print(f"    WARN: minus-set entry matched nothing (stale?): {w}")
        file_count_after = sum(1 for _ in tmp.rglob("*") if _.is_file())
        print(f"    {len(removed)} paths removed; {file_count_after} files remain.")

        # ------------------------------------------------------------------
        # Step 4: Leak gate
        # ------------------------------------------------------------------
        print(f"\n[3/6] Running scan-leaks gate ...")
        leak_cmd = [sys.executable, str(scan_leaks_script), "--root", tmp_dir]
        # Resolve leak roster: CLI flag > default .scan-leaks-local.json if present > none.
        leak_roster_path = args.leak_roster
        if not leak_roster_path and leak_roster_default.exists():
            leak_roster_path = str(leak_roster_default)
        if leak_roster_path:
            leak_cmd += ["--config-path", leak_roster_path]
        leak_result = subprocess.run(leak_cmd, capture_output=True, text=True)
        print(leak_result.stdout, end="")
        if leak_result.returncode != 0:
            print("\nERROR: scan-leaks found potential leaks — aborting. Target NOT modified.",
                  file=sys.stderr)
            print(leak_result.stderr, file=sys.stderr)
            sys.exit(1)
        leak_gate_result = "PASS (roster patterns clean)"
        if leak_result.returncode == 0 and "personal-name scan skipped" in leak_result.stdout:
            leak_gate_result = "PASS (structural patterns only; personal-name scan skipped — no roster)"

        # ------------------------------------------------------------------
        # Step 5: Sanity gates
        # ------------------------------------------------------------------
        print(f"\n[4/6] Sanity gates ...")
        license_path = tmp / "LICENSE"
        if not license_path.exists():
            print("ERROR: LICENSE not found in snapshot — aborting.", file=sys.stderr)
            sys.exit(1)
        print("    LICENSE: present.")

        readme_path = tmp / "README.md"
        if not readme_path.exists():
            readme_path = tmp / "README.rst"
        if not readme_path.exists():
            print("ERROR: README not found in snapshot — aborting.", file=sys.stderr)
            sys.exit(1)
        readme_text = readme_path.read_text()
        if README_DISCLAIMER_PHRASE not in readme_text:
            print(f'ERROR: README does not contain the required disclaimer phrase: '
                  f'{README_DISCLAIMER_PHRASE!r}\n'
                  f'Add this phrase to the README before cutting a public release.',
                  file=sys.stderr)
            sys.exit(1)
        print(f"    README disclaimer ({README_DISCLAIMER_PHRASE!r}): present.")

        # ------------------------------------------------------------------
        # Step 6: Confirm
        # ------------------------------------------------------------------
        print(f"\n[5/6] Summary before writing to target:")
        print(f"    version    : {args.version}")
        print(f"    source-ref : {args.source_ref} @ {short_sha}")
        print(f"    files      : {file_count_after}")
        print(f"    removed    : {len(removed)} paths (minus-set)")
        print(f"    target     : {target_str}")
        print(f"    author     : {author_str}")
        print(f"    push       : {'YES' if args.push else 'no (local only)'}")

        if not args.yes:
            try:
                answer = input("\nProceed? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                sys.exit(1)
            if answer not in ("y", "yes"):
                print("Aborted.")
                sys.exit(1)

        # ------------------------------------------------------------------
        # Step 7: Publish into target as ONE squashed commit
        # ------------------------------------------------------------------
        print(f"\n[6/6] Publishing to target ...")

        # Remove all tracked files from target working tree (keep .git).
        # --ignore-unmatch makes the wipe a no-op instead of erroring when the target
        # has no tracked files (first release to a fresh public repo).
        _git(["rm", "-rf", "--quiet", "--ignore-unmatch", "."], cwd=target_str)

        # Copy snapshot contents into target, preserving symlinks faithfully.
        # Default shutil behaviour dereferences symlinks (copies the target file),
        # which turns the 42 tools/X -> ../src/engram/tools/X symlinks into
        # duplicated real files.  symlinks=True keeps them as symlinks so the
        # published snapshot mirrors the source structure and a clone gets the
        # symlinks, not duplicate copies.
        for item in tmp.iterdir():
            dest = Path(target_str) / item.name
            if item.is_symlink():
                # Recreate the symlink verbatim (same relative target).
                os.symlink(os.readlink(item), dest)
            elif item.is_dir():
                shutil.copytree(item, dest, symlinks=True)
            else:
                shutil.copy2(item, dest)

        # Stage everything.
        _git(["add", "-A"], cwd=target_str)

        # Warn if the snapshot is byte-identical to the current HEAD (empty delta).
        # An empty delta is legal (--allow-empty below handles it), but is almost
        # always accidental — make it loud so the operator notices.
        empty_delta_check = _git(
            ["diff", "--cached", "--quiet"], cwd=target_str, check=False
        )
        if empty_delta_check.returncode == 0:
            print(
                "WARNING: snapshot is byte-identical to the target's current HEAD"
                " — committing an EMPTY release (--allow-empty)."
            )

        # Commit with explicit author+committer identity.
        commit_msg = (
            f"Release {args.version}\n\n"
            f"Snapshot of {args.source_ref} @ {short_sha}; "
            f"excludes {len(removed)} minus-set paths."
        )
        git_env = {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }
        _git(["commit", "--allow-empty", "-m", commit_msg], cwd=target_str, env=git_env)
        print(f"    Committed release {args.version}.")

        # Annotated tag. If --force and the tag existed, delete it now (all gates passed).
        if tag_exists and args.force:
            print(f"    Deleting old tag {args.version!r} (--force; all gates passed).")
            _git(["tag", "-d", args.version], cwd=target_str)
        tag_msg = f"Release {args.version}\n\nSnapshot of {args.source_ref} @ {short_sha}."
        _git(["tag", "-a", args.version, "-m", tag_msg], cwd=target_str, env=git_env)
        print(f"    Tagged {args.version}.")

        # ------------------------------------------------------------------
        # Step 8: Push (opt-in)
        # ------------------------------------------------------------------
        if args.push:
            print("    Pushing branch + tag to origin ...")
            _git(["push", "origin", "HEAD"], cwd=target_str)
            _git(["push", "origin", args.version], cwd=target_str)
            print(f"    Pushed.")
        else:
            print(f"\nLocal snapshot ready in {target_str!r} "
                  f"(commit + tag {args.version}). Review, then push to publish.")

        # ------------------------------------------------------------------
        # Step 9: Final summary
        # ------------------------------------------------------------------
        tag_sha = _git(["rev-list", "-n1", args.version], cwd=target_str).stdout.strip()[:8]
        print("\n=== Final summary ===")
        print(f"  version      : {args.version}")
        print(f"  source sha   : {short_sha}")
        print(f"  files shipped: {file_count_after}")
        print(f"  minus-set    : {len(removed)} paths removed, {len(warned)} stale entries")
        print(f"  leak gate    : {leak_gate_result}")
        print(f"  pushed       : {'yes' if args.push else 'no'}")
        print(f"  target tag   : {args.version} @ {tag_sha}")
        print()
        print("CAVEAT: scan-leaks is necessary-but-not-sufficient — it catches the configured")
        print("roster patterns and committed structural patterns. Extensionless files are skipped.")
        print("Structural review of the snapshot is still recommended before making the repo public.")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
