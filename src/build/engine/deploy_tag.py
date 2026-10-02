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
Fetching stays a separate concern from this pure predicate. The shared
``refresh_and_check_deployable_tag_at()`` wrapper owns bounded refresh for
every caller, including the shell script. This keeps the predicate itself
simple to test without network mocking.

``refresh_and_check_deployable_tag_at()`` is the convenience wrapper every
engine and shell call site uses. It owns a private POSIX process group for a
bounded fetch and falls through to the pure predicate regardless of whether
the fetch succeeded.  A failed refresh therefore authorizes only against
LOCAL cached remote-tracking refs: it does not prove current remote history,
and a remote force-rewind after the last successful fetch is not observable
here.  The wrapper emits a warning whenever it relies on that limitation.
"""

from __future__ import annotations

import math
import numbers
import os
import re
import signal
import subprocess
import sys

# Same regex the shell script uses (`grep -E '^(v|deploy/)'`) and the same
# pattern test_health_version_gate.py's deployable_tags() reference function
# describes (refs/tags/v* UNION refs/tags/deploy/*) — but this predicate
# enumerates tags AT HEAD (`git tag --points-at`), never the whole-repo
# `for-each-ref` scan deployable_tags() uses to find the max version. That
# function is a read-only reference for the PATTERN, not a function to call
# from here — it scans every tag in the repo, which is the wrong shape for
# "what's deployable right now, at this commit."
_DEPLOYABLE_TAG_PATTERN = re.compile(r"^(v|deploy/)")
_TIMEOUT_PATTERN = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)([smhd]?)$")
_TIMEOUT_MULTIPLIERS = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def _invalid_timeout() -> ValueError:
    return ValueError("invalid timeout: must be a positive finite number")


def _validate_timeout(timeout: float) -> float:
    """Return a positive finite numeric timeout or fail before starting git."""
    if isinstance(timeout, bool) or not isinstance(timeout, numbers.Real):
        raise _invalid_timeout()
    seconds = float(timeout)
    if not math.isfinite(seconds) or seconds <= 0:
        raise _invalid_timeout()
    return seconds


def parse_timeout_seconds(value: str) -> float:
    """Parse shell timeout input: positive decimal seconds with s/m/h/d.

    This intentionally accepts the useful single-unit GNU ``timeout`` syntax
    but not compound durations (for example ``1h30m``).
    """
    if not isinstance(value, str):
        raise _invalid_timeout()
    match = _TIMEOUT_PATTERN.fullmatch(value)
    if match is None:
        raise _invalid_timeout()
    seconds = float(value[:-1] if match.group(1) else value) * _TIMEOUT_MULTIPLIERS[
        match.group(1)
    ]
    # A huge finite decimal can become infinity after its unit conversion.
    if not math.isfinite(seconds) or seconds <= 0:
        raise _invalid_timeout()
    return seconds


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

    Performs NO fetching — callers use
    ``refresh_and_check_deployable_tag_at`` when they need a bounded refresh.
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
    """Fetch ``origin dev main`` in a private POSIX group, then call
    ``deployable_tag_at()`` against whatever local refs now exist.

    Used by the engine call sites (flows.py's ``_apply_marketplace_rebuilt``,
    cli.py's ``build_command``) and the installer shell wrapper.

    A non-zero fetch, timeout, missing ``origin``, or launch failure emits one
    bounded stderr warning and falls through to the predicate.  That verdict
    rests on LOCAL cached refs, whose current remote freshness is unverified;
    it is not a claim that remote history is current or cannot have rewound.
    A stale or absent ref may therefore reject a deployable tag, while a cached
    ref can still contain a commit later removed from the remote.

    Timeout cleanup is supported on Linux/macOS POSIX only.  The fetch gets a
    newly-created session/process group; on expiry the wrapper SIGKILLs that
    owned group and reaps its direct ``git`` child without waiting on pipes a
    descendant held.  This intentionally does not claim to reap descendants
    that escaped that newly-created group.
    """
    timeout_seconds = _validate_timeout(timeout)
    if os.name != "posix":
        raise RuntimeError("deploy-tag refresh timeout cleanup requires POSIX process groups")

    try:
        process = subprocess.Popen(
            ["git", "fetch", "--quiet", "origin", "dev", "main"],
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError:
        print(
            "deploy-tag: WARNING — refresh could not start; verdict uses LOCAL "
            "cached refs and current remote freshness is unverified.",
            file=sys.stderr,
        )
        return deployable_tag_at(repo_path, head)

    try:
        process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        # This process group was created by this call (start_new_session),
        # never discovered by process name or inherited from the caller.
        # One finally around the ENTIRE killpg+wait sequence: the capture
        # pipes must close on every exit from this block, including a
        # genuine killpg OSError and a wait() that itself times out -- not
        # only the wait() branch (#3133 review suggestion #1).
        try:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                raise RuntimeError(
                    "timed-out deploy-tag fetch cleanup failed to terminate owned process group"
                ) from exc
            try:
                # Reap direct git without another communicate(): a surviving
                # escaped descendant could retain a capture pipe indefinitely.
                process.wait(timeout=1)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("timed-out deploy-tag fetch could not reap git") from exc
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        print(
            "deploy-tag: WARNING — refresh timed out; verdict uses LOCAL "
            "cached refs and current remote freshness is unverified.",
            file=sys.stderr,
        )
    else:
        if process.returncode != 0:
            print(
                "deploy-tag: WARNING — refresh failed; verdict uses LOCAL "
                "cached refs and current remote freshness is unverified.",
                file=sys.stderr,
            )

    return deployable_tag_at(repo_path, head)
