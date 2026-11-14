#!/usr/bin/env python3
"""worktree-gc — garbage-collect finished fairy worktrees.

Fairy worktrees (`.claude/worktrees/agent-*`) accumulate because the Agent
tool only auto-removes a worktree it left UNCHANGED; any coder-fairy commits,
so its worktree is "changed" and persists indefinitely. Reviewer fairies leave
detached-HEAD / tmp-branch worktrees too. Nothing removes them once the PR
merges. (And `git merge-base --is-ancestor` can't detect squash-merges, so
git-ancestry is the wrong prune signal — GitHub PR state is the right one.)

This tool classifies each worktree and removes the safely-finished ones:

  PRUNE   branch worktree whose PR is MERGED or CLOSED (gh), worktree clean,
          AND no live process has its CWD inside it (fairy not still running)
  PRUNE   detached-HEAD worktree, clean, whose head SHA matches NO open PR
          (reviewer-fairy leftover; nothing to lose)
  PRUNE   `worktree-agent-*` / `review-*` tmp-branch worktree, clean
  KEEP    the worktree the deploy record names (`deployed_from` in
          `<ENGRAM_HOME>/.deployed-version`): the upgrade flow's source tree,
          load-bearing though it looks like a leftover (#4157); checked first
          on every path that would otherwise prune it
  KEEP    branch worktree whose PR is OPEN (active work)
  KEEP    branch worktree with NO PR (pre-PR or abandoned — flagged for a human)
  SKIP    an AGENT-OWNED worktree (branch `worktree-agent-*` / `review-*`, or any
          worktree under `<main>/.claude/worktrees/`) whose newest git-metadata
          activity is younger than the age guard (6h; --age-guard-seconds), or
          whose age cannot be read — a running fairy is not a process holding a
          CWD, so /proc cannot see it (#2808)
  SKIP    any worktree that is DIRTY (uncommitted changes) — never destroy work
  SKIP    any worktree that has a live process with CWD inside it (in-use guard)
  SKIP    detached-HEAD worktree whose head SHA is an OPEN PR's head (a
          reviewer's `refs/pr/<N>` checkout), or whose open-PR state could not
          be verified (gh unavailable) — never prune on unknown

Default is DRY-RUN: it prints the plan and changes nothing. Pass --apply to
execute (`git worktree remove` + delete the now-merged local branch, then
`git worktree prune` + delete orphan `worktree-agent-*` branch refs).

Safety invariants:
  * The main checkout is never touched.
  * A dirty worktree is never removed (clean-status gate), even if its PR merged.
  * An in-use worktree is never removed (in-use guard: /proc/*/cwd scan) — this
    is checked for EVERY prune path, including the PR-state (MERGED/CLOSED)
    path, not just the detached-HEAD and worktree-agent-*/review-* branch
    cases. Without it, a worktree whose PR just merged while its fairy is
    still writing to disk could be reaped out from under it — see
    classify() below, where the check runs before the MERGED/CLOSED PRUNE
    return.
  * A young agent-owned worktree is never removed, on EVERY prune path
    (detached-HEAD, tmp-branch, MERGED/CLOSED PR); an unreadable age keeps it
    (fail closed). Age = newest mtime of HEAD / index / logs/HEAD in the
    worktree's own admin dir under the main .git. Only a missing directory
    (stale admin record) is exempt.
  * An OPEN-PR worktree is never removed.
  * A no-PR branch is never auto-removed (could be unpushed/abandoned work) —
    only reported, so a human decides.

Usage:
    python tools/worktree-gc.py            # dry-run (default)
    python tools/worktree-gc.py --apply    # actually remove
    python tools/worktree-gc.py --age-guard-seconds 3600   # override the 6h guard
    python tools/worktree-gc.py --json     # machine-readable plan
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# The ONE ENGRAM-home resolver for tools lives in tools/_owner_read.py (#3863);
# resolve the package root through the symlink so `from tools import ...` works
# run as a script, loaded by path, or from the flattened plugin layout (same
# shim as deference_baseline.py).
_PKG_ROOT = str(Path(__file__).resolve().parent.parent)
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from tools import _owner_read as owner_read  # noqa: E402

# #2808: an agent-owned worktree younger than this is never pruned. The /proc
# in-use scan cannot see a running fairy (it is not a process holding a CWD).
AGE_GUARD_SECONDS = 6 * 3600


def _run(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _repo_root() -> str:
    rc, out, _ = _run(["git", "rev-parse", "--show-toplevel"])
    if rc != 0:
        print("worktree-gc: not inside a git repository", file=sys.stderr)
        sys.exit(2)
    return out.strip()


def _list_worktrees(root: str) -> list[dict]:
    """Parse `git worktree list --porcelain` into dicts.

    Each: {path, head, branch (or None for detached), is_main}.
    """
    rc, out, err = _run(["git", "worktree", "list", "--porcelain"], cwd=root)
    if rc != 0:
        print(f"worktree-gc: git worktree list failed: {err}", file=sys.stderr)
        sys.exit(2)
    trees: list[dict] = []
    cur: dict = {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            if cur:
                trees.append(cur)
            cur = {"path": line[len("worktree "):], "branch": None, "detached": False}
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            cur["branch"] = ref.replace("refs/heads/", "")
        elif line.strip() == "detached":
            cur["detached"] = True
    if cur:
        trees.append(cur)
    # First entry is always the main worktree.
    for i, t in enumerate(trees):
        t["is_main"] = (i == 0)
    return trees


def _is_clean(path: str) -> bool:
    rc, out, _ = _run(["git", "--no-optional-locks", "-C", path, "status", "--porcelain"])
    # Untracked-only is still "has stuff" — but fairy worktrees commit their work,
    # so any porcelain output (tracked or untracked) means don't auto-remove.
    return rc == 0 and out.strip() == ""


def _deploy_source() -> str | None:
    """The realpath of the directory `<ENGRAM_HOME>/.deployed-version` names as
    `deployed_from`, or None when it cannot be read (#4157).

    The upgrade flow uses that directory as the SOURCE_DIR of the next upgrade,
    so a worktree there is load-bearing even though it looks like a prunable
    leftover. Fails open to None (today's classification) on a missing,
    unreadable or malformed record, an empty value, or an unresolvable home.
    """
    try:
        record = owner_read.engram_home() / ".deployed-version"
        with open(record, encoding="utf-8") as f:
            for line in f:
                if line.startswith("deployed_from="):
                    value = line.strip().partition("=")[2].strip()
                    if value:
                        return os.path.realpath(value)
    except (OSError, ValueError):  # UnicodeDecodeError is a ValueError
        return None
    return None


def _is_in_use(path: str) -> bool:
    """Return True if any live process has its CWD inside this worktree directory.

    Scans /proc/*/cwd symlinks (Linux only). Falls back to False on non-Linux
    or permission errors — safe to call unconditionally; the worst case is we
    fail to detect an in-use worktree and proceed to the PR-state check.
    """
    proc = Path("/proc")
    if not proc.exists():
        return False
    resolved = Path(path).resolve()
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        cwd_link = pid_dir / "cwd"
        try:
            cwd = Path(os.readlink(str(cwd_link))).resolve()
            if cwd == resolved or str(cwd).startswith(str(resolved) + "/"):
                return True
        except (OSError, PermissionError):
            continue
    return False


def _main_checkout_path(path: str) -> str | None:
    """The main checkout's directory, derived from a worktree's own `.git`
    file (`gitdir: <main>/.git/worktrees/<name>`): the admin dir's common dir
    (its `commondir` file, else two levels up) is `<main>/.git`, and the main
    checkout is its parent. Reads files only (no subprocess). None if it cannot
    be derived (no `.git` file, unreadable, or a common dir not named `.git`).
    """
    try:
        line = (Path(path) / ".git").read_text().strip()
        if not line.startswith("gitdir:"):
            return None
        admin = Path(line[len("gitdir:"):].strip())
        if not admin.is_absolute():
            admin = Path(path) / admin
        cd = admin / "commondir"
        common = (admin / cd.read_text().strip()) if cd.is_file() else admin.parent.parent
        common = common.resolve()
    except (OSError, ValueError):
        return None
    if common.name != ".git":
        return None
    return str(common.parent)


def _is_agent_owned(t: dict, main_path: str | None) -> bool:
    """True for a fairy/reviewer worktree: tmp-branch name, or under
    `<main checkout>/.claude/worktrees/` whatever its branch (incl. detached).

    The folder is the MAIN checkout's, whatever directory the tool runs from.
    If `main_path` is None (it could not be derived) the under-folder test is
    False and only the branch-name rule applies; it never falls back to the
    run directory.
    """
    branch = t.get("branch")
    if branch and (branch.startswith("worktree-agent-") or branch.startswith("review-")):
        return True
    if not main_path:
        return False
    try:
        base = (Path(main_path) / ".claude" / "worktrees").resolve()
        Path(t["path"]).resolve().relative_to(base)
        return True
    except (ValueError, OSError):
        return False


def _worktree_age_seconds(path: str) -> float | None:
    """Seconds since the newest of HEAD / index / logs/HEAD in this worktree's
    admin dir (`.git/worktrees/<name>/`, found via the worktree's `.git` file).

    Returns None when the age cannot be read (fail closed: the caller keeps
    the worktree). A future mtime reads as age 0.
    """
    try:
        line = (Path(path) / ".git").read_text().strip()
        if not line.startswith("gitdir:"):
            return None
        admin = Path(line[len("gitdir:"):].strip())
        if not admin.is_absolute():
            admin = Path(path) / admin
        if not admin.is_dir():
            return None
        newest = None
        for rel in ("HEAD", "index", "logs/HEAD"):
            try:
                m = (admin / rel).stat().st_mtime
            except FileNotFoundError:
                continue
            newest = m if newest is None else max(newest, m)
        if newest is None:
            return None
        return max(0.0, time.time() - newest)
    except (OSError, ValueError):
        return None


def _fmt_guard(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _pr_state(branch: str, root: str) -> str | None:
    """Return 'MERGED'/'CLOSED'/'OPEN' for the branch's PR, or None if no PR / gh unavailable."""
    rc, out, _ = _run(
        ["gh", "pr", "list", "--head", branch, "--state", "all",
         "--json", "number,state", "-q", ".[0].state"],
        cwd=root,
    )
    if rc != 0:
        return None
    s = out.strip()
    return s or None


# Cache of {headRefOid: number} for every OPEN PR, keyed by repo root. main()
# classifies many worktrees and the open-PR set is identical for all of them,
# so this keeps the detached-HEAD lookup to one `gh` call per run rather than
# one per worktree. A FAILED lookup is never cached; the next caller retries.
_OPEN_PR_HEADS: dict[str, dict[str, int]] = {}


def _pr_for_sha(sha: str, root: str) -> tuple[bool, int | None]:
    """Return (verified, pr_number) for an OPEN PR whose head commit is `sha`.

    Three states, deliberately NOT collapsed (unlike _pr_state, whose `None`
    conflates "no PR" with "gh unavailable" — tolerable for a KEEP/PRUNE
    decision on a named branch, wrong for a delete verb on a bare SHA):
      * (True, N)     -> open PR #N has this head SHA
      * (True, None)  -> verified: no open PR has this head SHA
      * (False, None) -> the lookup could not be made (gh missing or errored);
                         the caller must not prune on this.
    """
    if not sha:
        return False, None
    heads = _OPEN_PR_HEADS.get(root)
    if heads is None:
        try:
            rc, out, _ = _run(
                ["gh", "pr", "list", "--state", "open", "--limit", "1000",
                 "--json", "number,headRefOid"],
                cwd=root,
            )
        except OSError:
            return False, None
        if rc != 0:
            return False, None
        if not out.strip():
            # rc == 0 with no output is not an affirmative "no open PR" — the
            # answer is unknown, and a delete verb must fail toward keeping.
            return False, None
        try:
            prs = json.loads(out)
        except ValueError:
            return False, None
        if not isinstance(prs, list):
            # An abnormal payload (object / scalar) is not the list
            # `gh pr list` emits; treat it as unknown, never as "no open PR".
            return False, None
        heads = {}
        for pr in prs:
            head_oid = pr.get("headRefOid") if isinstance(pr, dict) else None
            number = pr.get("number") if isinstance(pr, dict) else None
            if not (isinstance(head_oid, str) and head_oid
                    and isinstance(number, int) and not isinstance(number, bool)):
                # An entry we cannot read makes the whole answer UNKNOWN, not
                # "no match". Skipping such entries would be the dangerous
                # default: if `--json` ever loses headRefOid every entry lands
                # here, `heads` stays empty, and the caller is told "verified:
                # no open PR" — reproducing the bug this function exists to
                # prevent. A delete verb must fail toward keeping.
                return False, None
            heads[head_oid] = number
        _OPEN_PR_HEADS[root] = heads
    return True, heads.get(sha)


def classify(t: dict, root: str, *, main_path: str | None = None,
             age_guard_seconds: int = AGE_GUARD_SECONDS) -> tuple[str, str]:
    """Return (action, reason). action in {PRUNE, KEEP, SKIP, MAIN}.

    `main_path` is the main checkout (the `.claude/worktrees/` folder the age
    guard tests is ITS, not `root`'s); when None it is derived from the
    worktree's own git common dir.
    """
    if t.get("is_main"):
        return "MAIN", "main checkout — never touched"
    path = t["path"]
    if not Path(path).exists():
        return "PRUNE", "worktree directory missing (stale admin record)"
    # Deploy source (#4157): before ANY prune return. The upgrade flow reads
    # `deployed_from` and uses that directory as the next upgrade's source tree.
    deploy_source = _deploy_source()
    if deploy_source is not None and os.path.realpath(path) == deploy_source:
        return "KEEP", "deploy source — the deploy record's deployed_from; the next upgrade needs it"
    if not _is_clean(path):
        return "SKIP", "DIRTY — uncommitted changes; not removing"
    branch = t.get("branch")
    # Age guard (#2808): before ANY prune return, for agent-owned worktrees.
    if main_path is None:
        main_path = _main_checkout_path(path)
    if _is_agent_owned(t, main_path):
        age = _worktree_age_seconds(path)
        if age is None:
            return "SKIP", "agent worktree age could not be read; not pruning (fail closed)"
        if age < age_guard_seconds:
            return "SKIP", (f"young agent worktree (age {round(age / 60)}m < "
                            f"{_fmt_guard(age_guard_seconds)} age guard); not pruning")
    if branch is None:  # detached HEAD — reviewer-fairy leftover (unless still running)
        if _is_in_use(path):
            return "SKIP", "detached HEAD but process active (reviewer-fairy still running)"
        # A detached HEAD is often a reviewer's `git worktree add --detach …
        # refs/pr/<N>` checkout, so a clean detached worktree is NOT
        # automatically safe to prune: it may hold an OPEN PR's head. Prune
        # only when the open-PR lookup ran successfully AND no open PR points
        # at this SHA. If the lookup failed we cannot tell, and a delete verb
        # must fail toward keeping.
        try:
            verified, pr_number = _pr_for_sha(t.get("head") or "", root)
        except Exception:  # gh unavailable / subprocess error / unexpected output
            verified, pr_number = False, None
        if not verified:
            return "SKIP", "detached HEAD but open-PR state unverified (gh lookup failed); not pruning"
        if pr_number is not None:
            return "SKIP", f"detached HEAD matches OPEN PR #{pr_number}; not pruning"
        return "PRUNE", "detached HEAD, clean (reviewer-fairy leftover)"
    if branch.startswith("worktree-agent-") or branch.startswith("review-"):
        if _is_in_use(path):
            return "SKIP", f"tmp branch '{branch}' but process active (fairy still running)"
        return "PRUNE", f"tmp review branch '{branch}', clean"
    state = _pr_state(branch, root)
    if state in ("MERGED", "CLOSED"):
        # A merged/closed PR does not mean the fairy that owned this worktree
        # has finished with it — post-merge cleanup or a follow-up commit on a
        # closed PR's branch can still be running here, so the in-use guard
        # applies to this path too, exactly as it does for the detached-HEAD
        # and worktree-agent-*/review-* cases above.
        if _is_in_use(path):
            return "SKIP", f"PR {state.lower()} ({branch}) but process active (fairy still running)"
        return "PRUNE", f"PR {state.lower()} ({branch})"
    if state == "OPEN":
        return "KEEP", f"PR OPEN ({branch}) — active work"
    return "KEEP", f"no PR for '{branch}' — flagged for human review (pre-PR or abandoned)"


def _prune_orphan_agent_refs(root: str, checked_out: set[str]) -> tuple[int, int]:
    """Delete worktree-agent-* branch refs not checked out in any remaining worktree.

    Returns (pruned, total).
    """
    rc, out, _ = _run(["git", "branch", "--list", "worktree-agent-*"], cwd=root)
    if rc != 0 or not out.strip():
        return 0, 0
    refs = [b.strip().removeprefix("* ") for b in out.splitlines() if b.strip()]
    orphans = [r for r in refs if r not in checked_out]
    pruned = sum(
        1 for ref in orphans
        if _run(["git", "branch", "-D", ref], cwd=root)[0] == 0
    )
    return pruned, len(orphans)


def remove_worktree(t: dict, root: str) -> tuple[bool, str]:
    path = t["path"]
    rc, _, err = _run(["git", "worktree", "remove", "--force", path], cwd=root)
    if rc != 0:
        return False, f"remove failed: {err.strip()}"
    branch = t.get("branch")
    msg = "worktree removed"
    # Delete the now-finished local branch (merged/closed PR or tmp). -D because
    # squash-merge leaves it not-an-ancestor; the PR-state gate already vouched it.
    if branch:
        rc2, _, err2 = _run(["git", "branch", "-D", branch], cwd=root)
        msg += f"; branch -D {branch}" + ("" if rc2 == 0 else f" (failed: {err2.strip()})")
    return True, msg


def main() -> int:
    ap = argparse.ArgumentParser(prog="worktree-gc", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually remove (default: dry-run)")
    ap.add_argument("--json", action="store_true", help="machine-readable plan")
    ap.add_argument("--age-guard-seconds", type=int, default=AGE_GUARD_SECONDS,
                    help="never prune an agent-owned worktree younger than this "
                         f"(default {AGE_GUARD_SECONDS}s)")
    args = ap.parse_args()

    root = _repo_root()
    trees = _list_worktrees(root)
    # The main checkout is trees[0] (is_main), NOT `root`: run from inside a
    # linked worktree, `--show-toplevel` is that worktree.
    main_path = trees[0]["path"] if trees else None

    plan = []
    for t in trees:
        action, reason = classify(t, root, main_path=main_path,
                                  age_guard_seconds=args.age_guard_seconds)
        plan.append({"path": t["path"], "branch": t.get("branch"),
                     "action": action, "reason": reason})

    prune = [p for p in plan if p["action"] == "PRUNE"]
    keep = [p for p in plan if p["action"] == "KEEP"]
    skip = [p for p in plan if p["action"] == "SKIP"]

    if args.json:
        print(json.dumps({"plan": plan, "counts": {
            "prune": len(prune), "keep": len(keep), "skip": len(skip)}}, indent=2))
        if not args.apply:
            return 0

    if not args.json:
        def short(path: str) -> str:
            return path.replace(root + "/", "")
        print(f"worktree-gc: {len(trees)} worktrees "
              f"({len(prune)} prune, {len(keep)} keep, {len(skip)} skip-dirty)\n")
        for p in prune:
            print(f"  PRUNE  {short(p['path'])}  — {p['reason']}")
        for p in skip:
            print(f"  SKIP   {short(p['path'])}  — {p['reason']}")
        for p in keep:
            print(f"  keep   {short(p['path'])}  — {p['reason']}")
        if not args.apply:
            print(f"\nDRY-RUN — nothing removed. Re-run with --apply to remove the "
                  f"{len(prune)} PRUNE entries.")
            return 0

    # --apply
    removed = 0
    for t in trees:
        match = next((p for p in plan if p["path"] == t["path"]), None)
        if not match or match["action"] != "PRUNE":
            continue
        ok, msg = remove_worktree(t, root)
        print(f"  {'✓' if ok else '✗'} {t['path']} — {msg}")
        removed += ok
    _run(["git", "worktree", "prune"], cwd=root)
    remaining = _list_worktrees(root)
    checked_out = {t.get("branch") for t in remaining if t.get("branch")}
    pruned_refs, total_refs = _prune_orphan_agent_refs(root, checked_out)
    ref_note = f"; pruned {pruned_refs}/{total_refs} orphan worktree-agent-* refs" if total_refs else ""
    print(f"\nworktree-gc: removed {removed}/{len(prune)}; ran git worktree prune{ref_note}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
