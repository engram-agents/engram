#!/usr/bin/env python3
"""worktree-gc — garbage-collect finished fairy worktrees.

Fairy worktrees (`.claude/worktrees/agent-*`) accumulate because the Agent
tool only auto-removes a worktree it left UNCHANGED; any coder-fairy commits,
so its worktree is "changed" and persists indefinitely. Reviewer fairies leave
detached-HEAD / tmp-branch worktrees too. Nothing removes them once the PR
merges. (And `git merge-base --is-ancestor` can't detect squash-merges, so
git-ancestry is the wrong prune signal — GitHub PR state is the right one.)

This tool classifies each worktree and removes the safely-finished ones:

  PRUNE   branch worktree whose PR is MERGED or CLOSED (gh), worktree clean
  PRUNE   detached-HEAD worktree, clean (reviewer-fairy leftover; nothing to lose)
  PRUNE   `worktree-agent-*` / `review-*` tmp-branch worktree, clean
  KEEP    branch worktree whose PR is OPEN (active work)
  KEEP    branch worktree with NO PR (pre-PR or abandoned — flagged for a human)
  SKIP    any worktree that is DIRTY (uncommitted changes) — never destroy work

Default is DRY-RUN: it prints the plan and changes nothing. Pass --apply to
execute (`git worktree remove` + delete the now-merged local branch, then
`git worktree prune`).

Safety invariants:
  * The main checkout is never touched.
  * A dirty worktree is never removed (clean-status gate), even if its PR merged.
  * An OPEN-PR worktree is never removed.
  * A no-PR branch is never auto-removed (could be unpushed/abandoned work) —
    only reported, so a human decides.

Usage:
    python tools/worktree-gc.py            # dry-run (default)
    python tools/worktree-gc.py --apply    # actually remove
    python tools/worktree-gc.py --json     # machine-readable plan
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


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
    rc, out, _ = _run(["git", "-C", path, "status", "--porcelain"])
    # Untracked-only is still "has stuff" — but fairy worktrees commit their work,
    # so any porcelain output (tracked or untracked) means don't auto-remove.
    return rc == 0 and out.strip() == ""


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


def classify(t: dict, root: str) -> tuple[str, str]:
    """Return (action, reason). action in {PRUNE, KEEP, SKIP, MAIN}."""
    if t.get("is_main"):
        return "MAIN", "main checkout — never touched"
    path = t["path"]
    if not Path(path).exists():
        return "PRUNE", "worktree directory missing (stale admin record)"
    if not _is_clean(path):
        return "SKIP", "DIRTY — uncommitted changes; not removing"
    branch = t.get("branch")
    if branch is None:  # detached HEAD — reviewer-fairy leftover
        return "PRUNE", "detached HEAD, clean (reviewer-fairy leftover)"
    if branch.startswith("worktree-agent-") or branch.startswith("review-"):
        return "PRUNE", f"tmp review branch '{branch}', clean"
    state = _pr_state(branch, root)
    if state in ("MERGED", "CLOSED"):
        return "PRUNE", f"PR {state.lower()} ({branch})"
    if state == "OPEN":
        return "KEEP", f"PR OPEN ({branch}) — active work"
    return "KEEP", f"no PR for '{branch}' — flagged for human review (pre-PR or abandoned)"


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
    args = ap.parse_args()

    root = _repo_root()
    trees = _list_worktrees(root)

    plan = []
    for t in trees:
        action, reason = classify(t, root)
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
    print(f"\nworktree-gc: removed {removed}/{len(prune)}; ran git worktree prune.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
