#!/usr/bin/env python3
"""baton_gate — shared PR-flip gate helpers (#2884, second half).

Extracted out of tools/baton.py's cmd_flip so the CLI (tools/baton.py) and
the REST endpoint (src/forum/server.py POST /api/projects/<pid>/flip)
enforce the IDENTICAL CI-green / post-approval-freshness / repo-anchor gate,
rather than the CLI-only enforcement issue #2884 found: a client that talks
to the REST API directly (not through tools/baton.py) bypassed every
CI/approval/colleague check silently, because
forum.coordination.projects.flip()'s own docstring states validation is
"the caller's job and is NOT performed here."

Every function in this module is PURE: no argparse, no sys.exit, no
printing to stdout/stderr. Callers own how a failure is reported:
  - tools/baton.py's CLI wrappers print a friendly message + sys.exit with
    the original exit codes (EXIT_VALIDATION etc.) -- unchanged from before
    this extraction, see the "Scope note" below.
  - src/forum/server.py's HTTP wrapper (api_project_flip) returns a 4xx
    JSON body naming the failed check.

Deployment hazard (read before assuming this module is importable at
server.py's real runtime): tools/install-forum-service.sh's rsync step
snapshots ONLY src/forum/ (-> $APP/forum/) and tools/engram-pkg/ (->
$APP/tools/engram-pkg/) into the live service directory -- tools/baton.py,
this module, and the rest of src/engram/tools/ are NOT currently part of
that snapshot. So `import baton_gate` can genuinely fail to resolve in the
deployed forum service until the deploy script also snapshots this file
(tracked as a follow-up; see the PR body). src/forum/server.py's loader
handles this defensively: any import failure is treated as "gate
unavailable" and the endpoint FAILS CLOSED for the one check that needs
it (refuses a maintainer-role flip) rather than silently proceeding
ungated -- "unknown must not render as permitted" (#2884's own framing).

Scope note -- NOT a full extraction, deliberately (see PR "Ambiguous
decisions"): _pool_sentinel() / _is_pool_sentinel() / _is_maintainer_flip()
in tools/baton.py are NOT replaced with imports from here. tools/baton.py
keeps its own verbatim copies, byte-for-byte unchanged by this PR.
tests/test_baton.py monkeypatches `_baton._pool_sentinel` /
`_baton._is_pool_sentinel` / `_baton._POOL_SENTINEL_CACHE` directly at
~35 call sites, including TestIsMaintainerFlip, which patches
`_baton._pool_sentinel` and asserts the effect is visible through
`_baton._is_maintainer_flip(...)`. Python resolves a function's free
variables against the globals of the MODULE IT WAS DEFINED IN, not the
caller's module -- so if `_is_maintainer_flip` were defined here and
merely imported into tools/baton.py, patching `_baton._pool_sentinel`
would have no effect on it (it would still consult *this* module's own
_pool_sentinel), silently breaking that whole test class. Separately,
_pool_sentinel()'s caching (`_POOL_SENTINEL_CACHE`) is read/reset directly
by many cmd_init-style tests that never patch _pool_sentinel itself --
those rely on the cache literally living in tools/baton.py's own module
dict. Rather than rewrite ~35 established test call sites (disproportionate
for a "second half" hardening PR, per the issue's own scoping), this module
carries its OWN small, independent copies of the sentinel-resolution +
is-maintainer-flip predicates, for the REST endpoint's exclusive use. They
are ~40 lines of pure string/bool logic -- not the actual complexity or
bug-surface of this fix (that's the gh-shelling CI/approval logic below,
which IS fully unified with zero duplication). tests/test_baton_gate.py's
TestSentinelPredicateParity class pins the two independent copies
(tools.baton's and this module's) to identical behavior across a matrix of
inputs, so a future edit to one that silently diverges from the other
fails loudly rather than drifting unnoticed by anyone -- the
"kept in sync by hand" anti-pattern src/forum/board_projects.py already
carries and this PR was asked not to repeat, mitigated here (for this
narrower, low-risk slice) with an automated parity check rather than left
as bare hope.

Addendum (engram-alpha#3039 U1): `_is_pool_sentinel()` in both this module
and tools/baton.py was renamed `_is_legacy_pool_sentinel()` and demoted to a
fallback-only helper behind a new `_resolve_phase()` (added to both modules,
same deliberate-duplication treatment as the three predicates above --
mirrored, not imported, per this scope note's own reasoning). tools/baton.py
briefly kept a thin `_is_pool_sentinel = _is_legacy_pool_sentinel` back-compat
alias for src/forum/tests/test_baton_thin_client.py's 7 monkeypatch call
sites; that alias was retired in round-1 review (those call sites were
migrated to the new name instead), so as of U1's merged form `_is_pool_sentinel`
exists as a name in neither module. `_is_maintainer_flip` here is otherwise
unchanged except for this one rename in its body (its own phase-edge
rework is a later unit, per U1's spec).
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# GitHub anchor parsing (moved verbatim from tools/baton.py; #2884)
# ---------------------------------------------------------------------------

# GitHub anchor value validation: pr/<N>, pr/<owner>/<repo>/<N> (repo-qualified,
# #1715), project/<N>, or project/<owner>/<N> (#3568: the anchor carries the GitHub
# owner of the project, so no seat needs config for it). Repo-qualification is REQUIRED for any gh call this
# file makes (#1918: there is no default repo) -- an unqualified pr/<N> is
# refused-and-counted by cmd_gc/cmd_flip/cmd_merge (and now api_project_flip)
# rather than resolved against a guess.
#
# Numbers are ASCII digits only and at most 15 long (#3039 U2b round 5,
# S1/S2): ``\d`` / ``str.isdigit`` / ``int`` accept Arabic-Indic and other
# Unicode digits, and ``int(<5000 digits>)`` raises ValueError on Python >= 3.11
# (a 500 from the route and -- the anchor being write-once in ``peer`` -- a
# stuck baton). No real PR number is anywhere near 15 digits.
#
# #3568: the owner of ``project/<owner>/<N>`` follows the GitHub login rule: 1-39
# chars, alphanumeric or hyphen, not starting with a hyphen. Readers lowercase an
# anchor before parsing it; GitHub logins are case-insensitive, so that is fine.
_PROJECT_OWNER_PATTERN = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}"
_GITHUB_ANCHOR_RE = re.compile(
    r"^(?:pr/(?:[\w.-]+/[\w.-]+/)?[0-9]{1,15}"
    r"|project/(?:" + _PROJECT_OWNER_PATTERN + r"/)?[0-9]{1,15})$"
)
_PROJECT_OWNER_ANCHOR_RE = re.compile(
    r"^project/(" + _PROJECT_OWNER_PATTERN + r")/[0-9]{1,15}$"
)


def _ascii_digits(value) -> bool:
    """True for a non-empty string of ASCII digits 0-9 only. PURE."""
    return isinstance(value, str) and bool(value) and value.isascii() and value.isdigit()


def _same_number(a: str, b: str) -> bool:
    """Numeric equality of two ASCII-digit strings WITHOUT ``int()`` (no
    int-from-str digit limit, no Unicode digits): leading zeros are ignored.
    PURE; False when either is not ASCII digits."""
    if not (_ascii_digits(a) and _ascii_digits(b)):
        return False
    return (a.lstrip("0") or "0") == (b.lstrip("0") or "0")

# #1918: there is NO default repo, deliberately -- do not revisit.
#
# An earlier version of tools/baton.py resolved an unqualified `pr/<N>`
# anchor against $ENGRAM_DEFAULT_GITHUB_REPO (falling back to the public
# engram-agents/engram repo). That was wrong in both directions: on a
# private dev install it silently queried the WRONG repo (every gh call
# in this file failed or, worse, matched an unrelated same-numbered PR --
# gh pr merge --squash on that guess is the single most consequential call
# in this file); on a fresh external install it resolved against the
# ENGRAM project's OWN public repo, which is not the user's repo at all --
# there is no value of a default that is correct for an external install.
#
# The fix (per umwelt's src/umwelt/gh_reconcile.py, ported here rather than
# re-derived -- umwelt already solved this): every gh call requires an
# explicit, repo-qualified anchor. A PR-baton without one is refused and
# counted (never guessed at) by every caller. See each call site's own
# comment for how it surfaces the refusal.


# #3366 (Kepler): the ONE wording of the two-route hint shown when a PR-shaped
# baton id resolves no PR anchor. Shared VERBATIM by the CLI refusal (tools/baton.py
# ``_refuse_unanchored_pr_shaped``, #3364/#3365) and the forum server's 422
# (src/forum/server.py ``api_project_init``). Deliberately SYNTAX-NEUTRAL: the CLI
# says ``--github``, the API says the ``github`` field, so each surface supplies its
# own lead-in and this sentence is what must stay byte-identical (pinned by
# src/forum/tests/test_baton_init_refuses_anchorless_pr.py).
PR_ANCHOR_TWO_ROUTE_HINT = (
    "if it IS a PR, give the anchor pr/<owner>/<repo>/<N>; "
    "if it is NOT a PR, give project/<N> (a bunch) or use a non-PR-shaped id"
)


# #3377 (Kepler): the ONE wording of the accepted ``github`` anchor forms, shared by
# the forum server's 422 for a PRESENT github that does not parse (any baton id,
# src/forum/server.py ``api_project_init``) and the CLI's ``baton init --github``
# refusal (tools/baton.py ``cmd_init``). Surface-neutral: each surface supplies its
# own lead-in. These are exactly the shapes ``_parse_github_anchor`` accepts.
GITHUB_ANCHOR_ACCEPTED_FORMS = (
    "pr/<owner>/<repo>/<N>, pr/<N>, project/<owner>/<N>, or project/<N>"
)


def _parse_github_anchor(value: str) -> Optional[tuple]:
    """Parse a github anchor value into (kind, repo, number).

    kind is 'pr' or 'project'. repo is 'owner/repo' if the anchor is
    repo-qualified, else None (#1918: there is no default repo -- callers
    must refuse-and-count an unqualified PR anchor rather than guess one).
    number is the PR/project number as a string. Returns None if `value`
    doesn't match a known anchor shape (caller should treat as "no anchor").

    #3568: ``project/<owner>/<N>`` parses to ``("project", None, <N>)`` -- the SAME
    tuple as the bare ``project/<N>``. The owner is NEVER put in the repo slot: many
    callers read a non-None slot 1 as "repo-qualified PR". Use
    :func:`_project_anchor_owner` for the owner.
    """
    if not value:
        return None
    m = re.match(r"^pr/(?:([\w.-]+)/([\w.-]+)/)?([0-9]{1,15})$", value)
    if m:
        owner, repo, number = m.group(1), m.group(2), m.group(3)
        repo_qualified = f"{owner}/{repo}" if owner and repo else None
        return ("pr", repo_qualified, number)
    m = re.match(r"^project/(?:" + _PROJECT_OWNER_PATTERN + r"/)?([0-9]{1,15})$", value)
    if m:
        return ("project", None, m.group(1))
    return None


def _project_anchor_owner(value) -> Optional[str]:
    """The GitHub owner carried by a ``project/<owner>/<N>`` anchor (#3568), else None.

    None for a bare ``project/<N>``, for any ``pr/...`` anchor, and for anything
    malformed (or not a string). PURE. The owner is returned as written (callers
    lowercase anchors before parsing; GitHub logins are case-insensitive).
    """
    if not isinstance(value, str):
        return None
    m = _PROJECT_OWNER_ANCHOR_RE.match(value)
    return m.group(1) if m else None


_INIT_LOG_LINE_RE = re.compile(r"^- \S+ initialized → ([a-z0-9_-]+):", re.MULTILINE)


def _derive_legacy_author(body: str) -> str:
    """Recover the author-proxy for a baton created before #2961 (no
    `author:` frontmatter field) by parsing its own turn log's very first
    line, which every baton -- with no exception, back to the format's
    introduction -- carries verbatim:

        - {ts} initialized → {turn}: {turn_reason}

    (see `init()` in coordination/projects.py, the sole writer of this
    line). This is NOT a live-store migration: it makes EVERY existing
    baton file self-describing on read, with zero write, zero bulk
    mutation, and zero risk to the 1,493 already-live batons a colleague
    review measured directly against the shared store (#2961 follow-up,
    2026-09-16) -- the exact number a backfill pass would otherwise have
    needed to touch, each one a live write racing whatever the running
    forum process is doing to that same file at the same moment.

    Returns "" if the body has no matching line (a malformed or
    hand-crafted baton) -- treated identically to an empty `author:`
    field: the author exemption in `_is_maintainer_flip` simply cannot
    fire, never an error.

    Deliberately parses the FIRST match only (`re.search` semantics via
    the unanchored pattern, but the line format is only ever written once
    per baton, at `init()` time -- a body with more than one such line
    would be a hand-edited or corrupted file, not a real coordination
    artifact, and taking the first occurrence is the conservative choice
    either way).
    """
    m = _INIT_LOG_LINE_RE.search(body or "")
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Pool-sentinel resolution + is-maintainer-flip predicate
#
# Independent copy for the REST endpoint's exclusive use -- see this
# module's docstring ("Scope note") for why these are NOT shared with
# tools/baton.py's own _pool_sentinel / _is_pool_sentinel /
# _is_maintainer_flip, and tests/test_baton_gate.py's
# TestSentinelPredicateParity for the automated equivalence check.
# ---------------------------------------------------------------------------


class PoolSentinelUnresolved(Exception):
    """Raised when the pool sentinel cannot be determined.

    tools/baton.py's own _pool_sentinel() handles this identical condition
    by printing a message and calling sys.exit(EXIT_VALIDATION) -- correct
    for a single-shot CLI process, fatal for a long-running Flask server.
    This module raises instead, so a caller (the REST endpoint) can fail
    closed on just the one request without killing the server process.
    """


_POOL_SENTINEL_CACHE: list = []  # one-element list; empty = not yet resolved


def _resolve_pool_sentinel(engram_home: Optional[str] = None) -> str:
    """Return the pool sentinel for this install (I/O + cache).

    Same resolution order as tools/baton.py's _pool_sentinel():
      1. Cached value (after first call in this process).
      2. config.json primary_user field.
      3. BATON_POOL_SENTINEL env var (fallback only; config.json wins).
      4. Raise PoolSentinelUnresolved -- fails loud rather than silently
         hardcoding a name.
    """
    if _POOL_SENTINEL_CACHE:
        return _POOL_SENTINEL_CACHE[0]

    home = engram_home or os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
    try:
        config_path = Path(home) / "config.json"
        pu = json.loads(config_path.read_text()).get("primary_user")
        if pu:
            _POOL_SENTINEL_CACHE.append(str(pu).lower())
            return _POOL_SENTINEL_CACHE[0]
    except (OSError, ValueError, AttributeError):
        pass

    env_val = os.environ.get("BATON_POOL_SENTINEL")
    if env_val:
        _POOL_SENTINEL_CACHE.append(env_val.lower())
        return _POOL_SENTINEL_CACHE[0]

    raise PoolSentinelUnresolved(
        "cannot determine pool sentinel — set primary_user in "
        "$ENGRAM_HOME/config.json (or set BATON_POOL_SENTINEL)."
    )


def _is_legacy_pool_sentinel(name: str) -> bool:
    """Legacy fallback only -- see _resolve_phase. Exactly today's (pre-#3039)
    _is_pool_sentinel(): compares against THIS SEAT's own config.json
    primary_user, seat-DEPENDENT by construction. Renamed by #3039 U1 --
    mirrors tools/baton.py's own rename; see that module's copy of this
    function for the full rationale. _is_maintainer_flip below still calls
    this directly (mechanical rename only, not migrated to _resolve_phase in
    this unit).

    Accepts both the full sentinel ("lei shi") and its first token ("lei") so
    pools initialized with a short-form turn value work correctly (issue #1309).

    Raises PoolSentinelUnresolved if the sentinel cannot be determined --
    callers (api_project_flip) must treat that as gate-unavailable and fail
    closed, not as "not the sentinel."
    """
    sentinel = _resolve_pool_sentinel()  # e.g. "lei shi"
    if name == sentinel:
        return True
    first_token = sentinel.split()[0] if sentinel else ""
    return bool(first_token) and name == first_token


# Matches a PR-baton's project_id by PREFIX ONLY (optional single-segment
# repo-qualifier, then "PR-"). Mirror of tools/baton.py's _PR_BATON_NAME_RE;
# pinned to it by src/forum/tests/test_baton_phase_injection.py (#3039 U2b r2).
_PR_BATON_NAME_RE = re.compile(r"^(?:[A-Z][A-Z0-9]*-)?PR-")


def _is_pr_baton(project_id: str, fields: dict) -> bool:
    """True if this baton is a PR baton (not a bunch) -- the server's copy of
    tools/baton.py's ``_is_pr_baton`` / ``_is_pr_shaped`` (#3039 U2b round 2:
    the server must decide PR-vs-bunch itself instead of trusting a client).

    Anchor-first, name-only-when-unanchored: a parseable ``github`` anchor
    decides (``pr/...`` => PR, ``project/N`` => bunch) whatever the id looks
    like; only an unanchored (or unparseable-anchor) baton falls back to the
    id, where a ``pool-`` prefix is always a bunch and otherwise
    ``_PR_BATON_NAME_RE`` (case-insensitive) decides. PURE.
    """
    return _is_pr_shaped(project_id, (fields or {}).get("github", ""))


def _is_pr_shaped(project_id: str, github: str) -> bool:
    """Shared body of :func:`_is_pr_baton` and the server's init check (there
    is no baton file yet at init, only the id and the requested anchor)."""
    github = (github or "").strip().lower()
    parsed = _parse_github_anchor(github) if github else None
    if parsed is not None:
        return parsed[0] == "pr"
    if project_id.strip().lower().startswith("pool-"):
        return False
    return bool(_PR_BATON_NAME_RE.match(project_id.upper()))


def _resolve_phase(fields: dict, *, is_bunch: bool) -> tuple[str, bool]:
    """Resolve a baton's current phase from its frontmatter fields.

    Returns (phase, used_legacy_fallback).

    Mirrors tools/baton.py's own _resolve_phase() -- see that module's copy
    for the full two-tier-resolution rationale (engram-alpha#3039 U1) and
    tests/test_baton_gate.py::TestSentinelPredicateParity for the pinning
    test. DELIBERATELY DOES NOT PRINT -- this module's docstring ("Scope
    note") requires every function here to be PURE (no argparse, no
    sys.exit, no printing to stdout/stderr), since this module's functions
    are called from a long-running Flask process via api_project_flip; a
    caller that wants the legacy-fallback signal reads the returned
    `used_legacy_fallback` bool and logs it itself.

    May raise PoolSentinelUnresolved (via _is_legacy_pool_sentinel) when the
    legacy branch is reached and the sentinel can't be determined --
    callers must treat that as gate-unavailable and fail closed, same as
    _is_legacy_pool_sentinel's own callers.
    """
    explicit = fields.get("phase", "").strip().lower()
    if explicit:
        return explicit, False
    turn = fields.get("turn", "").strip().lower()
    if _is_legacy_pool_sentinel(turn):
        return ("pool" if is_bunch else "merge"), True
    return "", False


def _is_maintainer_flip(
    to_agent: str,
    current_holder: str,
    colleague_name: str,
    author: str = "",
) -> bool:
    """True when flipping the baton to `to_agent` represents presenting
    the PR for MERGE (to the maintainer) -- the point where CI-green,
    approval-freshness, and colleague-approval-existence must all hold --
    as opposed to a review-stage flip (to the registered colleague, who
    has not reviewed yet), a self-flip (still working on it, no
    merge-readiness claim being made), or a colleague sending it BACK TO
    THE AUTHOR after finding a blocker (#2961 -- see below).

    Fires on:
      - the literal pool sentinel (the single-human-owner convention
        `_is_legacy_pool_sentinel` already recognizes), OR
      - any OTHER agent -- the actual convention is to flip to a NAMED
        maintainer agent (e.g. "ariadne", "borges"), not the literal
        sentinel -- PROVIDED it is not a self-flip, not the flip TO the
        registered colleague (that flip is the "please review" signal,
        not "ready to merge", and firing CI/approval checks on it would
        be premature -- the colleague hasn't reviewed yet), and not a flip
        TO THE AUTHOR (#2961: a colleague returning the baton to the PR's
        own author after finding a blocker matches neither exemption above
        -- the target is not the current holder (the colleague holds it)
        and not the colleague (the colleague IS the actor) -- so without
        this third exemption it was misclassified as presenting-for-merge
        and demanded a colleague approval that cannot exist, since the
        colleague is the one objecting, not approving).

    `author` is optional and empty on a baton created before #2961 (no
    `author:` frontmatter field) -- an empty/falsy `author` simply means
    this exemption cannot fire, identical to pre-#2961 behavior.

    THIS COPY MUST STAY IN SYNC WITH tools/baton.py's -- see this module's
    top-of-file scope note for why it is a duplicate, not an import, and
    tests/test_baton_gate.py::TestSentinelPredicateParity for the pinning
    test.

    Caller guarantees `to_agent` is already either the pool sentinel or a
    genuine registered participant -- this function does not re-validate
    participant membership.

    May raise PoolSentinelUnresolved (via _is_legacy_pool_sentinel) --
    callers must treat that as gate-unavailable and fail closed.
    """
    if _is_legacy_pool_sentinel(to_agent):
        return True
    if to_agent == current_holder:
        return False
    if colleague_name and to_agent == colleague_name:
        return False
    if author and to_agent == author:
        return False
    return True


# ---------------------------------------------------------------------------
# CI-state helper for the flip guard (#685 / #2884)
# ---------------------------------------------------------------------------

# Conclusions that constitute a hard failure
_CI_RED_CONCLUSIONS = frozenset({
    "FAILURE", "ERROR", "CANCELLED", "TIMED_OUT",
    "ACTION_REQUIRED", "STARTUP_FAILURE",
})

# Statuses (or conclusions) that indicate a check is still running
_CI_PENDING_STATUSES = frozenset({
    "QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED",
})


def _dedupe_checks_by_latest(checks: list) -> list:
    """Keep only the latest run per check name.

    statusCheckRollup is scoped to the PR's head commit, but it carries
    EVERY run on that commit, not just the latest.  Two runs of one check
    can land on the same SHA: the workflow fires on `edited` as well as
    `synchronize`, so a trigger that changes no code can start a second run
    on a SHA already checked.  What the earlier run leaves behind turns on
    whether it was still in flight — if it was, `cancel-in-progress` on the
    per-PR concurrency group kills it and it concludes CANCELLED; if it had
    already finished, nothing is cancelled and its own conclusion (a FAILURE,
    say) simply stays.  Either way a stale entry sits beside the current
    SUCCESS, and iterating naively finds it and false-blocks the gate.  Runs
    on superseded SHAs never enter the rollup at all, so there is no
    cross-SHA case (measured 2026-08-30; this docstring claimed one until
    then).

    ISO-8601 completedAt strings compare correctly as plain strings
    (lexicographic == chronological).  A null/missing completedAt (check
    still in progress) sorts before any finished timestamp, so an in-progress
    re-run correctly loses to the completed previous run of the same name —
    the pending state is then caught by the pending-pass below.

    Edge case: if two runs for the same name both have completedAt=None
    (both in-progress), the first one seen is kept.  In practice both would
    be in-progress and the pending-pass catches them regardless of which wins.
    """
    latest: dict = {}
    for check in checks:
        name = check.get("name") or check.get("context") or ""
        completed_at = check.get("completedAt") or ""
        existing = latest.get(name)
        if existing is None or completed_at > (existing.get("completedAt") or ""):
            latest[name] = check
    return list(latest.values())


# ---------------------------------------------------------------------------
# Service GitHub identity (#3283).
#
# The three `gh` call sites below (`_pr_ci_state`, `_pr_approval_state`,
# `_reviewer_approval_state`) used to inherit the caller's environment. For the
# agent CLI that is intended (the agent's own gh login). For the FORUM SERVICE it
# meant the gate authenticated as whatever agent account the service runs under
# (broad `repo` scope, via that account's ~/.config/gh/hosts.yml).
#
# So the forum server -- and only the forum server -- calls
# `configure_service_github_identity()` once at startup. From then on every gh
# call runs under an EXPLICIT minimal env carrying a dedicated read-only token
# (GH_TOKEN) and an empty service-owned GH_CONFIG_DIR, and a missing token
# yields ("unknown", ...) BEFORE any subprocess call (`advance` already refuses
# on approval-side unknown). Until it is called -- the CLI, and every test that
# does not opt in -- behaviour is unchanged: gh inherits the environment and
# nothing is refused.
# ---------------------------------------------------------------------------

_SERVICE_IDENTITY_MISSING_MSG = (
    "GitHub gate identity not configured: the forum service has no dedicated "
    "read-only GH_TOKEN (#3283), so the gate refuses to call gh under an ambient login; "
    "set FORUM_GITHUB_INTEGRATION=off on the forum server for advisory mode, "
    "or install a read-only token"
)
_DEFAULT_GH_PATH = "/usr/local/bin:/usr/bin:/bin"


class _ServiceIdentity:
    """Module-private holder. repr/str never include the token."""

    __slots__ = ("on", "token", "config_dir")

    def __init__(self) -> None:
        self.on = False
        self.token = ""
        self.config_dir = ""

    def __repr__(self) -> str:
        return f"<_ServiceIdentity on={self.on} token={'set' if self.token else 'absent'}>"

    __str__ = __repr__


_SERVICE_IDENTITY = _ServiceIdentity()


def configure_service_github_identity(environ=None, config_dir=None) -> bool:
    """Switch the gate into service-identity mode (forum server, once, at startup).

    Reads ``GH_TOKEN`` from ``environ`` (default ``os.environ``) ONCE, here, and
    keeps it module-privately. Absent / empty / whitespace-only token, or no
    ``config_dir``, leaves the mode ON with no identity: every gh call site then
    returns ``unknown`` without invoking gh. ``config_dir`` is an empty directory
    the service owns; nothing is ever written into it. Returns True iff a usable
    identity (token AND config_dir) is now configured.

    ONCE-ONLY per process: the forum server removes ``GH_TOKEN`` from ``os.environ``
    after this captured it, so a second call with ``environ=None`` would see no
    token and switch the gate (and the board read that shares it) to no identity.
    """
    env = os.environ if environ is None else environ
    token = str(env.get("GH_TOKEN") or "").strip()
    _SERVICE_IDENTITY.on = True
    _SERVICE_IDENTITY.token = token
    _SERVICE_IDENTITY.config_dir = str(config_dir) if config_dir else ""
    return bool(token and _SERVICE_IDENTITY.config_dir)


def _service_identity_is_on() -> bool:
    return _SERVICE_IDENTITY.on


def _service_identity_reset() -> None:
    """Back to default-off (tests only; the server never calls this)."""
    _SERVICE_IDENTITY.on = False
    _SERVICE_IDENTITY.token = ""
    _SERVICE_IDENTITY.config_dir = ""


def _gh_identity():
    """Return (env_kwargs, refusal) for one gh call.

    Default-off: ({}, None) -- no `env=` kwarg at all, today's behaviour.
    Service mode with an identity: ({"env": <explicit minimal env>}, None).
    Service mode without one: ({}, ("unknown", <fixed message>)) -- the caller
    returns the refusal before any subprocess call.
    """
    ident = _SERVICE_IDENTITY
    if not ident.on:
        return {}, None
    if not ident.token or not ident.config_dir:
        return {}, ("unknown", _SERVICE_IDENTITY_MISSING_MSG)
    return {"env": {
        "PATH": os.environ.get("PATH") or _DEFAULT_GH_PATH,
        "GH_TOKEN": ident.token,
        "GH_CONFIG_DIR": ident.config_dir,
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
    }}, None


def gh_call_identity():
    """Public name of `_gh_identity` for callers outside this module (#3509).

    The forum server injects it into ``board_projects.configure_gh_identity`` so
    the board's PR-state read runs under the gate's identity. Same contract:
    ``({}, None)`` default-off; ``({"env": ...}, None)`` with an identity;
    ``({}, ("unknown", <fixed message>))`` in service mode without one.
    """
    return _gh_identity()


def _scrub_gh_detail(text: str) -> str:
    """Replace every occurrence of the service token with ``***`` (no-op otherwise)."""
    tok = _SERVICE_IDENTITY.token
    if not (_SERVICE_IDENTITY.on and tok):
        return text
    return text.replace(tok, "***")


# ---------------------------------------------------------------------------
# CI state from workflow runs (#3533)
#
# GitHub's fine-grained PAT UI has no "Checks" permission, so under the forum
# service's token `gh pr view --json statusCheckRollup` is refused with
# "Resource not accessible by personal access token".  The same token (Pull
# requests, Commit statuses, Actions, Metadata: Read) CAN read the workflow runs
# of a head commit, which carry the same conclusions, superseded runs included.
# Every mapping below fails closed: only a completed run whose conclusion is
# exactly "success" is green, and anything unestablished is unknown or pending.
# ---------------------------------------------------------------------------

_RUNS_FALLTHROUGH_MARKER = "resource not accessible by personal access token"
_HEAD_OID_RE = re.compile(r"[0-9a-f]{40}")
_CI_REPO_SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_RUN_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_RUN_RANK_GREEN, _RUN_RANK_PENDING, _RUN_RANK_RED = 0, 1, 2


def _gh_identity_is_service(gh_kw) -> bool:
    """True iff `gh_kw` (from `_gh_identity`) carries the service token env.

    Default-off identity is ``{}`` (the ambient login); a configured service
    identity is ``{"env": {..., "GH_TOKEN": <token>, ...}}``.
    """
    env = gh_kw.get("env") if isinstance(gh_kw, dict) else None
    return isinstance(env, dict) and bool(env.get("GH_TOKEN"))


def _repo_slug_is_valid(repo) -> bool:
    """A plain ``owner/name`` slug, safe to place in an API path."""
    if not (isinstance(repo, str) and _CI_REPO_SLUG_RE.fullmatch(repo)):
        return False
    return all(part.strip(".") for part in repo.split("/"))


def _ci_state_from_workflow_runs(raw: str) -> tuple:
    """Map the JSON of `actions/runs?head_sha=<oid>` to (state, detail).

    Newest run per workflow ``name`` by ``updated_at`` (an exact tie takes the
    worst); a newest run that is not completed, or has no conclusion, is pending;
    ``success`` is green; every other conclusion is red.  Red beats pending
    beats green across names.  No runs is pending.  A body that is unparseable,
    truncated (``total_count`` above the runs returned) or has a run this
    function cannot read is unknown.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return ("unknown", "gh returned unparseable JSON")
    if not isinstance(data, dict):
        return ("unknown", "workflow runs body is not a JSON object")
    runs = data.get("workflow_runs")
    if not isinstance(runs, list):
        return ("unknown", "workflow_runs missing or not a list")
    total = data.get("total_count")
    if not isinstance(total, int) or isinstance(total, bool):
        return ("unknown", "total_count missing or not an integer")
    if total > len(runs):
        return ("unknown", f"workflow runs truncated ({len(runs)} of {total} returned)")

    newest: dict = {}
    for item in runs:
        if not isinstance(item, dict):
            return ("unknown", "a workflow run is not an object")
        name = item.get("name")
        if not (isinstance(name, str) and name):
            return ("unknown", "a workflow run has no name")
        stamp = item.get("updated_at")
        if not isinstance(stamp, str):
            return ("unknown", f"workflow run '{name}' has no updated_at")
        try:
            when = datetime.strptime(stamp, _RUN_TIMESTAMP_FORMAT)
        except ValueError:
            return ("unknown", f"workflow run '{name}' has an unparseable updated_at")
        status = item.get("status")
        conclusion = item.get("conclusion")
        if not isinstance(status, str):
            return ("unknown", f"workflow run '{name}' has no status")
        if conclusion is not None and not isinstance(conclusion, str):
            return ("unknown", f"workflow run '{name}' has a malformed conclusion")
        if status != "completed" or conclusion is None:
            rank, why = _RUN_RANK_PENDING, f"workflow '{name}' status={status}"
        elif conclusion == "success":
            rank, why = _RUN_RANK_GREEN, f"workflow '{name}' conclusion=success"
        else:
            rank, why = _RUN_RANK_RED, f"workflow '{name}' conclusion={conclusion}"
        kept = newest.get(name)
        if kept is None or when > kept[0] or (when == kept[0] and rank > kept[1]):
            newest[name] = (when, rank, why)

    if not newest:
        return ("pending", "no workflow runs on the head")
    for wanted, label in ((_RUN_RANK_RED, "red"), (_RUN_RANK_PENDING, "pending")):
        for _when, rank, why in newest.values():
            if rank == wanted:
                return (label, why)
    return ("green", "all workflow runs succeeded")


def _ci_state_from_rollup_output(result) -> tuple:
    """Map the output of `gh pr view --json statusCheckRollup,mergeStateStatus`.

    Moved verbatim out of `_pr_ci_state` (#3533) so the rollup path and the
    workflow-runs path can sit side by side there; behaviour is unchanged.
    """
    raw = (result.stdout or "").strip()
    if not raw:
        return ("unknown", "gh returned empty output")

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return ("unknown", "gh returned unparseable JSON")

    # mergeStateStatus=CLEAN is GitHub's authoritative "latest run per check
    # all passed" verdict — trust it directly and skip per-check analysis.
    merge_state = (data.get("mergeStateStatus") or "").upper()
    if merge_state == "CLEAN":
        return ("green", "mergeStateStatus=CLEAN")

    checks = data.get("statusCheckRollup")
    if not isinstance(checks, list):
        return ("unknown", "statusCheckRollup missing or not a list")

    if not checks:
        # No checks registered — treat as green
        return ("green", "no checks registered")

    # Dedupe: keep only the latest run per check name so a superseded same-SHA
    # run (e.g. a CANCELLED one the concurrency group killed) can't false-block (#1366).
    checks = _dedupe_checks_by_latest(checks)

    for check in checks:
        # Handle both check-run shape (conclusion) and status-context shape (state/status)
        conclusion = (check.get("conclusion") or "").upper()
        state = (check.get("state") or check.get("status") or "").upper()

        if conclusion in _CI_RED_CONCLUSIONS:
            name = check.get("name") or check.get("context") or "unknown check"
            return ("red", f"check '{name}' conclusion={conclusion}")
        if state in _CI_RED_CONCLUSIONS:
            name = check.get("name") or check.get("context") or "unknown check"
            return ("red", f"check '{name}' state={state}")

    # Second pass: look for pending (no reds found above)
    for check in checks:
        conclusion = (check.get("conclusion") or "").upper()
        state = (check.get("state") or check.get("status") or "").upper()

        if state in _CI_PENDING_STATUSES:
            name = check.get("name") or check.get("context") or "unknown check"
            return ("pending", f"check '{name}' status={state}")
        if not conclusion and not state:
            # conclusion=null + no status means it hasn't started yet
            name = check.get("name") or check.get("context") or "unknown check"
            return ("pending", f"check '{name}' not yet started")

    return ("green", "all checks passed")



def _pr_ci_state(pr_number: str, repo: str) -> tuple:
    """Query GitHub CI state for a pull request via `gh`.

    Returns (state, detail) where state is one of:
      "green"   — all checks passed (or no checks registered)
      "red"     — at least one check failed
      "pending" — no failure, but at least one check still running
      "unknown" — could not obtain a verdict (degraded; advisory only)

    #1715 / #1918: `repo` (owner/repo) is REQUIRED, not optional. Every
    caller resolves it from a repo-qualified anchor before calling this --
    that guarantee used to be enforced only by caller discipline (`repo:
    Optional[str] = None`, falling back to gh's cwd-based inference when
    omitted). #1918 removed baton.py's DEFAULT_GITHUB_REPO precisely
    because a silently-guessed repo is how a cross-repo PR-baton ends up
    checking the WRONG repo's PR entirely -- cwd inference is that same
    failure mode one function down, just unnamed. Making `repo` required
    at the signature enforces the guarantee structurally rather than by
    every caller remembering to pass it (all six production call sites
    already did; this closes the latent re-entry point for a future one
    that doesn't).
    """
    if not repo:
        return ("unknown", "repo argument is required and was empty")
    gh_kw, refusal = _gh_identity()
    if refusal is not None:
        return refusal
    if not _gh_identity_is_service(gh_kw):
        cmd = ["gh", "pr", "view", pr_number,
               "--json", "statusCheckRollup,mergeStateStatus", "--repo", repo]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                **gh_kw,
            )
        except FileNotFoundError:
            return ("unknown", "gh not found on PATH")
        except subprocess.TimeoutExpired:
            return ("unknown", "gh timed out after 15s")
        except OSError as exc:
            return ("unknown", _scrub_gh_detail(f"gh exec error: {exc}"))

        if result.returncode != 0:
            detail = (result.stderr or "").strip() or f"gh exit {result.returncode}"
            if _RUNS_FALLTHROUGH_MARKER not in detail.lower():
                return ("unknown", _scrub_gh_detail(detail))
            # The fine-grained-PAT refusal of the check-run rollup (#3533): read
            # the workflow runs below instead.  Every OTHER failure stays unknown.
        else:
            return _ci_state_from_rollup_output(result)

    # Workflow-runs path (#3533): the service token cannot read the check-run
    # rollup, so ask for mergeStateStatus + headRefOid, then the runs on that
    # head.  Anything this path cannot establish is unknown or pending, never green.
    cmd_b = ["gh", "pr", "view", pr_number,
             "--json", "mergeStateStatus,headRefOid", "--repo", repo]
    try:
        res_b = subprocess.run(
            cmd_b,
            capture_output=True,
            text=True,
            timeout=15,
            **gh_kw,
        )
    except FileNotFoundError:
        return ("unknown", "gh not found on PATH")
    except subprocess.TimeoutExpired:
        return ("unknown", "gh timed out after 15s")
    except OSError as exc:
        return ("unknown", _scrub_gh_detail(f"gh exec error: {exc}"))

    if res_b.returncode != 0:
        detail = (res_b.stderr or "").strip() or f"gh exit {res_b.returncode}"
        return ("unknown", _scrub_gh_detail(detail))

    raw_b = (res_b.stdout or "").strip()
    if not raw_b:
        return ("unknown", "gh returned empty output")
    try:
        data_b = json.loads(raw_b)
    except (json.JSONDecodeError, ValueError):
        return ("unknown", "gh returned unparseable JSON")
    if not isinstance(data_b, dict):
        return ("unknown", "gh returned JSON that is not an object")

    merge_state = data_b.get("mergeStateStatus")
    if isinstance(merge_state, str) and merge_state.upper() == "CLEAN":
        return ("green", "mergeStateStatus=CLEAN")

    head_oid = data_b.get("headRefOid")
    if not (isinstance(head_oid, str) and _HEAD_OID_RE.fullmatch(head_oid)):
        return ("unknown", "headRefOid missing or not a 40-hex commit id")
    if not _repo_slug_is_valid(repo):
        return ("unknown", "repo is not a plain owner/name slug")

    cmd_c = ["gh", "api", f"repos/{repo}/actions/runs?head_sha={head_oid}&per_page=100"]
    try:
        res_c = subprocess.run(
            cmd_c,
            capture_output=True,
            text=True,
            timeout=15,
            **gh_kw,
        )
    except FileNotFoundError:
        return ("unknown", "gh not found on PATH")
    except subprocess.TimeoutExpired:
        return ("unknown", "gh timed out after 15s")
    except OSError as exc:
        return ("unknown", _scrub_gh_detail(f"gh exec error: {exc}"))

    if res_c.returncode != 0:
        detail = (res_c.stderr or "").strip() or f"gh exit {res_c.returncode}"
        return ("unknown", _scrub_gh_detail(detail))

    raw_c = (res_c.stdout or "").strip()
    if not raw_c:
        return ("unknown", "gh returned empty output")
    state, detail = _ci_state_from_workflow_runs(raw_c)
    return (state, _scrub_gh_detail(detail))


# ---------------------------------------------------------------------------
# Reads for the two approval states (#3541)
#
# The forum service's fine-grained token cannot read GraphQL
# `reviews{commit{oid}}` (it needs Contents:Read), but REST
# `GET repos/<repo>/pulls/<N>/reviews` serves `commit_id`, and `gh pr view
# --json headRefOid,author` serves the head and the PR author, under the same
# token (both measured live; REST `pulls/<N>` was NOT measured, so it is not
# used).  Both approval states read through the ONE private reader below.
# Every mapping fails closed: anything unestablished is unknown / incomplete /
# stale, never fresh / approved_current.
# ---------------------------------------------------------------------------

# Page size / hard page cap for the review walk.  A page with fewer rows than
# the page size is the last one; a full page needs the next page fetched.
# Hitting the cap with the last page still full means the walk is INCOMPLETE
# and the gate refuses -- a truncated read must never read as complete.
_REVIEW_PAGE_SIZE = 100
_REVIEW_PAGE_CAP = 10


def _gh_read_json(args: list) -> tuple:
    """Run ``gh <args>`` and parse its stdout as JSON.

    The only process spawn of the approval reads.  It takes the identity itself
    (`_gh_identity`), so no call can run without it: a refusal comes back as the
    reason before any spawn.  Returns ``(parsed_json, None)`` or
    ``(None, scrubbed_reason)``; never raises.  The argv is exactly
    ``["gh", *args]`` (no token, no ``--jq``, no ``--paginate``).
    """
    gh_kw, refusal = _gh_identity()
    if refusal is not None:
        return None, refusal[1]
    try:
        res = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=15, **gh_kw)
    except FileNotFoundError:
        return None, "gh not found on PATH"
    except subprocess.TimeoutExpired:
        return None, "gh timed out after 15s"
    except Exception as exc:  # noqa: BLE001 -- fail closed on anything else
        return None, _scrub_gh_detail(f"gh exec error: {exc}")
    if res.returncode != 0:
        stderr = res.stderr if isinstance(res.stderr, str) else ""
        return None, _scrub_gh_detail(stderr.strip() or f"gh exit {res.returncode}")
    if res.stdout is not None and not isinstance(res.stdout, str):
        return None, "gh returned non-text output"
    raw = (res.stdout or "").strip()
    if not raw:
        return None, "gh returned empty output"
    try:
        return json.loads(raw), None
    except (ValueError, RecursionError):
        return None, "gh returned unparseable JSON"


def _rest_pull_head(repo: str, pr_str: str) -> tuple:
    """Read the head and author with `gh pr view <N> --json headRefOid,author
    --repo <repo>`: ``(view_object, None)`` or ``(None, reason)``.

    `headRefOid` must be a 40-char lowercase hex commit id; anything else is a
    refusal (the value is only ever compared for equality).
    """
    body, why = _gh_read_json(
        ["pr", "view", pr_str, "--json", "headRefOid,author", "--repo", repo])
    if why is not None:
        return None, why
    if not isinstance(body, dict):
        return None, "pr view body is not a JSON object"
    sha = body.get("headRefOid")
    if not (isinstance(sha, str) and _HEAD_OID_RE.fullmatch(sha)):
        return None, "headRefOid missing or not a 40-hex commit id"
    return body, None


_PR_NOT_FOUND_TEXT = "could not resolve to a pullrequest"


def _pr_identity(pr_number: str, repo: str) -> tuple:
    """Does PR ``<repo>#<pr_number>`` exist, and who wrote it?  Never raises.

    ``("exists", author_login_lower)`` | ``("absent", reason)`` |
    ``("unknown", reason)``.  Reads `gh pr view <N> --json number,author --repo
    <repo>` through `_gh_read_json` (the one spawn point: service identity,
    scrubbed stderr).  ``absent`` is ONLY GitHub's not-found answer; every other
    failure (timeout, gh missing, auth, identity refusal, unparseable JSON, a
    missing or ill-typed author) is ``unknown`` -- a caller that must not guess
    (baton init, #3638) treats it as unreadable, never as absent or present.
    The repo slug and the number are validated before any spawn.
    """
    if not _repo_slug_is_valid(repo):
        return ("unknown", "repo is not a plain owner/name slug")
    if not (isinstance(pr_number, str) and _ascii_digits(pr_number)):
        return ("unknown", "PR number is not ASCII digits")
    body, why = _gh_read_json(
        ["pr", "view", pr_number, "--json", "number,author", "--repo", repo])
    if why is not None:
        if _PR_NOT_FOUND_TEXT in why.lower():
            return ("absent", why)
        return ("unknown", why)
    author = body.get("author") if isinstance(body, dict) else None
    login = author.get("login") if isinstance(author, dict) else None
    if not (isinstance(login, str) and login.strip()):
        return ("unknown", "pr view has no author login")
    return ("exists", login.strip().lower())


def _rest_review_rows(repo: str, pr_str: str) -> tuple:
    """Walk `pulls/<N>/reviews` by explicit page: ``(rows, None)`` or
    ``(None, (state, reason))`` with state ``unknown`` or ``incomplete``."""
    rows: list = []
    for page in range(1, _REVIEW_PAGE_CAP + 1):
        body, why = _gh_read_json(
            ["api", f"repos/{repo}/pulls/{pr_str}/reviews?per_page={_REVIEW_PAGE_SIZE}&page={page}"])
        if why is not None:
            return None, ("unknown", why)
        if not isinstance(body, list):
            return None, ("unknown", "reviews page is not a JSON list")
        for item in body:
            if not isinstance(item, dict):
                return None, ("unknown", "a review row is not an object")
            if not isinstance(item.get("state"), str):
                return None, ("unknown", "a review row has no string state")
        rows.extend(body)
        if len(body) < _REVIEW_PAGE_SIZE:
            return rows, None
    return None, (
        "incomplete",
        f"reviews reached the {_REVIEW_PAGE_CAP}-page cap "
        f"({_REVIEW_PAGE_CAP * _REVIEW_PAGE_SIZE} rows) with the last page still full; "
        "refusing to decide from a partial read",
    )


def _rest_read_reviews(repo: str, pr_str: str, early=None) -> tuple:
    """Head, then every review page, then the head again.

    REST is not one atomic query, so the head is read once before the reviews
    and once after the last page: if it moved in between the read is refused.
    `early(pull)` may return a final ``(state, detail)`` verdict from the first
    head read alone (no review needs reading).  Returns ``(pull, rows, None)``
    or ``(None, None, (state, detail))``.
    """
    pull, why = _rest_pull_head(repo, pr_str)
    if why is not None:
        return None, None, ("unknown", why)
    if early is not None:
        verdict = early(pull)
        if verdict is not None:
            return None, None, verdict
    rows, failure = _rest_review_rows(repo, pr_str)
    if failure is not None:
        return None, None, failure
    after, why = _rest_pull_head(repo, pr_str)
    if why is not None:
        return None, None, ("unknown", why)
    if after["headRefOid"] != pull["headRefOid"]:
        return None, None, ("unknown", "PR head moved while reviews were being read")
    return pull, rows, None


# ---------------------------------------------------------------------------
# Approval-staleness helper for the flip guard (#1002 / #2884)
# ---------------------------------------------------------------------------

def _pr_approval_state(pr_number: str, repo: str) -> tuple:
    """Query GitHub for post-approval tip movement on a pull request via `gh`.

    Companion to _pr_ci_state (#974): that guard checks CI is green NOW;
    this one checks the green tip is the APPROVED tip — a commit pushed
    after the latest approval re-triggers CI and invalidates the reviewed
    state ("re-check after ANY post-approval commit", CLAUDE.md).

    Uses commit-identity comparison (headRefOid vs approval commit_id)
    rather than timestamp comparison. Timestamp comparison is evadable: a
    rebased or backdated commit can carry a committedDate older than the
    approval and falsely read as fresh. Oid comparison is exact — a rebased
    tip produces a new oid even when content is identical, and therefore
    reads stale BY DESIGN (conservative: a rebased tip was not the reviewed
    tip; the colleague must re-approve).

    #1715 / #1918: `repo` (owner/repo) is REQUIRED, not optional. This used
    to fall back to a `gh repo view` cwd-inference call when `repo` was
    omitted -- exactly the mechanism that let this guard silently check the
    wrong repo for a genuinely cross-repo PR-baton, and exactly the failure
    class #1918 removed baton.py's DEFAULT_GITHUB_REPO to prevent one layer
    up. That fallback path is gone; a caller with no repo-qualified anchor
    gets "unknown" (advisory, non-blocking) rather than a guess.

    #3541: REST, not GraphQL. `GraphQL reviews{commit{oid}}` needs Contents:Read,
    which the forum service's fine-grained token lacks; REST `GET
    repos/<repo>/pulls/<N>/reviews` serves `commit_id` and `gh pr view
    --json headRefOid,author` serves the head, with no extra token scope. The
    head is read before and after the review pages (moved -> unknown) and the
    review walk is paged by explicit `page=`; a truncated walk is "unknown",
    never "no_approval".

    Returns (state, detail) where state is one of:
      "fresh"       — at least one APPROVED review has commit_id == headRefOid
                      (an approval that reviewed exactly the current tip)
      "stale"       — APPROVED review(s) exist but none match the tip oid
                      (tip moved after approval, or tip was rebased)
      "no_approval" — PR has no APPROVED review; this gate passes through
                      (approval-existence is the colleague-layer's
                      jurisdiction, not an oid question)
      "unknown"     — could not obtain a verdict (degraded; advisory only)
    """
    # pr_number goes into a REST path below -- require a bare ASCII integer so
    # a malformed anchor can't smuggle path or query text.
    pr_str = str(pr_number).lstrip("#")
    if not _ascii_digits(pr_str):
        return ("unknown", f"PR number {pr_number!r} is not numeric")
    pr_number = pr_str

    if not repo:
        return ("unknown", "repo argument is required and was empty")
    if not _repo_slug_is_valid(repo):
        return ("unknown", f"repo {repo!r} is not owner/repo shaped")

    pull, rows, verdict = _rest_read_reviews(repo, pr_number)
    if verdict is not None:
        # an "incomplete" walk is "unknown" here: it must never read as no_approval
        return ("unknown", verdict[1])
    head_oid = pull["headRefOid"]

    # Walk review rows (REST order is oldest-first): collect APPROVED reviews
    # and check the oid match. A row with a null/missing commit_id is simply
    # non-matching, never an error. A row that is not APPROVED never counts.
    has_approval = False
    latest_approval_oid = None
    for item in rows:
        if item["state"].upper() != "APPROVED":
            continue
        has_approval = True
        node_oid = item.get("commit_id")
        if not isinstance(node_oid, str):
            node_oid = None
        latest_approval_oid = node_oid  # last APPROVED row in list = most recent
        if node_oid and node_oid == head_oid:
            short = head_oid[:8]
            return ("fresh", f"approval covers tip oid {short}")

    if not has_approval:
        return ("no_approval", "no APPROVED review on the PR")

    tip_short = head_oid[:8]
    approval_short = (latest_approval_oid or "unknown")[:8]
    return (
        "stale",
        f"tip oid {tip_short} not covered by any approval "
        f"(latest approval oid {approval_short})",
    )


# ---------------------------------------------------------------------------
# #3039 U2b -- the strict `peer -> merge` gate primitives
#
# Two pieces live here, both PURE (no printing, no sys.exit -- this module is
# imported into the long-running forum process):
#
#   1. `_load_login_map`  -- the agent -> GitHub-login map, read from a
#      PROTECTED file the server is told about at start (never a default
#      path, never shipped identities).
#   2. `_reviewer_approval_state` -- one reviewer login's CURRENT verdict on a
#      PR head.
#
# Deliberately NOT a retrofit of `_pr_approval_state` above: its other callers
# ask a broader question ("does ANY approval cover the tip?") and treat
# `unknown` as advisory.  This gate asks "did THIS mapped account approve THIS
# head, and is that still its latest opinion?", and every answer other than
# `approved_current` refuses -- `unknown` included.
#
# What this gate does NOT prove (stated, not hidden): it proves an
# authenticated non-author GitHub approval by an account in the reviewed map,
# on the current head.  It does not prove that the ASSIGNED colleague approved
# (the forum's `colleague` / `agent` fields are caller-declared and mutable),
# nor who ran the verb.
# ---------------------------------------------------------------------------

# Start option / env that names the protected map file.  There is NO default
# path in code: an absent option means the gate fails closed.
_BATON_LOGIN_MAP_ENV = "ENGRAM_BATON_LOGIN_MAP"

_LOGIN_MAP_VERSION = 1
_LOGIN_MAP_MAX_BYTES = 1024 * 1024


class LoginMapError(Exception):
    """The login map is absent, unreadable, malformed, or not trustworthy.

    The message names the cause (and the failing path component / agent key),
    so a caller can surface it verbatim as a refusal.
    """


def _login_map_fail(msg: str) -> "LoginMapError":
    return LoginMapError(f"baton login map: {msg}")


def _check_map_component(st, label: str, trusted: frozenset, *, want_dir: bool) -> None:
    """Owner/mode/type check on an OPEN fd's stat result (never a path stat)."""
    if want_dir:
        if not stat.S_ISDIR(st.st_mode):
            raise _login_map_fail(f"{label} is not a directory")
    elif not stat.S_ISREG(st.st_mode):
        raise _login_map_fail(f"{label} is not a regular file")
    if st.st_uid not in trusted:
        raise _login_map_fail(
            f"{label} has owner uid {st.st_uid}, which is neither root nor an "
            "admin uid named at server start"
        )
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise _login_map_fail(
            f"{label} is group- or world-writable (mode {oct(stat.S_IMODE(st.st_mode))})"
        )


class LoginMap(dict):
    """``{agent(lower): [login(lower), ...]}`` plus ``.repos``.

    ``repos`` is the admin-owned id-prefix table (#3039 U2b round 4, A2): an
    UPPER-CASED baton-id prefix (``PR``, ``UMWELT-PR``) -> the lower-cased
    ``owner/repo`` a baton with that prefix may be anchored to. It is a dict
    subclass so existing callers that treat the result as the agent map keep
    working unchanged.
    """

    repos: dict = {}


def _validate_repos(raw_repos) -> dict:
    """Schema validation of the ``repos`` table; returns {PREFIX: "owner/repo"}."""
    if not isinstance(raw_repos, dict) or not raw_repos:
        raise _login_map_fail(
            "'repos' must be a non-empty object of baton-id prefix -> \"owner/repo\" "
            "(the strict peer->merge gate binds a baton's id to its anchor's repo)"
        )
    out: dict = {}
    for prefix, repo in raw_repos.items():
        if not isinstance(prefix, str) or not prefix.strip():
            raise _login_map_fail(f"'repos' prefix {prefix!r} must be a non-empty string")
        key = prefix.strip().upper()
        if key in out:
            raise _login_map_fail(
                f"'repos' prefix {key!r} appears twice (prefixes are case-insensitive)"
            )
        if not isinstance(repo, str) or not _REPO_SLUG_RE.match(repo.strip()):
            raise _login_map_fail(
                f"'repos' entry for prefix {key!r} must be an \"owner/repo\" string, got {repo!r}"
            )
        out[key] = repo.strip().lower()
    return out


def _validate_login_map(data) -> dict:
    """Schema validation; returns a :class:`LoginMap`
    ({agent(lower): [login(lower), ...]} with ``.repos``)."""
    if not isinstance(data, dict):
        raise _login_map_fail("top level must be a JSON object")
    version = data.get("version")
    if isinstance(version, bool) or version != _LOGIN_MAP_VERSION:
        raise _login_map_fail(
            f"missing or unsupported 'version' (expected {_LOGIN_MAP_VERSION}, got {version!r})"
        )
    raw_map = data.get("map")
    if not isinstance(raw_map, dict):
        raise _login_map_fail("'map' must be an object of agent -> [github logins]")
    out: dict = {}
    owner_of: dict = {}
    for agent, logins in raw_map.items():
        if not isinstance(agent, str) or not agent.strip():
            raise _login_map_fail(f"agent key {agent!r} must be a non-empty string")
        key = agent.strip().lower()
        if key in out:
            raise _login_map_fail(f"agent {key!r} appears twice (keys are case-insensitive)")
        if not isinstance(logins, list):
            raise _login_map_fail(f"agent {key!r}: value must be a list of logins")
        if not logins:
            raise _login_map_fail(f"agent {key!r}: empty login list")
        cleaned = []
        for login in logins:
            if not isinstance(login, str) or not login.strip():
                raise _login_map_fail(f"agent {key!r}: each login must be a non-empty string")
            low = login.strip().lower()
            prior = owner_of.get(low)
            if prior is not None and prior != key:
                raise _login_map_fail(
                    f"login {low!r} is mapped to more than one agent ({prior!r} and {key!r})"
                )
            owner_of[low] = key
            if low not in cleaned:
                cleaned.append(low)
        out[key] = cleaned
    result = LoginMap(out)
    result.repos = _validate_repos(data.get("repos"))
    return result


# A baton id binds its anchor (#3039 U2b round 4, A2): ``<prefix>-<N>[-suffix]``.
# Non-greedy prefix, so ``UMWELT-PR-925`` is prefix ``UMWELT-PR`` / number 925 and
# ``PR-92-paper`` is prefix ``PR`` / number 92.
_ID_BIND_RE = re.compile(r"(?P<prefix>.+?)-(?P<num>[0-9]+)(?:-.+)?", re.IGNORECASE | re.DOTALL)


def _anchor_id_binding_refusal(project_id: str, anchor_repo: str, anchor_number: str,
                               repos: dict, anchor_shown: Optional[str] = None) -> Optional[str]:
    """None when the PR anchor is the one the baton id names, else the refusal text.

    The id must carry a number; its prefix must have an entry in the admin-owned
    ``repos`` table; the anchor's number must equal the id's number AND its
    ``owner/repo`` must equal that table entry (case-insensitive). PURE.

    ``anchor_shown`` (round 6, D1): when the caller DERIVED ``anchor_repo`` from
    this same table for an unqualified ``pr/<N>`` anchor, the literal anchor to
    name in the refusal text instead of the derived ``pr/<repo>/<N>``.
    """
    m = _ID_BIND_RE.fullmatch(project_id or "")
    if m is None:
        return (
            f"baton id {project_id!r} carries no PR number (expected <PREFIX>-<N>[-suffix]), "
            "so its anchor cannot be bound to it; the peer->merge advance applies only to "
            "a PR baton whose id names its PR."
        )
    prefix, number = m.group("prefix").upper(), m.group("num")
    want_repo = (repos or {}).get(prefix)
    if want_repo is None:
        return (
            f"baton id prefix {prefix!r} (from {project_id!r}) has no entry in the baton login "
            "map's 'repos' table, so the repository it may be anchored to is unknown. The "
            "admin must add the prefix."
        )
    number_ok = _same_number(number, str(anchor_number))
    repo_ok = (anchor_repo or "").lower() == want_repo
    if not (number_ok and repo_ok):
        shown = anchor_shown or "pr/%s/%s" % (anchor_repo, anchor_number)
        return (
            f"the github anchor of {project_id!r} does not match its id: expected "
            f"'pr/{want_repo}/{number}' (PR {number} from the id, repo {want_repo} for prefix "
            f"{prefix}) but the anchor is '{shown}'."
        )
    return None


def _load_login_map(
    path,
    *,
    extra_trusted_uids=(),
    trust_anchor: str = "/",
    _before_leaf_open=None,
) -> dict:
    """Load + validate the protected agent -> GitHub-login map.  Fails closed.

    Integrity rules (the forum server runs as an AGENT unix account, so
    "owned by the server's own uid" is NOT acceptable -- the file would be
    agent-editable):

      * every directory from the trust anchor down, and the file itself, must
        be owned by root or by an admin uid named EXPLICITLY at server start
        (`extra_trusted_uids`; never defaulted to the caller's own uid);
      * none of them may be group- or world-writable;
      * no component may be a symlink (each is opened with O_NOFOLLOW);
      * owner/mode are checked with fstat on the OPEN fd of each component,
        walked with dir_fd, so a swap or symlink replace between check and use
        cannot change what was verified.

    `trust_anchor` is "/" in production.  It is an injectable seam so tests
    (which cannot chown to root) can root the walk at a scratch directory; it
    must be an ancestor of `path`, and components at or above it are not
    checked.  `_before_leaf_open` is a test seam run after the directory walk
    and before the leaf is opened.
    """
    if path is None or not str(path).strip():
        raise _login_map_fail(
            "no login map configured (set --baton-login-map or "
            f"${_BATON_LOGIN_MAP_ENV}); the strict peer->merge gate fails closed"
        )
    path = str(path)
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        raise _login_map_fail(
            f"{path!r} must be an absolute, normalized path (no '..', '.', or doubled '/')"
        )
    anchor = os.path.normpath(str(trust_anchor))
    if not os.path.isabs(anchor):
        raise _login_map_fail("trust anchor must be absolute")
    anchor_prefix = anchor if anchor == "/" else anchor + "/"
    if not path.startswith(anchor_prefix):
        raise _login_map_fail(f"{path!r} is not under the trust anchor {anchor!r}")
    trusted = frozenset({0}) | frozenset(int(u) for u in (extra_trusted_uids or ()))

    rel = path[len(anchor_prefix):]
    comps = [c for c in rel.split("/") if c]
    if not comps:
        raise _login_map_fail(f"{path!r} names no file")

    cloexec = getattr(os, "O_CLOEXEC", 0)
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | cloexec
    fds: list = []
    try:
        try:
            cur = os.open(anchor, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | cloexec)
        except OSError as exc:
            raise _login_map_fail(f"cannot open trust anchor {anchor!r}: {exc.strerror}")
        fds.append(cur)
        walked = anchor
        if anchor == "/":
            _check_map_component(os.fstat(cur), "/", trusted, want_dir=True)
        for comp in comps[:-1]:
            walked = walked.rstrip("/") + "/" + comp
            try:
                nxt = os.open(comp, dir_flags, dir_fd=cur)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise _login_map_fail(
                        f"{walked} is a symlink or not a directory (symlinks are refused)"
                    )
                raise _login_map_fail(f"cannot open {walked}: {exc.strerror}")
            fds.append(nxt)
            _check_map_component(os.fstat(nxt), walked, trusted, want_dir=True)
            cur = nxt
        leaf = walked.rstrip("/") + "/" + comps[-1]
        if _before_leaf_open is not None:
            _before_leaf_open()
        try:
            leaf_fd = os.open(comps[-1], os.O_RDONLY | os.O_NOFOLLOW | cloexec, dir_fd=cur)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise _login_map_fail(f"{leaf} is a symlink (symlinks are refused)")
            raise _login_map_fail(f"cannot open {leaf}: {exc.strerror}")
        fds.append(leaf_fd)
        st = os.fstat(leaf_fd)
        _check_map_component(st, leaf, trusted, want_dir=False)
        if st.st_size > _LOGIN_MAP_MAX_BYTES:
            raise _login_map_fail(f"{leaf} is larger than {_LOGIN_MAP_MAX_BYTES} bytes")
        with os.fdopen(os.dup(leaf_fd), "rb") as fh:
            raw = fh.read(_LOGIN_MAP_MAX_BYTES + 1)
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass

    if len(raw) > _LOGIN_MAP_MAX_BYTES:
        raise _login_map_fail(f"{leaf} is larger than {_LOGIN_MAP_MAX_BYTES} bytes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _login_map_fail(f"{leaf} is not valid JSON")
    return _validate_login_map(data)


_REPO_SLUG_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def _reviewer_approval_state(pr_number, repo, reviewer_login) -> tuple:
    """One reviewer's CURRENT verdict on a PR head, via REST `gh api` (#3541).

    Reads `gh pr view <N> --json headRefOid,author` before and after
    `GET repos/<repo>/pulls/<N>/reviews` (paged by `page=`), all through
    `_gh_read_json`; no GraphQL `commit{oid}`, so no Contents:Read scope.

    Returns (state, detail) where state is one of:
      "approved_current"  -- the reviewer's latest opinionated review is an
                             APPROVED on exactly headRefOid.  The ONLY state
                             that lets `peer -> merge` pass.
      "changes_requested" -- latest opinionated review is CHANGES_REQUESTED
                             (an earlier APPROVED on the same head does not
                             survive it).
      "dismissed"         -- latest opinionated review is DISMISSED.
      "stale_head"        -- latest opinionated review is an APPROVED on an
                             older commit.
      "no_review"         -- this login has no opinionated review.
      "self_review"       -- the reviewer login is the PR's author.
      "incomplete"        -- the review walk hit the page cap with the last
                             page still full.
      "ambiguous"         -- two or more of the reviewer's opinionated reviews
                             share the MAXIMUM submitted_at and differ in state,
                             so which one is latest cannot be known.
      "unknown"           -- no verdict obtainable (gh missing / timeout /
                             non-zero / empty / unparseable / missing fields /
                             head moved while reading, or an opinionated review
                             of this reviewer whose submitted_at is null /
                             missing / not a string).

    Derivation (client-side, over ALL reviews, so it does not depend on what a
    convenience field's name implies): keep reviews by `reviewer_login`
    (case-insensitive; a null user never matches), drop COMMENTED and PENDING
    (they carry no opinion and must not revoke an approval), order by
    submitted_at with the numeric review id as tie-break (NEVER REST order, and
    ids compare as integers, not strings), and let the LAST remaining review
    decide.
    """
    pr_str = str(pr_number or "").lstrip("#")
    if not _ascii_digits(pr_str):
        return ("unknown", f"PR number {pr_number!r} is not numeric")
    if not _repo_slug_is_valid(repo):
        return ("unknown", f"repo {repo!r} is not owner/repo shaped")
    if not isinstance(reviewer_login, str) or not reviewer_login.strip():
        return ("unknown", "reviewer login is empty")
    wanted = reviewer_login.strip().lower()

    def _self_review(pull):
        if "author" not in pull:
            return ("unknown", "pr view body has no author field")
        owner = pull["author"]
        author_login = owner.get("login") if isinstance(owner, dict) else None
        if isinstance(author_login, str) and author_login.strip().lower() == wanted:
            return ("self_review", f"{reviewer_login} is the author of PR #{pr_str}")
        return None

    pull, rows, verdict = _rest_read_reviews(repo, pr_str, early=_self_review)
    if verdict is not None:
        return verdict
    head_oid = pull["headRefOid"]

    mine: list = []
    for node in rows:
        rid = node.get("id")
        if not isinstance(rid, int) or isinstance(rid, bool):
            return ("unknown", "review row missing an integer id")
        who = node.get("user")
        login = who.get("login") if isinstance(who, dict) else None
        if not isinstance(login, str) or login.strip().lower() != wanted:
            continue
        state = node["state"].upper()
        if state in ("COMMENTED", "PENDING"):
            continue
        oid = node.get("commit_id")
        if not isinstance(oid, str):
            oid = None
        submitted = node.get("submitted_at")
        if not isinstance(submitted, str) or not submitted.strip():
            # An opinionated review we cannot place in time makes "which one
            # is the latest" undecidable -- never guess an order (round 4, R3).
            return (
                "unknown",
                f"review {rid} by {reviewer_login} has no usable submitted_at; "
                "cannot order the reviewer's opinions",
            )
        mine.append((submitted, rid, state, oid))

    if not mine:
        return ("no_review", f"{reviewer_login} has no opinionated review on PR #{pr_str}")
    mine.sort(key=lambda r: (r[0], r[1]))
    # Round 5 (S4): look at EVERY review sharing the MAXIMUM timestamp, not just
    # the last two -- (CHANGES_REQUESTED, APPROVED, APPROVED) at one second must
    # not read as the id tie-break's "last" APPROVED.
    top_ts = mine[-1][0]
    tied_states = sorted({r[2] for r in mine if r[0] == top_ts})
    if len(tied_states) > 1:
        # Opinions with the SAME (latest) timestamp and different states: the
        # order between them is not knowable (an id tie-break is arbitrary),
        # so none may be treated as the latest (round 4, R3).
        return (
            "ambiguous",
            f"{reviewer_login} has opinionated reviews ({', '.join(tied_states)}) that share "
            f"the latest timestamp {top_ts}; their order is not knowable",
        )
    _ts, _rid, state, oid = mine[-1]
    if state == "APPROVED":
        if oid == head_oid:
            return ("approved_current", f"{reviewer_login} approved tip oid {head_oid[:8]}")
        return (
            "stale_head",
            f"{reviewer_login}'s latest approval is on {(oid or 'unknown')[:8]}, "
            f"tip is {head_oid[:8]}",
        )
    if state == "CHANGES_REQUESTED":
        return ("changes_requested", f"{reviewer_login}'s latest review requests changes")
    if state == "DISMISSED":
        return ("dismissed", f"{reviewer_login}'s latest review was dismissed")
    return ("unknown", f"unrecognized review state {state!r}")
