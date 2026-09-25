"""src.build.engine.deploy_tag — the shared deploy-tag predicate (#3092).

One Python predicate, called by BOTH the engine (flows.py's
``_apply_marketplace_rebuilt`` and cli.py's ``build_command``) AND
``tools/install-local-marketplace.sh`` (via a ``python3 -c`` one-liner), so
the two paths cannot drift on what counts as "a deployable tag at HEAD"
ever again. Before this module existed, the shell script enforced "refuse
an untagged HEAD unless --allow-untagged" (#2586/#2711) while the engine
enforced nothing of the kind — an untagged ``dev`` tip could upgrade fine
through the engine while the shell script would have refused it.

Design decision (do not re-derive — this is the resolved ambiguity from
issue #3092): ``deployable_tag_at()`` is a PURE, network-free,
unit-testable function. It reads local git state only (current tags,
current ``origin/dev``/``origin/main`` refs) and performs NO fetching.
Fetching stays a separate concern, done by each *caller* before invoking
the predicate — exactly as the shell script already separates these two
things today (its own fetch-with-timeout preamble, then the
points-at/ancestor check). This keeps the predicate itself simple to test
(no network mocking needed) and keeps the shell script's existing
bash-specific timeout/``gtimeout``-detection machinery untouched.

``refresh_and_check_deployable_tag_at()`` is the convenience wrapper the
TWO ENGINE call sites use (flows.py / cli.py) — NOT the shell script, which
keeps its own existing bash fetch preamble unchanged and calls
``deployable_tag_at()`` directly after it (see
install-local-marketplace.sh). The wrapper fetches with a timeout via
Python's own ``subprocess.run(timeout=...)`` — deliberately NOT a
reimplementation of bash's ``timeout``/``gtimeout`` binary-detection dance,
since Python's stdlib already gives a clean, portable timeout — and falls
through to the pure predicate regardless of whether the fetch succeeded.
This is fail-closed by construction: a stale/absent ``origin/dev`` ref
makes the ancestry check either correctly pass (if the ref happens to
already be current enough) or correctly fail closed by returning ``None``
— it never fails open.
"""

from __future__ import annotations

import re
import subprocess

# Same regex the shell script uses (`grep -E '^(v|deploy/)'`) and the same
# pattern test_health_version_gate.py's deployable_tags() reference function
# describes (refs/tags/v* UNION refs/tags/deploy/*) — but this predicate
# enumerates tags AT HEAD (`git tag --points-at`), never the whole-repo
# `for-each-ref` scan deployable_tags() uses to find the max version. That
# function is a read-only reference for the PATTERN, not a function to call
# from here — it scans every tag in the repo, which is the wrong shape for
# "what's deployable right now, at this commit."
_DEPLOYABLE_TAG_PATTERN = re.compile(r"^(v|deploy/)")


def _run_git(args: list[str], repo_path: str) -> subprocess.CompletedProcess:
    """Run a git command in repo_path; return CompletedProcess (check=False)."""
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )


def deployable_tag_at(repo_path: str, head: str = "HEAD") -> "str | None":
    """Return the deployable tag(s) at ``head``, or ``None`` if not deployable.

    A commit is "deployable" when BOTH hold:
      1. One or more tags matching ``^(v|deploy/)`` point at ``head``
         (``git tag --points-at <head>``, filtered — never
         ``for-each-ref``/``deployable_tags()``, which scans the whole
         repo for the max version rather than what's at THIS commit).
      2. ``head`` is an ancestor of ``origin/dev`` OR ``origin/main``
         (``git merge-base --is-ancestor``, which dereferences an
         annotated tag to its commit automatically — no separate
         tag-vs-commit handling needed; the ancestry check always uses
         ``head`` itself, never a bare tag name, so it stays unambiguous
         when several tags point at the same commit).

    A tag matching the pattern but on a commit that is NOT an ancestor of
    either ref is an UNMERGED local tag — the guard's whole point is that a
    same-named tag must not bypass it on unmerged work, so this returns
    ``None`` in that case, not the tag.

    If ``origin/dev``/``origin/main`` don't exist locally at all (e.g. a
    repo with no such remote-tracking refs configured), ``git merge-base
    --is-ancestor`` simply fails to resolve them and returns non-zero —
    treated here as "not an ancestor" (``None``), the correct fail-closed
    behavior for an absent/stale ref, never a raised exception.

    Multiple matching tags are joined by ``,`` in SORTED order — mirrors
    the shell script's ``ALPHA_TAG`` (its own ``paste -sd, -`` of
    ``git tag --points-at``'s output), but deterministic rather than
    git's arbitrary tag-iteration order. A small, deliberate improvement
    over the shell's own unsorted behavior.

    Performs NO fetching — see the module docstring's design-decision
    note. Callers that need fresh remote-tracking refs must fetch first
    (the shell script's own preamble, or ``refresh_and_check_deployable_tag_at``
    below for the two engine call sites).
    """
    points_at = _run_git(["tag", "--points-at", head], repo_path)
    if points_at.returncode != 0:
        return None

    candidates = sorted(
        line.strip()
        for line in points_at.stdout.splitlines()
        if line.strip() and _DEPLOYABLE_TAG_PATTERN.match(line.strip())
    )
    if not candidates:
        return None

    is_ancestor = False
    for ref in ("origin/dev", "origin/main"):
        check = _run_git(["merge-base", "--is-ancestor", head, ref], repo_path)
        if check.returncode == 0:
            is_ancestor = True
            break

    if not is_ancestor:
        return None

    return ",".join(candidates)


def refresh_and_check_deployable_tag_at(
    repo_path: str, head: str = "HEAD", timeout: float = 15.0
) -> "str | None":
    """Fetch ``origin dev main`` (bounded by ``timeout``), then call
    ``deployable_tag_at()`` against whatever local refs now exist.

    Used by the TWO ENGINE call sites (flows.py's
    ``_apply_marketplace_rebuilt``, cli.py's ``build_command``) — NOT the
    shell script, which keeps its own existing bash fetch preamble
    unchanged and calls ``deployable_tag_at()`` directly (see the module
    docstring).

    On any fetch failure (non-zero exit, ``TimeoutExpired``, ``origin``
    remote missing, ...) this logs nothing itself and simply falls through
    to calling ``deployable_tag_at()`` anyway against whatever local refs
    currently exist. This is fail-closed by construction: a stale/absent
    ``origin/dev`` ref makes the ancestry check either correctly pass (the
    ref happens to already be current enough) or correctly fail closed by
    returning ``None`` — it can never fail OPEN, since a nonexistent or
    genuinely-behind ref cannot manufacture a false ancestry match.

    Deliberately uses ``subprocess.run(timeout=...)`` rather than
    reimplementing bash's ``timeout``/``gtimeout`` binary-detection dance
    — Python's stdlib already gives a clean, portable timeout, so there's
    nothing to port here.
    """
    try:
        subprocess.run(
            ["git", "fetch", "--quiet", "origin", "dev", "main"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        # Fetch failed to even run/complete — fall through to the pure
        # predicate against whatever local refs currently exist. Fail-closed
        # by construction (see docstring above); never crash the caller.
        pass

    return deployable_tag_at(repo_path, head)
