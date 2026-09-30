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
  forum #389's parallel incident thread): the sha of the git checkout the
  ``viz`` package was ACTUALLY IMPORTED FROM (derived from this module's
  own ``__file__``, never a configured/assumed directory -- a configured
  path and the code that actually ran can diverge, and the configured-path
  label is then confidently wrong about the code that just executed, per
  forum #389's discussion of that subtlety), with a ``-dirty`` suffix if
  that tree has uncommitted (tracked-file) changes.

Which shape is in play is determined structurally, not by a flag --
**PR #3174**: this module no longer walks the tree itself to decide. It
delegates entirely to ``version_info.capture_version_info()`` (this
module's own ``__file__``-derived directory, passed explicitly as
``start_path``), which additionally verifies the found git root actually
TRACKS the running ``server.py`` before accepting it -- footer's OWN prior
walk had no such check, which is exactly what let it silently misattribute
an unrelated ANCESTOR repo's HEAD as this code's provenance on a real
plugin install (measured against the installed plugin copy:
``_code_provenance()`` returned ``~/.engram``'s HEAD labeled
``(worktree)`` -- ``~/.engram`` encloses the plugin-flattened ``viz/``
tree under a gitignored ``marketplace/`` and was never the code's own
repo). A null ``sha`` whose reason means "genuinely no tracking checkout"
(``version_info.is_plugin_fallback_reason``) falls back to the
``.deployed-version`` marker (``version_info.read_deployed_version``,
moved there since PR #3174 -- see that module's docstring for why no
circular import results); any other null-``sha`` reason (a checkout
exists but git couldn't read it -- dubious ownership, git-command
failure) renders ``unversioned`` rather than risk reporting a stale
plugin marker as if it were this run's live code provenance.

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

from . import version_info

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


def _code_provenance() -> str:
    """The CODE sha actually serving this page, labeled by install shape.

    Computed once per process and cached (module-level; code doesn't
    change mid-process — a changed source file needs a process restart to
    take effect regardless, per server.py's own INVOCATION NOTE) rather
    than re-run on every request.

    PR #3174: delegates entirely to ``version_info.capture_version_info()``
    -- this module no longer walks the tree itself (see module docstring
    for why: footer's own prior walk had no tracking check and could
    silently misattribute an unrelated ancestor repo's HEAD as this code's
    provenance on a real plugin install). ``start_path`` is this module's
    own on-disk directory, passed explicitly rather than relying on
    ``capture_version_info``'s internal default -- both resolve to the
    same directory when this really is ``viz/footer.py`` running from the
    ``viz`` package, but an explicit ``start_path`` is what makes this
    function's behavior derivable from (and testable against) THIS
    module's own ``__file__``, per ``capture_version_info``'s own
    docstring note that a caller may pass an explicit start_path standing
    in for "the viz package's own location."
    """
    global _CODE_PROVENANCE_CACHE
    if _CODE_PROVENANCE_CACHE is not None:
        return _CODE_PROVENANCE_CACHE

    this_dir = Path(__file__).resolve().parent
    info = version_info.capture_version_info(this_dir)

    if info.get("sha") is not None:
        # Working-tree / dev-checkout shape: a tracking git root was found.
        sha = _short(info["sha"])
        dirty_suffix = "-dirty" if info.get("dirty") else ""
        label = f"{sha}{dirty_suffix} (worktree)"
        _CODE_PROVENANCE_CACHE = label
        return label

    if version_info.is_plugin_fallback_reason(info.get("reason")):
        # Plugin-install shape: no tracking checkout found, so read the
        # marker the installer wrote at build/upgrade time. ENGRAM_HOME
        # follows the same env-override convention as engram_core.DATA_DIR
        # (read_deployed_version's own default).
        dv = version_info.read_deployed_version()
        alpha_sha = dv.get("alpha_sha", "")
        if alpha_sha and alpha_sha != "unknown":
            dirty_suffix = "-dirty" if dv.get("alpha_dirty") == "dirty" else ""
            label = f"{alpha_sha}{dirty_suffix} (plugin)"
            _CODE_PROVENANCE_CACHE = label
            return label

    # Either no plugin marker to fall back to, or the null sha meant a
    # checkout genuinely exists but git couldn't read it (dubious
    # ownership / git-command failure) -- fail-honest rather than guess.
    _CODE_PROVENANCE_CACHE = _UNVERSIONED
    return _UNVERSIONED


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
