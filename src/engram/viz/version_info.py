"""viz.version_info -- startup-captured git version report (engram-alpha#2531).

The viz server (:5001) runs from a checkout that can move (a new commit
lands, a branch is switched) after the process starts. Agents reading the
live process have no way to tell which commit it actually LOADED. This
module captures that once, at process startup, from the ``viz`` package's
OWN on-disk location -- never a fixed ``parents[N]`` depth, because the
plugin build flattens paths differently than the ``src/`` source layout
(see this repo's CLAUDE.md, "Plugin build restructures code paths"). A git
WORKTREE's ``.git`` is a FILE (containing ``gitdir: ...``), not a
directory -- the root-walk below accepts either.

The walk is bounded by more than a stopping condition, though: a found
``.git`` marker is only ACCEPTED as the root if that repo actually TRACKS
the running code (verified via ``git ls-files --error-unmatch`` against the
real ``server.py`` living next to this module). On a real plugin install,
the ``viz`` package's own directory has no ``.git`` -- walking parents can
land on an unrelated ANCESTOR repo that merely encloses it (e.g.
``~/.engram``, the agent's own git-versioned graph-history store, which
gitignores ``marketplace/`` and does not track the plugin-flattened
``viz/`` tree living under it). Accepting that ancestor without checking
tracking would silently report a completely unrelated repo's sha/branch/
dirty state. When the found root does not track the code, the walk does
NOT retry further up the tree (an ancestor of an untracked path cannot
track it either) -- it returns the null triple with a dedicated
``"reason"`` instead.

``capture_version_info()`` is a pure function (given a start path, always
re-reads git fresh) -- it is meant to be called EXACTLY ONCE per server
process, at startup, with its result cached by the caller (see
``viz/server.py``'s module-level ``_VERSION_INFO`` / ``_VERSION_INFO_DIR``
and its ``/api/version`` route, which serves the cached value and never
calls this function per-request).

Never raises: every subprocess invocation is wrapped and any failure
(missing ``git`` binary, timeout, not a git checkout, or any other
``OSError``/``subprocess.SubprocessError``) degrades to a null-valued
result carrying a ``"reason"`` string, never an exception out of this
module.

PR #3174: the production viz server runs as OS user ``spt``, serving a
checkout (``/home/agents-shared/engram-alpha``) owned by a different user.
An earlier round of this module bypassed git's dubious-ownership refusal
with a per-call ``-c safe.directory=<root>`` override (plus
``core.fsmonitor=false`` / ``core.hooksPath=/dev/null`` as its companion) --
but that override reopens a config-driven-exec surface the neutralizer list
does not fully close: a repo-local ``filter.<x>.clean`` script still fires
under a plain ``git status --porcelain`` (measured on this host's git
2.43.0), bypass or no neutralizers. ``_run_git`` therefore does **not**
bypass the ownership check at all -- git's own protection governs, exactly
as it would for any other caller. Without the bypass, a repo owned by
another user is refused by git itself, so no repo-local config (clean/smudge
filters, fsmonitor, hooks) belonging to an untrusted owner is ever executed.
The production checkout's real serving user does not need the bypass either:
its own gitconfig already permits reading that checkout (measured against
the live ``:5001`` footer). See ``_run_git``'s own docstring for the full
reasoning, and ``_is_dubious_ownership`` for the path where ownership
genuinely blocks (a repo this process's user has not been granted access
to) -- that path is unchanged; only the bypass is gone.

PR #3174 (second half): this module is now ALSO the sole code-side git-root
walker for ``viz.footer``'s page-footer provenance label -- ``footer.py``
used to carry its own, independent, DEPTH-BOUNDED walk with no tracking
check, which is exactly what let it silently misattribute an unrelated
ANCESTOR repo's HEAD as this code's own provenance on a real plugin install
(measured: importing the installed plugin's ``footer.py`` and calling
``_code_provenance()`` returned ``~/.engram``'s HEAD, labeled ``(worktree)``,
because ``~/.engram`` encloses the plugin-flattened ``viz/`` tree under a
gitignored ``marketplace/``). ``footer._code_provenance()`` now calls
``capture_version_info()`` directly rather than re-implementing the walk;
when the result's ``sha`` is null because the code isn't tracked by any
enclosing repo (see ``reason``), footer falls back to the ``.deployed-version``
marker via ``read_deployed_version()`` below (moved here from ``footer.py``
so both callers share one reader with no circular import: this module never
imports ``footer``, so ``footer`` importing THIS module is one-directional).
``is_plugin_fallback_reason()`` is the shared predicate for which of this
module's null-``sha`` ``reason`` strings mean "genuinely no checkout, the
plugin marker is the right fallback" versus "there IS a checkout but git
couldn't read it" (dubious ownership / git command failure) -- the latter
renders ``unversioned`` rather than silently reporting a possibly-stale
plugin marker as if it were live code provenance.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# Short timeout -- this runs once at startup, but must never hang a server
# boot on a wedged git subprocess (e.g. an unreachable network filesystem).
_GIT_TIMEOUT_SECONDS = 3.0


def find_git_root(start: Path) -> "Path | None":
    """Walk `start` and its parents looking for a `.git` entry. Returns the
    directory containing it, or None if none is found before the
    filesystem root. `.git` may be a directory (an ordinary checkout) or a
    FILE (a git worktree's `.git` is a one-line pointer file) -- both count.
    Never assumes a fixed depth.

    NOTE: this only locates the nearest `.git` marker -- it does NOT verify
    that repo actually TRACKS the running code. On the real plugin-install
    topology, the viz package's own directory has no `.git` of its own, and
    walking parents can land on an unrelated ANCESTOR repo that merely
    encloses it (e.g. `~/.engram`, the agent's own git-versioned
    graph-history store, which gitignores `marketplace/` and so does not
    track the plugin-flattened `viz/` tree living under it). Callers MUST
    additionally verify tracking (see `_path_is_tracked` in
    `capture_version_info` below) before trusting a root this function
    returns."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _path_is_tracked(root: Path, path: Path) -> "tuple[bool | None, bool]":
    """Check whether `path` (an absolute path) is tracked by the git repo
    rooted at `root`, via `git ls-files --error-unmatch`. Returns a
    `(tracked, dubious_ownership)` pair:
      - `(True, False)` -- tracked.
      - `(False, False)` -- git ran fine but reported the path untracked
        (e.g. it's gitignored, or was simply never committed), or `path`
        isn't even under `root`.
      - `(None, False)` -- the git call itself failed for a reason OTHER
        than ownership -- missing binary, timeout, or any other subprocess
        error. Mapped by the caller to the existing git-unavailable null
        shape.
      - `(None, True)` -- the underlying `ls-files` call refused
        specifically on ownership grounds (see `_is_dubious_ownership`) --
        `_run_git` passes no `safe.directory` override, so this is the
        expected outcome whenever the running process's user does not own
        (or has not been granted `safe.directory` access to) the checkout.
        Mapped by the caller to its own, more specific reason string rather
        than the generic git-command-failed one."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False, False  # path isn't even under root -- can't be tracked by it
    proc = _run_git(["ls-files", "--error-unmatch", "--", str(rel)], root)
    if proc is None:
        return None, False
    if _is_dubious_ownership(proc):
        return None, True
    return proc.returncode == 0, False


_DUBIOUS_OWNERSHIP_REASON = (
    "git refused: dubious ownership (the server's user does not own this "
    "checkout; add it to that user's safe.directory)"
)


def _is_dubious_ownership(proc: "subprocess.CompletedProcess | None") -> bool:
    """True when `proc` is a completed (non-exception) git call that failed
    specifically because git refused the repo on ownership grounds --
    `_run_git` passes no override for this, so any checkout this process's
    user does not own (and has not been granted `safe.directory` access to)
    hits this path. Distinct from every other failure shape `_run_git` can
    produce: None on exception, or a non-zero exit for an unrelated
    reason."""
    return (
        proc is not None
        and proc.returncode != 0
        and "dubious ownership" in (proc.stderr or "")
    )


def _run_git(args: list[str], cwd: Path) -> "subprocess.CompletedProcess | None":
    """Run a git subcommand in `cwd`; return None (never raise) on ANY
    failure -- missing binary, timeout, or any other subprocess error.

    PR #3174 (second half): this deliberately does NOT bypass git's
    dubious-ownership check -- no `-c safe.directory=...` override, and
    (as its former companion) no `core.fsmonitor=false` / `core.hooksPath=
    /dev/null` neutralizers either. An earlier round of this module passed
    `-c safe.directory=<root>` per invocation (the production viz server
    runs as a different OS user than the checkout it serves, and git
    otherwise refuses to read a repo it doesn't own), with those two
    neutralizers as its guard against the config-driven-exec surface that
    bypassing ownership reopens. That guard was incomplete: a repo-local
    `filter.<x>.clean` script still runs under a plain `git status
    --porcelain` even with both neutralizers set (measured on this host's
    git 2.43.0 -- `core.fsmonitor` and `core.hooksPath` say nothing about
    clean/smudge filters). Patching each newly-found exec surface is an
    unbounded game; the sound fix is to never override the check at all.
    Without the override, a repo owned by another user is refused by git
    itself, so no repo-local config (clean/smudge filters, fsmonitor,
    hooks) of an untrusted owner is ever read, let alone executed. The
    production checkout's real serving user does not need the bypass
    either -- that user's own gitconfig already permits reading the
    checkout it serves (measured against the live `:5001` footer). See
    `_is_dubious_ownership` for the path taken when ownership genuinely
    blocks -- unchanged; only the bypass is gone."""
    root = str(Path(cwd).resolve())
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def capture_version_info(start_path: "str | Path | None" = None) -> dict:
    """Capture {sha, branch, dirty, started_at} for the git checkout
    containing `start_path` (default: this module's own directory -- i.e.
    the `viz` package's on-disk location).

    Returns one of three shapes:
      - git available, both branch + dirty resolved: {"sha": <full sha str>,
        "branch": <str | None>, "dirty": <bool>, "started_at":
        <ISO-8601 UTC str>[, "warning": <str> if branch not in (dev, main)
        or dirty]}
      - git available (sha resolved) but the ``rev-parse --abbrev-ref``
        and/or ``status --porcelain`` calls individually failed: same shape
        as above, except the failed field(s) are ``None`` (``branch``
        and/or ``dirty``) and a ``"reason"`` string names which call(s)
        failed. A branch-call failure is NOT the same as a detached HEAD --
        the latter is ``branch: None`` with no ``"reason"`` entry for it. A
        dirty-call failure also adds ``"dirty state unknown"`` to
        ``"warning"`` (never silently reads as a clean tree).
      - git unavailable entirely (not a checkout / binary missing / any
        subprocess error resolving the sha itself, OR a found root that
        does not actually TRACK the running code -- see `find_git_root`'s
        docstring, OR git refused the repo on OWNERSHIP grounds -- `_run_git`
        passes no `safe.directory` override, so this is git's own,
        un-bypassed protection): {"sha":
        None, "branch": None, "dirty": None, "started_at": <ISO-8601 UTC
        str>, "reason": <str>}. Three distinct ``"reason"`` texts, never
        conflated: the not-tracked case ("code is not tracked by the
        enclosing git repo (<root>) -- likely an installed plugin copy");
        the dubious-ownership case ("git refused: dubious ownership (the
        server's user does not own this checkout; add it to that user's
        safe.directory)" -- see `_is_dubious_ownership`); and the
        git-command-failure case (the tracking check's own ``ls-files``
        call failed for any OTHER reason -- missing binary, timeout, or any
        other subprocess error), which reuses the existing "git command
        failed" reason text, since from the caller's perspective it's the
        same git-unavailable situation.

    Never raises.
    """
    start = Path(start_path) if start_path is not None else Path(__file__).resolve().parent
    started_at = datetime.now(timezone.utc).isoformat()

    root = find_git_root(start)
    if root is None:
        return {
            "sha": None, "branch": None, "dirty": None,
            "started_at": started_at,
            "reason": "not a git checkout",
        }

    # engram-alpha#2531 reviewer round: a found root only counts if it
    # actually TRACKS the running code -- an ancestor of an untracked path
    # cannot track it either, so a rejection here does not retry further
    # up the tree (find_git_root already only ever returns the nearest
    # `.git` marker, so there is nothing further to retry against anyway).
    # `start` here plays the role of "the viz package's own location": in
    # the default (no start_path given) case it IS `Path(__file__).resolve()
    # .parent`, i.e. the real directory containing this module and its
    # real server.py; a caller who passes an explicit start_path is
    # asserting that directory stands in for that same role.
    server_py = start.resolve() / "server.py"
    tracked, dubious_ownership = _path_is_tracked(root, server_py)
    if dubious_ownership:
        # PR #3174: the production viz server runs as a different OS user
        # than the checkout it serves, and `_run_git` passes no
        # safe.directory override -- git's own ownership refusal fires,
        # exactly as it would for any other caller. Surface a reason
        # distinct from the generic git-command-failed one below, per the
        # spec.
        return {
            "sha": None, "branch": None, "dirty": None,
            "started_at": started_at,
            "reason": _DUBIOUS_OWNERSHIP_REASON,
        }
    if tracked is None:
        return {
            "sha": None, "branch": None, "dirty": None,
            "started_at": started_at,
            "reason": "git command failed (missing binary, timeout, or error)",
        }
    if not tracked:
        return {
            "sha": None, "branch": None, "dirty": None,
            "started_at": started_at,
            "reason": (
                f"code is not tracked by the enclosing git repo ({root}) "
                "— likely an installed plugin copy"
            ),
        }

    sha_proc = _run_git(["rev-parse", "HEAD"], root)
    if sha_proc is None or sha_proc.returncode != 0:
        return {
            "sha": None, "branch": None, "dirty": None,
            "started_at": started_at,
            "reason": "git command failed (missing binary, timeout, or error)",
        }
    sha = sha_proc.stdout.strip()

    branch: "str | None" = None
    branch_failed = False
    branch_proc = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    if branch_proc is not None and branch_proc.returncode == 0:
        raw_branch = branch_proc.stdout.strip()
        branch = None if raw_branch == "HEAD" else raw_branch  # "HEAD" = detached
    else:
        branch_failed = True  # distinct from detached HEAD, which is branch=None with no failure

    dirty: "bool | None" = False
    dirty_failed = False
    # `--untracked-files=no` (PR #3174): unifies with footer.py's own
    # dirty-check convention (which this module's capture now feeds
    # directly, see module docstring) -- a served checkout may carry
    # harmless untracked files, and "dirty" should mean modified TRACKED
    # code, not "a stray file happens to sit in the tree."
    status_proc = _run_git(["status", "--porcelain", "--untracked-files=no"], root)
    if status_proc is not None and status_proc.returncode == 0:
        dirty = bool(status_proc.stdout.strip())
    else:
        dirty = None
        dirty_failed = True

    info = {"sha": sha, "branch": branch, "dirty": dirty, "started_at": started_at}

    reasons = []
    if branch_failed:
        reasons.append("branch unknown (git rev-parse --abbrev-ref failed)")
    if dirty_failed:
        reasons.append("could not determine dirty state (git status failed)")
    if reasons:
        info["reason"] = "; ".join(reasons)

    warnings = []
    if branch_failed:
        # Distinct from the generic non-dev/main case below -- a failed
        # lookup is not the same claim as "the branch genuinely isn't
        # dev/main" (a genuine detached HEAD, which is also branch=None,
        # falls through to that generic case instead, per
        # test_detached_head_reports_branch_null).
        warnings.append("branch unknown (lookup failed)")
    elif branch not in ("dev", "main"):
        warnings.append(f"branch is {branch!r}, not dev/main")
    if dirty:
        warnings.append("checkout is dirty")
    if dirty_failed:
        warnings.append("dirty state unknown")
    if warnings:
        info["warning"] = "; ".join(warnings)

    return info


def is_plugin_fallback_reason(reason: "str | None") -> bool:
    """True when *reason* -- a null-``sha`` result's ``"reason"`` string from
    ``capture_version_info()`` -- means "genuinely no checkout to read, the
    ``.deployed-version`` plugin marker is the right fallback." Covers the
    two reasons meaning exactly that: ``"not a git checkout"`` (no ``.git``
    anywhere above the start path) and the not-tracked-ancestor-repo reason
    (an enclosing ``.git`` was found but doesn't track the running code --
    the real plugin-install topology, see this module's docstring).

    False for the other two null-``sha`` reasons -- dubious ownership and
    a generic git-command failure -- because THOSE mean a checkout genuinely
    exists but git couldn't read it; silently substituting a
    ``.deployed-version`` marker there could report a stale sha as if it
    were this run's actual code provenance, which is the exact fabrication
    this module's fail-honest design exists to prevent (those two cases
    render ``unversioned`` instead -- see ``footer._code_provenance()``).
    """
    if not reason:
        return False
    return reason == "not a git checkout" or "not tracked" in reason


def read_deployed_version(engram_home: "str | Path | None" = None) -> dict[str, str]:
    """Parse ``.deployed-version``'s ``key=value`` lines from *engram_home*
    (default: ``$ENGRAM_HOME`` or ``~/.engram``, the same env-override
    convention ``engram_core.DATA_DIR`` uses). Returns ``{}`` on any
    read/parse failure or if the file doesn't exist -- never raises.

    Moved here from ``viz.footer`` (PR #3174): this module is now the
    single owner of the plugin-fallback shape (``is_plugin_fallback_reason``
    above decides WHEN to use it; this function reads WHAT to render), so
    both ``footer.py`` and ``server.py``'s ``/api/version`` startup capture
    share one reader instead of two independent copies.
    """
    home = (
        Path(engram_home) if engram_home is not None
        else Path(os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram"))
    )
    path = home / ".deployed-version"
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip():
                out[key.strip()] = value.strip()
    except OSError:
        return {}
    return out
