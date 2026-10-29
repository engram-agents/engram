#!/usr/bin/env python3
"""backup_remote_repair -- read, and explicitly repair, a diverged off-disk backup remote
(engram-alpha#4205).

    python3 tools/backup_remote_repair.py --status [--home PATH]
    python3 tools/backup_remote_repair.py --repair [--home PATH] [--max-blob-bytes N]

``--home`` overrides ``ENGRAM_HOME`` (default ``~/.engram``).  Stdlib only; refuses by default.

``--status`` prints ``backup-push-status.json`` (or ``absent``) and the live ahead/behind of the
current branch against its upstream (local refs only, no network).

``--repair`` force-pushes (``--force-with-lease``) ONLY when every check passes, in this order:

  1. control: every file under ``$ENGRAM_HOME/secrets/`` (when that directory exists) must match
     at least one secret pattern, else the scan proves nothing -> exit 5
     ``control failed: pattern matched M of N key files``.  With no key file at all it prints
     ``control: no key files under secrets/; pattern unproven on this seat`` and continues.
  2. tracked-tree secret scan (``git grep -I -l -E``) -> any hit: exit 2, paths printed
     (contents never printed).
  3. no tracked blob larger than ``--max-blob-bytes`` (default 100 MB) anywhere in history
     -> exit 4, paths printed.  This tool never rewrites history.
  4. fetch, then every remote-only commit must be a same-message "twin" of a local-only commit
     (the amend signature) -> otherwise exit 3, the non-twin commits printed.
  4b. the ADDED lines of the not-yet-pushed commits (``git log -p upstream..branch``) get the
     same secret scan, so a key committed then deleted still refuses (exit 2).
  5. ``git push --force-with-lease=<branch>:<upstream> <remote> <branch>`` (a plain ``git push``
     when nothing is behind), re-fetch, print ahead/behind and update the status file.

Never automatic: nothing calls this but a human or an agent that was told to.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_PKG_ROOT = str(Path(__file__).resolve().parent.parent)
if _PKG_ROOT not in sys.path:
    sys.path.append(_PKG_ROOT)
from tools import _owner_read as owner_read  # noqa: E402

STATUS_NAME = "backup-push-status.json"
DEFAULT_MAX_BLOB_BYTES = 100 * 1000 * 1000

# POSIX-ERE-compatible, so the same text serves `git grep -E` and Python's re.
SECRET_PATTERNS = (
    r"sk-ant-[a-z0-9]+-[A-Za-z0-9_-]{20,}",
    r"ghp_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN (RSA|OPENSSH|EC|PGP) PRIVATE KEY",
)

EXIT_ERROR, EXIT_SECRET, EXIT_NON_TWIN, EXIT_OVERSIZE, EXIT_CONTROL = 1, 2, 3, 4, 5


def _git(home, *args):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", *args], cwd=home, capture_output=True, text=True, env=env)


def _branch(home):
    r = _git(home, "symbolic-ref", "--short", "-q", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def _upstream(home, branch):
    r = _git(home, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return f"origin/{branch}"


def _ahead_behind(home, branch, upstream):
    r = _git(home, "rev-list", "--left-right", "--count", f"{branch}...{upstream}")
    if r.returncode != 0:
        return None
    a, b = r.stdout.split()
    return int(a), int(b)


def _fmt_ab(ab):
    return "no upstream" if ab is None else f"ahead {ab[0]} behind {ab[1]}"


def _control(home):
    """-> (matched, total) over $home/secrets/*; (0, 0) when there is no such directory."""
    d = Path(home) / "secrets"
    if not d.is_dir():
        return (0, 0)
    files = [p for p in sorted(d.rglob("*")) if p.is_file()]
    regs = [re.compile(p) for p in SECRET_PATTERNS]
    matched = 0
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(rg.search(text) for rg in regs):
            matched += 1
    return (matched, len(files))


def _secret_hits(home):
    args = ["grep", "-I", "-l", "-E"]
    for p in SECRET_PATTERNS:
        args += ["-e", p]
    r = _git(home, *args)
    if r.returncode == 1:
        return []
    if r.returncode != 0:
        raise RuntimeError(f"git grep failed: {r.stderr.strip()}")
    return [ln for ln in r.stdout.splitlines() if ln]


def _added_line_hits(home, rng):
    """Commits in `rng` whose ADDED lines match a secret pattern, so a key committed and
    then deleted inside the not-yet-pushed range is still caught. -> [short sha]."""
    r = _git(home, "log", "-p", "-U0", "--format=@@commit %H", rng)
    if r.returncode != 0:
        raise RuntimeError(f"git log -p {rng} failed: {r.stderr.strip()}")
    regs = [re.compile(p) for p in SECRET_PATTERNS]
    hits, cur = [], None
    for ln in r.stdout.splitlines():
        if ln.startswith("@@commit "):
            cur = ln[9:17]
        elif ln.startswith("+") and not ln.startswith("+++") and cur and cur not in hits:
            if any(rg.search(ln) for rg in regs):
                hits.append(cur)
    return hits


def _oversize_blobs(home, limit):
    revs = _git(home, "rev-list", "--objects", "--all")
    if revs.returncode != 0:
        raise RuntimeError(f"git rev-list failed: {revs.stderr.strip()}")
    r = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objecttype) %(objectname) %(objectsize) %(rest)"],
        cwd=home, input=revs.stdout, capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git cat-file failed: {r.stderr.strip()}")
    big = {}
    for ln in r.stdout.splitlines():
        parts = ln.split(" ", 3)
        if len(parts) >= 3 and parts[0] == "blob" and parts[2].isdigit() and int(parts[2]) > limit:
            big[parts[1]] = (int(parts[2]), parts[3] if len(parts) > 3 else "")
    return big


def _messages(home, rng):
    """-> list of (sha, full message) for the commits in `rng`."""
    r = _git(home, "log", "--format=%H%x00%B%x01", rng)
    if r.returncode != 0:
        raise RuntimeError(f"git log {rng} failed: {r.stderr.strip()}")
    out = []
    for chunk in r.stdout.split("\x01"):
        chunk = chunk.strip("\n")
        if "\x00" in chunk:
            sha, msg = chunk.split("\x00", 1)
            out.append((sha.strip(), msg.strip()))
    return out


def _write_status(home, result, stderr, sha, ab):
    path = Path(home) / STATUS_NAME
    prev = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        prev = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        pass
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = {
        "last_attempt_utc": now,
        "last_success_utc": now if result == "ok" else prev.get("last_success_utc"),
        "last_commit_sha": sha,
        "result": result,
        "stderr_tail": "" if result == "ok" else stderr.strip()[-300:],
        "ahead": ab[0] if ab else None,
        "behind": ab[1] if ab else None,
        "rewrite_guard": prev.get("rewrite_guard", "installed"),
    }
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def cmd_status(home) -> int:
    p = Path(home) / STATUS_NAME
    print(f"{STATUS_NAME}: " + (p.read_text(encoding="utf-8").rstrip() if p.exists() else "absent"))
    branch = _branch(home)
    if branch is None:
        print("git: detached HEAD or not a repository")
        return 0
    sb = _git(home, "status", "-sb")
    if sb.returncode == 0 and sb.stdout:
        print("git status -sb: " + sb.stdout.splitlines()[0])
    print("git: " + _fmt_ab(_ahead_behind(home, branch, _upstream(home, branch))))
    return 0


def cmd_repair(home, max_blob_bytes) -> int:
    branch = _branch(home)
    if branch is None:
        print("refusing: HEAD is not on a branch", file=sys.stderr)
        return EXIT_ERROR

    matched, total = _control(home)
    if total == 0:
        # Neither a pass nor a fail: with no key file to match, the patterns are unproven here.
        print("control: no key files under secrets/; pattern unproven on this seat")
    elif matched != total:
        print(f"control failed: pattern matched {matched} of {total} key files")
        return EXIT_CONTROL
    try:
        hits = _secret_hits(home)
        if hits:
            print("refusing: secret-shaped content in tracked files (contents not shown):")
            for h in hits:
                print(f"  {h}")
            return EXIT_SECRET
        big = _oversize_blobs(home, max_blob_bytes)
    except RuntimeError as e:
        print(f"refusing: {e}", file=sys.stderr)
        return EXIT_ERROR
    if big:
        print(f"refusing: tracked blob(s) over {max_blob_bytes} bytes (history is never rewritten here):")
        for sha, (size, path) in sorted(big.items(), key=lambda kv: kv[1][1]):
            print(f"  {path or sha} ({size} bytes)")
        return EXIT_OVERSIZE

    upstream = _upstream(home, branch)
    remote = upstream.split("/", 1)[0]
    f = _git(home, "fetch", remote)
    if f.returncode != 0:
        print(f"refusing: fetch failed: {f.stderr.strip()}", file=sys.stderr)
        return EXIT_ERROR
    try:
        remote_only = _messages(home, f"{branch}..{upstream}")
        local_msgs = {m for _, m in _messages(home, f"{upstream}..{branch}")}
    except RuntimeError as e:
        print(f"refusing: {e}", file=sys.stderr)
        return EXIT_ERROR
    non_twins = [(s, m) for s, m in remote_only if m not in local_msgs]
    if non_twins:
        print("refusing: remote-only commits that are not twins of a local commit:")
        for s, m in non_twins:
            print(f"  {s[:8]} {m.splitlines()[0] if m else ''}")
        return EXIT_NON_TWIN

    try:
        added = _added_line_hits(home, f"{upstream}..{branch}")
    except RuntimeError as e:
        print(f"refusing: {e}", file=sys.stderr)
        return EXIT_ERROR
    if added:
        print("refusing: secret-shaped content in lines added by not-yet-pushed commits "
              "(contents not shown; deleting the file later does not help):")
        for c in added:
            print(f"  commit {c}")
        return EXIT_SECRET

    ab = _ahead_behind(home, branch, upstream)
    sha = _git(home, "rev-parse", "HEAD").stdout.strip()
    if ab == (0, 0):
        print("nothing to repair: " + _fmt_ab(ab))
        return 0

    if ab is not None and ab[1] == 0:
        print("nothing diverged; plain push")
        push = _git(home, "push", remote, branch)
    else:
        push = _git(home, "push", f"--force-with-lease={branch}:{upstream}", remote, branch)
    _git(home, "fetch", remote)
    ab = _ahead_behind(home, branch, upstream)
    if push.returncode != 0:
        low = push.stderr.lower()
        result = "refused" if ("stale info" in low or "[rejected]" in low or "non-fast-forward" in low) else "error"
        _write_status(home, result, push.stderr, sha, ab)
        print(f"push failed ({result}): {push.stderr.strip()[-300:]}", file=sys.stderr)
        print(_fmt_ab(ab))
        return EXIT_ERROR
    _write_status(home, "ok", "", sha, ab)
    print("repaired: " + _fmt_ab(ab))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read or explicitly repair a diverged off-disk backup remote (#4205).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true", help="print the push status file and live ahead/behind")
    g.add_argument("--repair", action="store_true", help="force-push with lease, only if every check passes")
    ap.add_argument("--home", help="ENGRAM home (default: $ENGRAM_HOME, else ~/.engram)")
    ap.add_argument("--max-blob-bytes", type=int, default=DEFAULT_MAX_BLOB_BYTES,
                    help="refuse when a tracked blob is larger than this (default 100 MB)")
    args = ap.parse_args(argv)
    home = args.home or str(owner_read.engram_home())
    if not (Path(home) / ".git").exists():
        print(f"not a git repository: {home}", file=sys.stderr)
        return EXIT_ERROR
    return cmd_status(home) if args.status else cmd_repair(home, args.max_blob_bytes)


if __name__ == "__main__":
    sys.exit(main())
