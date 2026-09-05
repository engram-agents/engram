"""viz.footer — the shared page-footer partial (engram-alpha#2555 PR B
amendment-1, requirement 4).

Renders once per request (unlike ``shell``'s import-time page shells) with
two provenance labels — DATA and CODE — per the design ruling on forum #389
(reply #7271, the settled answer after an earlier partial one on the same
thread).

**Data sha**: the SELECTED agent's own ``~/.engram``-equivalent data
directory's git HEAD (the "anchor" concept ``health/engine.py``'s
``_anchor_commit_sha()`` already computes for health snapshots) — but
computed for the agent whose graph the page is rendering, not the viz
server process's own default. ``health/engine.py``'s function can't be
reused UNMODIFIED for this: it always runs against ``engram_core.DATA_DIR``,
a process-global set once at ``engram_core`` import time, with no
per-call override (``core._git``'s ``cwd=DATA_DIR`` is a fixed positional
argument, and passing ``cwd=`` again via its ``**kwargs`` would raise
``TypeError: got multiple values for keyword argument 'cwd'``). On a
multi-tenant viz server serving 6 agents behind one process, "the process's
DATA_DIR" and "the agent whose page is being rendered" are usually
different directories. So this module reuses the PATTERN (git rev-parse
HEAD, never-raise, same subprocess safety settings) against an EXPLICIT,
per-call directory instead.

**Code sha**: two labeled install shapes, per the forum #389 ruling --
"a reader knows which one they are looking at ... #2531's hazard made
visible on the page instead of latent":

- Plugin install: ``.deployed-version``'s ``alpha_sha`` (+ ``-dirty`` when
  ``alpha_dirty=dirty``) -- the same field ``engram_stats.py``'s ``version``
  section already reads, written at build/upgrade time by
  ``install-local-marketplace.sh``.
- Working-tree run (what the live shared :5001 dashboard actually is, per
  forum #389's parallel incident thread): ``git rev-parse HEAD`` of the
  source root the ``viz`` package was ACTUALLY IMPORTED FROM (derived from
  this module's own ``__file__``, never a configured/assumed directory --
  a configured path and the code that actually ran can diverge, and the
  configured-path label is then confidently wrong about the code that
  just executed, per forum #389's discussion of that subtlety), with
  a ``-dirty`` suffix if that tree has uncommitted changes.

Which shape is in play is determined structurally, not by a flag: walk
upward from this module's own file looking for a ``.git`` directory. The
deployed plugin bundle (``build-plugin.sh``'s output) is a flat, non-git
tree -- no ``.git`` exists anywhere above it -- so finding one means this
is a working-tree/dev-checkout run; finding none means it's a plugin
install, and the ``.deployed-version`` marker (written by the exact same
installer that produces that flat, non-git tree) is the shape to read.

**Fail-honest, both halves** (forum #389's ruling): a value that can't be
resolved renders as the literal word ``unversioned`` -- never a fabricated
or cached number. See ``_data_provenance``/``_code_provenance`` below;
neither ever raises.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_GIT_EXE = os.environ.get("ENGRAM_GIT_EXE") or "git"
_GIT_TIMEOUT = 15  # seconds -- matches engram_core.GIT_TIMEOUT's value

_UNVERSIONED = "unversioned"


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess | None:
    """Run a git command in *cwd*. Returns None (never raises) on any
    failure -- unavailable binary, timeout, cwd not a repo, etc. Mirrors
    ``engram_core._git``'s never-raise contract and safety settings
    (``GIT_TERMINAL_PROMPT=0`` so a bad credential prompt can't hang a page
    request), parameterized by an explicit *cwd* since this module needs a
    different directory per call (see module docstring for why
    ``engram_core._git`` itself can't be reused here)."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(
            [_GIT_EXE, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            env=env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(f"[viz.footer] git command failed: {args} in {cwd} — {e}",
              file=sys.stderr)
        return None


def _short(sha: str) -> str:
    """First 7 chars, matching git's own default short-sha length and
    every other short-sha rendering already in this codebase (e.g.
    install-local-marketplace.sh's ``rev-parse --short HEAD``)."""
    return sha[:7]


def _data_provenance(data_dir: str | os.PathLike | None) -> str:
    """The DATA sha for the agent whose graph is being rendered.

    *data_dir* is the directory CONTAINING that agent's ``knowledge.db``
    (i.e. its ``~/.engram``-equivalent root) -- the caller derives this
    from the agent's already-resolved ``db_path``. Returns the literal
    string "unversioned" (never raises, never fabricates) when *data_dir*
    is falsy, doesn't exist, or isn't a git working tree.
    """
    if not data_dir:
        return _UNVERSIONED
    path = Path(data_dir)
    if not path.is_dir():
        return _UNVERSIONED
    result = _run_git(["rev-parse", "HEAD"], cwd=path)
    if result is None or result.returncode != 0:
        return _UNVERSIONED
    sha = result.stdout.strip()
    return _short(sha) if sha else _UNVERSIONED


# How many ancestor levels above viz/footer.py's own directory to search
# for a .git before giving up (see _find_git_root's docstring for why this
# walk, unlike its siblings, is deliberately BOUNDED). The real cases are
# shallow: 2 in the deployed plugin tree (viz/ -> plugin root), 4 in this
# repo's own source tree (viz/ -> src/engram -> src -> repo root). 12 is
# generous headroom above both without reaching a user's $HOME or a
# container's filesystem root in any layout this project ships.
_GIT_ROOT_SEARCH_DEPTH = 12


def _find_git_root(start: Path) -> Path | None:
    """Walk upward from *start*, up to ``_GIT_ROOT_SEARCH_DEPTH`` ancestor
    levels, looking for the nearest directory that itself contains a
    ``.git`` entry (dir for a normal repo, file for a worktree/submodule --
    ``.exists()`` covers both). Returns None if none is found within that
    bound -- either the flat, non-git plugin-bundle case, or (deliberately,
    see below) a ``.git`` that exists but is too far up to plausibly be
    THIS code's own repo.

    Deliberately BOUNDED, unlike ``data/config.py``'s ``_find_tools_parent``
    / ``data/stats.py``'s ``_find_indexer_dir``, which walk unbounded to the
    filesystem root -- reviewer-fairy finding on PR#2564: those two search
    for a SPECIFIC sibling file, where a false-positive match requires an
    unrelated file with that exact name existing at the exact right
    relative shape somewhere above the real location -- vanishingly
    unlikely. This one searches for "any .git", which is a much broader,
    more plausible false-positive target: an unrelated ancestor directory
    genuinely being a git repo for its OWN reasons (a dotfiles-tracked
    $HOME, a git-tracked container filesystem root) is a real thing that
    happens, and finding one would silently report an unrelated repo's
    HEAD sha as this code's own provenance -- a real sha, confidently
    wrong, which is exactly what this module's fail-honest design exists
    to prevent. Bounding the walk trades "correct at an implausible
    nesting depth no real deployment uses" for "never silently misattributes
    provenance to an unrelated ancestor repo" -- the right side of that
    trade for a module whose whole job is not fabricating a wrong answer.
    """
    for candidate in [start, *start.parents][:_GIT_ROOT_SEARCH_DEPTH]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _read_deployed_version(engram_home: Path) -> dict[str, str]:
    """Parse ``.deployed-version``'s ``key=value`` lines. Returns {} on any
    read/parse failure or if the file doesn't exist -- never raises."""
    path = engram_home / ".deployed-version"
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip():
                out[key.strip()] = value.strip()
    except OSError:
        return {}
    return out


def _code_provenance() -> str:
    """The CODE sha actually serving this page, labeled by install shape.

    Computed once per process and cached (module-level; code doesn't
    change mid-process — a changed source file needs a process restart to
    take effect regardless, per server.py's own INVOCATION NOTE) rather
    than re-run on every request.
    """
    global _CODE_PROVENANCE_CACHE
    if _CODE_PROVENANCE_CACHE is not None:
        return _CODE_PROVENANCE_CACHE

    this_file = Path(__file__).resolve()
    git_root = _find_git_root(this_file.parent)

    if git_root is not None:
        # Working-tree / dev-checkout shape: the code that just imported
        # is running straight out of a git repo (no flat plugin bundle
        # ever has a .git anywhere above it).
        result = _run_git(["rev-parse", "HEAD"], cwd=git_root)
        if result is not None and result.returncode == 0 and result.stdout.strip():
            sha = _short(result.stdout.strip())
            status = _run_git(["status", "--porcelain", "--untracked-files=no"],
                              cwd=git_root)
            dirty = bool(status is not None and status.returncode == 0
                         and status.stdout.strip())
            label = f"{sha}{'-dirty' if dirty else ''} (worktree)"
            _CODE_PROVENANCE_CACHE = label
            return label
        _CODE_PROVENANCE_CACHE = _UNVERSIONED
        return _UNVERSIONED

    # Plugin-install shape: no .git above this file, so read the marker
    # the installer wrote at build/upgrade time. ENGRAM_HOME follows the
    # same env-override convention as engram_core.DATA_DIR.
    engram_home = Path(os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram"))
    dv = _read_deployed_version(engram_home)
    alpha_sha = dv.get("alpha_sha", "")
    if not alpha_sha or alpha_sha == "unknown":
        _CODE_PROVENANCE_CACHE = _UNVERSIONED
        return _UNVERSIONED
    dirty_suffix = "-dirty" if dv.get("alpha_dirty") == "dirty" else ""
    label = f"{alpha_sha}{dirty_suffix} (plugin)"
    _CODE_PROVENANCE_CACHE = label
    return label


_CODE_PROVENANCE_CACHE: str | None = None


# ---------------------------------------------------------------------------
# The rendered footer fragment.
# ---------------------------------------------------------------------------

_PRODUCT_NAME = "ENGRAM"

# Wording ruled final (#2555 amendment 2, forum #389): kept as shipped.
# Originally drafted from the amendment-1 design post's own phrasing
# ("computed heuristics, not guarantees"), kept verbatim.
_DISCLAIMER = (
    "© 2026 the ENGRAM authors · Apache-2.0 · "
    "scores and diagnostics are computed heuristics, not guarantees"
)


def render_footer(data_dir: str | os.PathLike | None) -> str:
    """Return the ``<footer>`` HTML fragment for one page render.

    *data_dir* is the directory containing the currently-viewed agent's
    ``knowledge.db`` (the caller derives this from the resolved
    ``db_path`` — see ``server.py``'s page-route handler). Pass None (or
    an empty/nonexistent path) when there is no resolved agent (e.g. the
    requested ``?agent=`` names nothing registered) — the data half then
    renders "unversioned" rather than guessing at a directory.
    """
    data_label = _data_provenance(data_dir)
    code_label = _code_provenance()
    return (
        '<footer class="viz-footer">\n'
        f'  <span class="viz-footer-product">{_PRODUCT_NAME}</span>\n'
        f'  <span class="viz-footer-provenance">'
        f'data anchor {data_label} (graph repo\'s HEAD) · '
        f'code {code_label} (the code this run imported)'
        f'</span>\n'
        f'  <span class="viz-footer-disclaimer">{_DISCLAIMER}</span>\n'
        '</footer>'
    )
