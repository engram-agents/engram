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

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# GitHub anchor parsing (moved verbatim from tools/baton.py; #2884)
# ---------------------------------------------------------------------------

# GitHub anchor value validation: pr/<N>, pr/<owner>/<repo>/<N> (repo-qualified,
# #1715), or project/<N>. Repo-qualification is REQUIRED for any gh call this
# file makes (#1918: there is no default repo) -- an unqualified pr/<N> is
# refused-and-counted by cmd_gc/cmd_flip/cmd_merge (and now api_project_flip)
# rather than resolved against a guess.
_GITHUB_ANCHOR_RE = re.compile(
    r"^(?:pr/(?:[\w.-]+/[\w.-]+/)?\d+|project/\d+)$"
)

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


def _parse_github_anchor(value: str) -> Optional[tuple]:
    """Parse a github anchor value into (kind, repo, number).

    kind is 'pr' or 'project'. repo is 'owner/repo' if the anchor is
    repo-qualified, else None (#1918: there is no default repo -- callers
    must refuse-and-count an unqualified PR anchor rather than guess one).
    number is the PR/project number as a string. Returns None if `value`
    doesn't match a known anchor shape (caller should treat as "no anchor").
    """
    if not value:
        return None
    m = re.match(r"^pr/(?:([\w.-]+)/([\w.-]+)/)?(\d+)$", value)
    if m:
        owner, repo, number = m.group(1), m.group(2), m.group(3)
        repo_qualified = f"{owner}/{repo}" if owner and repo else None
        return ("pr", repo_qualified, number)
    m = re.match(r"^project/(\d+)$", value)
    if m:
        return ("project", None, m.group(1))
    return None


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
    cmd = ["gh", "pr", "view", pr_number,
           "--json", "statusCheckRollup,mergeStateStatus", "--repo", repo]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return ("unknown", "gh not found on PATH")
    except subprocess.TimeoutExpired:
        return ("unknown", "gh timed out after 15s")
    except OSError as exc:
        return ("unknown", f"gh exec error: {exc}")

    if result.returncode != 0:
        detail = (result.stderr or "").strip() or f"gh exit {result.returncode}"
        return ("unknown", detail)

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


# ---------------------------------------------------------------------------
# Approval-staleness helper for the flip guard (#1002 / #2884)
# ---------------------------------------------------------------------------

def _pr_approval_state(pr_number: str, repo: str) -> tuple:
    """Query GitHub for post-approval tip movement on a pull request via `gh`.

    Companion to _pr_ci_state (#974): that guard checks CI is green NOW;
    this one checks the green tip is the APPROVED tip — a commit pushed
    after the latest approval re-triggers CI and invalidates the reviewed
    state ("re-check after ANY post-approval commit", CLAUDE.md).

    Uses commit-identity comparison (headRefOid vs approval commit oid)
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

    One gh call: gh api graphql querying headRefOid and
    reviews(last:50){state,commit{oid}}.

    Returns (state, detail) where state is one of:
      "fresh"       — at least one APPROVED review has commit.oid == headRefOid
                      (an approval that reviewed exactly the current tip)
      "stale"       — APPROVED review(s) exist but none match the tip oid
                      (tip moved after approval, or tip was rebased)
      "no_approval" — PR has no APPROVED review; this gate passes through
                      (approval-existence is the colleague-layer's
                      jurisdiction, not an oid question)
      "unknown"     — could not obtain a verdict (degraded; advisory only)
    """
    # pr_number is interpolated into the GraphQL query string below (unlike
    # the old argv-passed form, which the shell layer kept inert) — require a
    # bare integer so a malformed anchor can't inject query text.
    pr_str = str(pr_number).lstrip("#")
    if not pr_str.isdigit():
        return ("unknown", f"PR number {pr_number!r} is not numeric")
    pr_number = pr_str

    if not repo:
        return ("unknown", "repo argument is required and was empty")
    if "/" not in repo:
        return ("unknown", f"repo {repo!r} is not owner/repo shaped")
    owner, repo_name = repo.split("/", 1)

    # GraphQL — fetch headRefOid and last 50 reviews with commit oid.
    query = (
        "{ repository(owner: \"%s\", name: \"%s\") {"
        " pullRequest(number: %s) {"
        "  headRefOid"
        "  reviews(last: 50) { nodes { state commit { oid } } }"
        " } } }"
    ) % (owner, repo_name, pr_number)

    try:
        gql_result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return ("unknown", "gh not found on PATH")
    except subprocess.TimeoutExpired:
        return ("unknown", "gh timed out after 15s")
    except OSError as exc:
        return ("unknown", f"gh exec error: {exc}")

    if gql_result.returncode != 0:
        detail = (gql_result.stderr or "").strip() or f"gh exit {gql_result.returncode}"
        return ("unknown", detail)

    raw_gql = (gql_result.stdout or "").strip()
    if not raw_gql:
        return ("unknown", "gh returned empty output")

    try:
        gql_data = json.loads(raw_gql)
        pr_data = gql_data["data"]["repository"]["pullRequest"]
        head_oid = pr_data["headRefOid"]
        review_nodes = pr_data["reviews"]["nodes"]
    except (json.JSONDecodeError, ValueError):
        return ("unknown", "gh returned unparseable JSON")
    except (KeyError, TypeError):
        return ("unknown", "gh response missing expected fields")

    if not head_oid:
        return ("unknown", "headRefOid missing or empty")
    if not isinstance(review_nodes, list):
        return ("unknown", "reviews.nodes missing or not a list")

    # Walk review nodes: collect APPROVED reviews and check oid match.
    # A review node with commit: null is simply non-matching, never an error.
    has_approval = False
    latest_approval_oid = None
    for node in review_nodes:
        if not isinstance(node, dict):
            continue
        if (node.get("state") or "").upper() != "APPROVED":
            continue
        has_approval = True
        commit = node.get("commit")
        node_oid = commit.get("oid") if isinstance(commit, dict) else None
        latest_approval_oid = node_oid  # last APPROVED node in list = most recent
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
