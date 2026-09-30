#!/usr/bin/env python3
"""baton — Multi-Agent Turn-State CLI

A thin CLI wrapping the baton file protocol at
/home/agents-shared/forum/projects/. Provides explicit turn declarations for
shared projects (PRs, designs, etc.) so agents always know whose move it
is next.

Note: the old path /home/agents-shared/projects/ (pre-2026-06-27) may still
exist on some hosts and greps clean against it — that misdirection produced
a real wrong census (issue #2290). Always point at the forum/projects/ path
above.

Thirteen subcommands:
  init        Create a new project baton file
  flip        Pass the baton to another participant (PR layer)
  claim       Take a Project-layer baton from the pool (pool → self)
  release     Return a Project-layer baton to the pool (self → pool sentinel)
  status      List active projects (not merged or cancelled)
  mine        Shorthand for status --mine
  show        Display a project file
  close       Mark a project merged or cancelled
  reopen      Flip a closed baton back to an active status (inverse of close)
  gc          Batch-close PR-batons whose GitHub PR is merged or closed
  rename      Update the human-readable project title
  anchor      Set or update the github: anchor on an existing baton
  merge       Gate-checked squash merge: baton-turn + CI-green + approval-fresh

Design: Lei + Borges, 2026-05-28 (solving the importance-inflation-derivation-shaped misalignment
incident where Borges and Ariadne disagreed about whose court PR #425 was in).

Protocol ref: docs/baton-protocol.md
Hook ref:     hooks/claude/engram-baton-prompt-hook.py
"""

import argparse
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Phase 3: thin-client imports — same pattern as tools/ia.py.
# Explicit path insert ensures this import works when baton.py is loaded via
# importlib (e.g. in direct-import tests) — Python's automatic script-dir
# addition only fires when baton.py is the __main__ entry point.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from forum_api import (
    ForumClient,
    ForumHttpError,
    ForumNetworkError,
    forum_url_from_config,
)

# #2884 (second half): the pure CI-green / approval-freshness / repo-anchor
# gate logic cmd_flip runs is now SHARED with the REST endpoint
# (src/forum/server.py's api_project_flip) via baton_gate.py, so a direct
# API caller can no longer bypass the checks cmd_flip enforces. Imported by
# bare name (not `import baton_gate; baton_gate._pr_ci_state(...)`) so the
# existing monkeypatch.setattr(_baton, "_pr_ci_state", ...) test pattern
# keeps working unchanged -- patching baton.py's own module attribute is
# what every call site below actually resolves against.
#
# _pool_sentinel / _is_pool_sentinel / _is_maintainer_flip are DELIBERATELY
# NOT imported from baton_gate -- see baton_gate.py's module docstring
# ("Scope note") for why: they keep their own verbatim definitions further
# down in this file, unchanged by this refactor.
from baton_gate import (
    _parse_github_anchor,
    _dedupe_checks_by_latest,
    _pr_ci_state,
    _pr_approval_state,
    _GITHUB_ANCHOR_RE,
    _derive_legacy_author,
)

# ---------------------------------------------------------------------------
# Environment + paths
# ---------------------------------------------------------------------------

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)
# Exit codes
EXIT_OK = 0
EXIT_VALIDATION = 1
EXIT_IO = 2
EXIT_STATE = 3

# Valid project statuses
VALID_STATUSES = {"planning", "in-progress", "in-review", "merged", "cancelled"}
CLOSED_STATUSES = {"merged", "cancelled"}

# ---------------------------------------------------------------------------
# Loud-fail API helpers
# ---------------------------------------------------------------------------

def _api_get(client, path, params=None):
    """GET via forum API; fail loud on network error or server error."""
    try:
        return client.get(path, params=params)
    except ForumNetworkError as e:
        print(f"baton: UCS unreachable — {e}", file=sys.stderr)
        sys.exit(EXIT_IO)
    except ForumHttpError as e:
        print(f"baton: server error {e.status}: {e.body}", file=sys.stderr)
        sys.exit(EXIT_IO if e.status >= 500 else EXIT_VALIDATION)


def _api_get_raw(client, project_id):
    """GET a single project's raw markdown; clean exit on 404/unreachable."""
    try:
        return client.get(f"/api/projects/{project_id}")["raw"]
    except ForumNetworkError as e:
        print(f"baton: UCS unreachable — {e}", file=sys.stderr)
        sys.exit(EXIT_IO)
    except ForumHttpError as e:
        if e.status == 404:
            print(
                f"baton: project not found: '{project_id}'. "
                "Use 'baton status' to see active projects.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)
        print(f"baton: server error {e.status}: {e.body}", file=sys.stderr)
        sys.exit(EXIT_IO if e.status >= 500 else EXIT_VALIDATION)


def _require_forum(config=None) -> None:
    """Exit with EXIT_STATE if no forum is configured.

    Write commands call this guard at entry. Installs with no forum
    configured that run a write command get a clear actionable message
    rather than an obscure API error.
    """
    if not _is_forum_configured(config):
        print(
            "baton: no forum configured (set forum.url or agent_name in "
            "config.json); baton requires a configured forum.",
            file=sys.stderr,
        )
        sys.exit(EXIT_STATE)


# ---------------------------------------------------------------------------
# Config helpers (mirrors ia.py conventions)
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Load $ENGRAM_HOME/config.json. Returns {} on any failure."""
    config_path = Path(ENGRAM_HOME) / "config.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _get_agent_name(config: Optional[dict] = None) -> str:
    """Resolve this agent's own name.

    Priority:
      1. config.json["agent_name"] field (explicit; wins if set).
      2. $USER env var, agent- prefix stripped.
      3. $LOGNAME env var, agent- prefix stripped.
      4. pwd.getpwuid(os.getuid()).pw_name, agent- prefix stripped.
      5. Empty string.
    """
    if config is None:
        config = _load_config()
    name = config.get("agent_name", "").strip()
    if name:
        return name

    def _strip_agent_prefix(username: str) -> str:
        if username.startswith("agent-"):
            return username[len("agent-"):]
        return username

    for envvar in ("USER", "LOGNAME"):
        username = os.environ.get(envvar, "").strip()
        if username:
            return _strip_agent_prefix(username)

    try:
        username = pwd.getpwuid(os.getuid()).pw_name
        if username:
            return _strip_agent_prefix(username)
    except KeyError:
        pass

    return ""


def _is_forum_configured(config: Optional[dict] = None) -> bool:
    """Return True if forum is explicitly configured (url or agent_name set).

    Gates write commands: if neither forum.url nor agent_name is in config,
    baton refuses with an actionable message. Mirrors ia.py / the engram hook
    helper.
    """
    if config is None:
        config = _load_config()
    forum_cfg = config.get("forum", {})
    has_url = isinstance(forum_cfg, dict) and bool(forum_cfg.get("url", "").strip())
    has_agent = bool(config.get("agent_name", "").strip())
    return has_url or has_agent


# ---------------------------------------------------------------------------
# Frontmatter parsing and generation
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FRONTMATTER_FIELD_RE = re.compile(r"^(\w[\w-]*):\s*(.*)$", re.MULTILINE)

# Participants list format: [alice, bob] or alice,bob
_PARTICIPANTS_LIST_RE = re.compile(r"^\[?(.*?)\]?$")


def _parse_frontmatter(text: str) -> tuple:
    """Extract YAML-ish frontmatter from a baton markdown file.

    Returns (fields_dict, body_text). On parse failure returns ({}, text).
    Fields are returned as strings; callers parse participants specially.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_block = m.group(1)
    body = text[m.end():]
    fields: dict = {}
    for field_m in _FRONTMATTER_FIELD_RE.finditer(fm_block):
        key = field_m.group(1).strip().lower()
        val = field_m.group(2).strip()
        fields[key] = val
    return fields, body


def _parse_participants(participants_str: str) -> list:
    """Parse participants field: '[borges, ariadne]' or 'borges,ariadne'.

    Returns a list of lowercase stripped agent names.
    """
    # Strip surrounding brackets if present
    m = _PARTICIPANTS_LIST_RE.match(participants_str.strip())
    if m:
        inner = m.group(1)
    else:
        inner = participants_str
    parts = [p.strip().lower() for p in inner.split(",") if p.strip()]
    return parts


def _format_participants(participants: list) -> str:
    """Format participants list for frontmatter: [borges, ariadne]"""
    return "[" + ", ".join(participants) + "]"


def _parse_iso(ts_str: str) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp; return None on failure."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_str(ts: Optional[datetime]) -> str:
    """Return human-readable age string like '1h ago', '2d ago'."""
    if ts is None:
        return ""
    now = datetime.now(timezone.utc)
    delta = now - ts
    total_secs = int(delta.total_seconds())
    if total_secs < 0:
        return ""
    if total_secs < 60:
        return f"{total_secs}s ago"
    elif total_secs < 3600:
        return f"{total_secs // 60}m ago"
    elif total_secs < 86400:
        return f"{total_secs // 3600}h ago"
    else:
        return f"{total_secs // 86400}d ago"


# ---------------------------------------------------------------------------
# Project ID validation
# ---------------------------------------------------------------------------

_PROJECT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*-[A-Za-z0-9][A-Za-z0-9_-]*$")

# Matches a PR-baton's project_id by PREFIX ONLY: an optional
# single-segment repo-qualifier (e.g. "UMWELT-") followed by "PR-". This
# is a pure shape test -- what follows the "PR-" prefix (a bare number, or
# a number plus a suffix like "-paper", e.g. "PR-37-paper") is irrelevant
# to this gate; the actual PR number/repo always come from the
# separately-stored `github:` anchor field, never from this regex. #2010:
# the original prefix-only check required the id to START with "PR-", so
# a repo-qualified id like "UMWELT-PR-572" was invisible to this gate; a
# first fix over-corrected into a full-string ("...(\d+)$") match, which
# then made a live "PR-<N>-<repo-slug>" naming shape (e.g. "PR-37-paper")
# invisible too. This prefix-only form fixes both. Kept in sync by hand
# with the sibling copy in hooks/claude/engram-baton-prompt-hook.py.
_PR_BATON_NAME_RE = re.compile(r"^(?:[A-Z][A-Z0-9]*-)?PR-")

# _GITHUB_ANCHOR_RE and _parse_github_anchor now live in baton_gate.py
# (#2884, second half) -- imported above, shared with src/forum/server.py's
# api_project_flip. See baton_gate.py for the full "#1918: no default repo"
# rationale.


def _validate_project_id(project_id: str) -> Optional[str]:
    """Validate project ID format (TYPE-ID). Returns error string or None."""
    if not _PROJECT_ID_RE.match(project_id):
        return (
            f"baton: invalid project ID '{project_id}' — "
            "expected TYPE-ID format (e.g. PR-425, DESIGN-trust-tier-v2). "
            "Must start with a letter, use only letters/digits/hyphens/underscores, "
            "and contain at least one hyphen separating type from id."
        )
    return None







# _dedupe_checks_by_latest / _pr_ci_state / _pr_approval_state now live in
# baton_gate.py (#2884, second half) -- imported above, shared with
# src/forum/server.py's api_project_flip. See baton_gate.py for the full
# docstrings and the #685 / #1002 / #1715 / #1918 history.

# ---------------------------------------------------------------------------
# Project scanning
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Subcommand: init
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace, config: dict, agent_name: str, client: ForumClient) -> None:
    _require_forum(config)

    project_id = args.project_id
    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    # Parse participants
    participants_raw = args.participants
    participants = [p.strip().lower() for p in participants_raw.split(",") if p.strip()]
    if len(participants) < 1:
        print(
            "baton init: --participants requires at least one agent name "
            "(e.g. --participants borges,ariadne).",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # --colleague: add the named colleague to participants (dedup) (#1267 Fix B).
    # The colleague argument makes the colleague-gate enforceable at flip time:
    # if a colleague participant is present, `baton flip PR-N <sentinel>` with
    # no APPROVED review will be rejected. Without it, the gate falls back to
    # single-agent (warn-only) mode.
    colleague_name = getattr(args, "colleague", None)
    colleague_note = ""
    if colleague_name:
        colleague_name = colleague_name.strip().lower()
        if colleague_name and colleague_name not in participants:
            participants.append(colleague_name)
        colleague_note = f"Colleague reviewer: {colleague_name}"

    # Default turn: the invoking agent (if in participants) else first participant
    if args.turn:
        initial_turn = args.turn.strip().lower()
    elif agent_name and agent_name.lower() in participants:
        initial_turn = agent_name.lower()
    else:
        initial_turn = participants[0]

    # Validate initial turn
    if not _is_legacy_pool_sentinel(initial_turn) and initial_turn not in participants:
        print(
            f"baton init: turn '{initial_turn}' must be in participants "
            f"{participants} or the pool sentinel ({_pool_sentinel()!r}).",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    status = args.status or "planning"
    if status not in VALID_STATUSES:
        print(
            f"baton init: invalid status '{status}'. "
            f"Valid statuses: {', '.join(sorted(VALID_STATUSES))}.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Validate --github anchor value if provided
    github_anchor = getattr(args, "github", None)
    if github_anchor is not None:
        github_anchor = github_anchor.strip().lower()
        if not _GITHUB_ANCHOR_RE.match(github_anchor):
            print(
                f"baton init: invalid --github value '{github_anchor}'. "
                "Expected pr/<N>, pr/<owner>/<repo>/<N>, or project/<N> "
                "(e.g. pr/490, pr/engram-agents/engram-paper/22, project/4).",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)

    # Auto-derive a bare pr/<N> anchor from a PR-<N> project_id when the
    # caller didn't pass --github at all (#1715: every PR-baton should carry
    # SOME anchor from creation, not rely on downstream code re-deriving the
    # PR number from project_id -- that re-derivation, done ad hoc in two
    # different places pre-#1715, is exactly what let a repo-unqualified
    # match silently corrupt a cross-repo baton's stored status).
    if github_anchor is None:
        _pr_from_id = re.match(r"^(?:PR-|pr-)(\d+)$", project_id)
        if _pr_from_id:
            github_anchor = f"pr/{_pr_from_id.group(1)}"

    # --repo qualifies a pr/<N> anchor to pr/<owner>/<repo>/<N> (#1715).
    # Effectively REQUIRED for every PR-baton now (#1918: there is no
    # default repo) -- an unqualified anchor is refused-and-counted by
    # cmd_gc/cmd_flip/cmd_merge rather than reconciled against a guess.
    repo_arg = getattr(args, "repo", None)
    if repo_arg is not None:
        repo_arg = repo_arg.strip().lower()
        if not re.match(r"^[\w.-]+/[\w.-]+$", repo_arg):
            print(
                f"baton init: invalid --repo value '{repo_arg}'. "
                "Expected owner/repo (e.g. engram-agents/engram-paper).",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)
        parsed = _parse_github_anchor(github_anchor) if github_anchor else None
        if parsed is None or parsed[0] != "pr":
            print(
                "baton init: --repo only applies to a PR anchor "
                "(pr/<N> or a PR-<N> project_id) -- "
                f"got github anchor {github_anchor!r} for project_id {project_id!r}.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)
        # If --github was ALREADY repo-qualified with a DIFFERENT repo,
        # silently overwriting it with --repo's value would drop the
        # anchor's real repo with no warning (reviewer-fairy-flagged
        # operator-error case, #1715). Reject rather than guess which one
        # is intended.
        if parsed[1] is not None and parsed[1] != repo_arg:
            print(
                f"baton init: --github already specifies repo {parsed[1]!r}, "
                f"which conflicts with --repo {repo_arg!r}. Pass only one, "
                "or make them agree.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)
        github_anchor = f"pr/{repo_arg}/{parsed[2]}"

    # #1924: the mint path must not produce what the consume paths refuse.
    # #1918/#1920 made cmd_gc/cmd_flip/cmd_merge refuse-and-count an
    # unqualified pr/<N> anchor (there is no default repo to guess against),
    # but left cmd_init free to keep creating them -- including SILENTLY, via
    # the auto-derive above, on the plainest possible invocation
    # (`baton init PR-1234 --title ... --participants ...`). The result was a
    # baton born unmergeable: `baton merge` hard-refuses it later with
    # "run baton anchor first", and that refusal lands on whoever tries to
    # merge, days or weeks after the person who could trivially have avoided
    # it has moved on.
    #
    # REFUSE, not warn. This shipped first as a WARN because a first-draft
    # REFUSE broke 8 existing tests -- including
    # test_init_auto_derives_bare_anchor_from_pr_project_id, which exists
    # specifically to PIN bare auto-derivation as intended #1715 behavior.
    # Flipping a pinned contract is a team decision, not a follow-up's to
    # make unilaterally, so #1924 carried that governance question forward
    # rather than deciding it here.
    #
    # That question is now SETTLED (forum #411, 2026-09-13, Kepler ruling
    # with Aleph as design owner): REFUSE, not warn -- flip the #1715-pinned
    # contract deliberately as part of accepting this fix.
    #
    # The ruling is narrow: require an explicit --repo or refuse, with NO
    # cwd-inference of any kind. An earlier draft of the ruling proposed
    # resolving the repo from the checkout's git `origin` remote when
    # unambiguous; that was proposed, examined, and explicitly withdrawn
    # (#1918: there is NO default repo, deliberately -- do not revisit). A
    # cwd-inferred anchor is confidently wrong and fully qualified rather
    # than merely unresolvable: minting a UMWELT-PR-<N> baton from inside
    # a checkout of THIS project's own public repo would silently produce
    # pr/engram-agents/engram/<N>, a DIFFERENT repo's PR of the same
    # number, which passes every downstream gate silently -- worse than the
    # bare-anchor bug this section fixes, which at least gets refused loudly
    # by gc/flip/merge. There is no cwd-inference anywhere in this function,
    # deliberately.
    #
    # Scope note: fires only when an anchor RESOLVES TO A PR and is
    # unqualified. A baton with no github anchor at all (a DESIGN-* project,
    # or a PR-shaped id the auto-derive regex doesn't match, e.g.
    # UMWELT-PR-<N>) is untouched -- cmd_gc already counts those as ungauged,
    # and widening this to cover them needs a broader notion of "is this a PR
    # baton" than project_id shape alone. Tracked in #1924, not guessed at.
    _final_anchor = _parse_github_anchor(github_anchor) if github_anchor else None
    if _final_anchor is not None and _final_anchor[0] == "pr" and _final_anchor[1] is None:
        _n = _final_anchor[2]
        _derived = repo_arg is None and not getattr(args, "github", None)
        print(
            f"baton init: refusing to create '{project_id}' with the "
            f"unqualified PR anchor 'pr/{_n}'"
            + (" (auto-derived from the project id)" if _derived else "")
            + " -- there is no default repo (#1918), so gc/flip/merge would "
            "all refuse this baton later. Qualify it now:\n"
            f"    baton init {project_id} ... --repo <owner>/<repo>\n"
            f"    baton init {project_id} ... --github pr/<owner>/<repo>/{_n}",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    title = args.title or project_id
    init_reason = f"project initialized by {agent_name or 'unknown'}"

    # #3039 U2a round 3: the INTENDED initial phase, computed directly from
    # this transition's own semantics (not from _resolve_phase -- there is
    # nothing to fall back to; this is a fresh baton with no prior state).
    # PR-vs-bunch is determined the same way _is_pr_baton does -- by the
    # already-resolved --github anchor, else by project_id's name shape --
    # via the shared _is_pr_shaped helper (factored out of _is_pr_baton
    # specifically so this call site and cmd_gc's don't each hand-copy the
    # anchor/name rule a third time). `init` is ALWAYS a transition (per
    # Kepler's round-3 ruling): sent as `transition_phase`, not `phase`, so
    # the write-body's turn-log line reads "(transition)" rather than
    # "(legacy conversion)" on a baton that was never legacy (Borges's
    # smaller finding on #3254).
    _init_is_pr = _is_pr_shaped(project_id, github_anchor or "")
    _init_phase = "fairy" if _init_is_pr else "pool"

    # #3042: the author is the INVOKING agent -- whoever ran `baton init`
    # created this baton, regardless of whose court --turn puts it in.
    # #2961 originally persisted `initial_turn` here as a proxy, which is
    # correct only in the normal case where an author inits with --turn
    # pointing at themselves; with --turn naming someone else it durably
    # attributed authorship to a seat that did not create the baton, and
    # no verb repairs it afterwards. `agent_name` is the real value and is
    # already in scope (see "agent" above). Persisted once, here, into a
    # dedicated `author:` field that flip/claim/release never touch --
    # unlike `turn_by`, which IS overwritten on every turn-change and so
    # cannot serve as an author proxy past the very first flip. An empty
    # `agent_name` deliberately leaves the field unwritten so the legacy
    # log-derived proxy fires rather than a confidently wrong author.
    try:
        client.post("/api/projects", {
            "agent": agent_name or "baton",
            "project_id": project_id,
            "title": title,
            "status": status,
            "turn": initial_turn,
            "turn_reason": init_reason,
            "github": github_anchor,
            "participants": participants,
            "colleague": colleague_name,
            "author": agent_name,
            "transition_phase": _init_phase,
        })
    except ForumNetworkError as e:
        print(f"baton init: {e}", file=sys.stderr)
        sys.exit(EXIT_IO)
    except ForumHttpError as e:
        if e.status == 409:
            print(f"baton init: project already exists: {project_id}", file=sys.stderr)
        else:
            print(f"baton init: server error {e.status}: {e.body}", file=sys.stderr)
        sys.exit(EXIT_IO if e.status >= 500 else EXIT_VALIDATION)
    print(f"baton init: created {project_id} (via forum API)")
    print(f"  project:      {project_id}")
    print(f"  title:        {title}")
    print(f"  participants: {', '.join(participants)}")
    print(f"  turn:         {initial_turn}")
    print(f"  status:       {status}")
    if github_anchor:
        print(f"  github:       {github_anchor}")
    if colleague_name:
        print(f"  colleague:    {colleague_name}")

    # Colleague-gate enforceability warning for PR-named batons (#1267 Fix B).
    # If this is a PR baton (PR-N id) and no colleague is resolvable from
    # participants (participants ⊆ {initial turn holder, sentinel}), warn at
    # creation time
    # so the gap surfaces here rather than silently at flip.
    _is_bare_pr_id = re.match(r"^(?:PR-|pr-)(\d+)$", project_id)
    if _is_bare_pr_id:
        _sentinel = _pool_sentinel()
        # Colleague candidates: participants other than the initial turn
        # holder and the pool sentinel. (This is about the COLLEAGUE gate --
        # it is not where `author` is determined; see the #3042 note above.)
        _colleagues_at_init = [
            p for p in participants
            if p != initial_turn and p != _sentinel
        ]
        if not _colleagues_at_init:
            print(
                f"baton init: warning — no colleague participant for PR baton "
                f"'{project_id}'. The colleague gate (flip-to-maintainer "
                "approval check) will not be enforced — use "
                "--colleague <name> or add a colleague to --participants to "
                "enable it.",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Subcommand: flip
# ---------------------------------------------------------------------------

def cmd_flip(args: argparse.Namespace, config: dict, agent_name: str, client: ForumClient) -> None:
    _require_forum(config)

    project_id = args.project_id
    to_agent = args.to.strip().lower()
    reason = args.reason

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    # argparse accepts an empty/whitespace positional; refuse before any
    # remedy text could print a command with a missing argument.
    if not to_agent:
        print("baton flip: recipient handle is empty.", file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    raw = _api_get_raw(client, project_id)
    fields, body = _parse_frontmatter(raw)
    if not fields:
        print(
            f"baton flip: cannot parse frontmatter for '{project_id}'. "
            "The record may be malformed.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Validate: to_agent must be in participants or the pool sentinel
    participants_str = fields.get("participants", "").strip()
    participants = _parse_participants(participants_str) if participants_str else []

    if not _is_legacy_pool_sentinel(to_agent) and to_agent not in participants:
        # add-participant is server-gated to existing participants, so the
        # remedy line depends on whether THIS invoker is one.
        if (agent_name or "").strip().lower() in participants:
            who_adds = "add them first:"
        else:
            who_adds = (
                f"a participant of {project_id} must add them first "
                "(you are not one):"
            )
        print(
            f"baton flip: '{to_agent}' is not a participant in {project_id}. "
            f"Participants: {participants}.\n"
            f"If '{to_agent}' is the intended recipient (e.g. an escape-seat "
            f"review), {who_adds}\n"
            f"  baton add-participant {shlex.quote(project_id)} {shlex.quote(to_agent)}\n"
            "then re-run the flip. If it is a typo, check the handle.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Check project is not closed
    current_status = fields.get("status", "").strip().lower()
    if current_status in CLOSED_STATUSES:
        print(
            f"baton flip: project '{project_id}' is {current_status} — "
            "cannot flip a closed project.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # CI-green guard (#685): run only when flipping a PR-baton to the pool
    # sentinel (the "presenting for merge" signal per #51 protocol).
    # .strip().lower() is intentional case-normalization — the github: field is
    # user-authored YAML and may be mixed-case; matching _pool_sentinel()'s lowering.
    #
    # #1715 fix (reviewer-fairy-caught blocker): the old bare `^pr/(\d+)$`
    # regex silently failed to match a repo-qualified anchor
    # (pr/<owner>/<repo>/<N>), which meant this ENTIRE guard block --
    # CI-green, post-approval-staleness, AND the colleague-approval gate --
    # was skipped with zero warning for exactly the cross-repo PR-batons
    # this repo-qualification feature exists to support. Use the shared
    # anchor parser instead, and thread the resolved repo through to both
    # gh-querying helpers so they check the ANCHOR's repo, not gh's
    # ambient-cwd guess.
    github_anchor = fields.get("github", "").strip().lower()
    _flip_force = args.force
    _parsed_anchor = _parse_github_anchor(github_anchor)
    _pr_anchor_match = _parsed_anchor is not None and _parsed_anchor[0] == "pr"
    # #2912: hoisted here (was previously computed only later, inside the
    # no_approval branch) so both call sites below -- the unqualified-anchor
    # refusal and the main CI/approval/colleague gate -- can call
    # _is_maintainer_flip, which needs to know who currently holds the baton
    # and who the registered colleague is. Same fields.get("turn", ...) read
    # _current_holder used at its old, later site (now reusing this value
    # instead of recomputing); nothing mutates `fields` in between.
    _current_holder = fields.get("turn", agent_name or "unknown").strip().lower()
    _colleague_name = fields.get("colleague", "").strip().lower()
    # #2961 follow-up (Ariadne's round-2 catch): a stored `author:` field
    # only exists on batons created after this fix -- the live store had
    # 1,493 existing files with none, on which the exemption below could
    # never fire without either a bulk migration (a risky live write to
    # every one of them, racing whatever the running forum process is
    # doing concurrently) or this fallback: derive it from the turn log's
    # own immutable "initialized → X:" first line, which every baton
    # already carries. Zero migration, zero write, covers all 1,493
    # existing batons and every future one uniformly.
    _author_name = fields.get("author", "").strip().lower() or _derive_legacy_author(body)
    if _pr_anchor_match and _is_maintainer_flip(to_agent, _current_holder, _colleague_name, _author_name) and _parsed_anchor[1] is None:
        # #1918/#2511: unqualified pr/<N> anchor -- there is no default repo
        # to guess against. A flip-to-the-pool-sentinel IS the merge-readiness
        # signal the maintainer trusts (CLAUDE.md's baton-tracked
        # merge-readiness convention) -- skipping the CI/approval/colleague
        # gates here and merely warning left the artifact indistinguishable
        # from one whose gates actually ran and passed, which is worse than
        # refusing outright. REFUSE, matching the same repo-qualification
        # gate cmd_merge already enforces (see its "refusing to guess which
        # repo" comment) -- and matching what the header comment, the #1918
        # note, and the `baton init` warning text have promised all along.
        # This is not skippable with --force -- --force skips CI/approval
        # QUALITY gates, it has never meant "proceed without knowing which
        # repo," and this gate does not change that contract.
        print(
            f"baton flip: '{project_id}' has no repo-qualified github anchor "
            f"-- refusing to guess which repo's PR #{_parsed_anchor[2]} to "
            "verify CI/approval/colleague state against. Run "
            f"'baton anchor {project_id} --github pr/<owner>/<repo>/{_parsed_anchor[2]}' "
            "first. This is not skippable with --force.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)
    elif _pr_anchor_match and _is_maintainer_flip(to_agent, _current_holder, _colleague_name, _author_name):
        _pr_num = _parsed_anchor[2]
        _pr_repo = _parsed_anchor[1]
        _ci_state, _ci_detail = _pr_ci_state(_pr_num, repo=_pr_repo)
        if _ci_state in ("red", "pending"):
            if _flip_force:
                print(
                    f"warning: --force — flipping despite CI {_ci_state} "
                    f"on PR #{_pr_num} ({_ci_detail})",
                    file=sys.stderr,
                )
            else:
                print(
                    f"baton flip: CI is {_ci_state} on PR #{_pr_num} "
                    f"(the current tip is not merge-ready). "
                    "Re-flip when checks are green, or pass --force to override.",
                    file=sys.stderr,
                )
                sys.exit(EXIT_VALIDATION)
        elif _ci_state == "unknown":
            print(
                f"warning: could not verify CI state for PR #{_pr_num} "
                f"({_ci_detail}) — proceeding anyway",
                file=sys.stderr,
            )
        # green: proceed silently

        # Post-approval re-check guard (#1002): companion to the CI-green
        # guard above. Even with CI green on the current tip, a commit
        # pushed AFTER the latest approval means the maintainer would merge
        # a tip nobody approved — reject the flip until re-review (or
        # --force). no_approval is now handled by the colleague-gate below
        # (#1267): it rejects when a colleague participant is present, and
        # warns-only in single-agent mode (no colleague to perform the review).
        _ap_state, _ap_detail = _pr_approval_state(_pr_num, repo=_pr_repo)
        if _ap_state == "stale":
            if _flip_force:
                print(
                    f"warning: --force — flipping despite post-approval tip "
                    f"movement on PR #{_pr_num} ({_ap_detail})",
                    file=sys.stderr,
                )
            else:
                print(
                    f"baton flip: tip moved after approval on PR #{_pr_num} "
                    f"({_ap_detail}). The approved commit is not the commit "
                    "the maintainer would merge — request a re-review of the "
                    "new tip, or pass --force to override.",
                    file=sys.stderr,
                )
                sys.exit(EXIT_VALIDATION)
        elif _ap_state == "no_approval":
            # Colleague-gate (#1267): determine whether a colleague participant
            # exists.  A colleague is any participant who is neither the current
            # baton holder (the PR author/driver flipping to the sentinel) nor
            # the pool sentinel itself.  The current holder is the baton's
            # turn: field — the same value used as from_agent below.
            # GitHub blocks self-approval, so "an APPROVED review exists" ≡
            # "a non-author approved."  If no approval exists and a colleague
            # is expected, reject the flip (enforce).  If no colleague is in
            # the participants list (single-agent mode / no counterpart), warn
            # and proceed — per CLAUDE.md the colleague layer collapses when no
            # counterpart exists ("don't block on a colleague review that has
            # no one to perform it").  _current_holder was hoisted above
            # (#2912) so _is_maintainer_flip could use it too; reused here
            # rather than recomputed.
            _sentinel = _pool_sentinel()
            _colleague_participants = [
                p for p in participants
                if p != _current_holder and p != _sentinel
            ]
            if _colleague_participants:
                if _flip_force:
                    print(
                        f"warning: --force — flipping despite no colleague approval "
                        f"on PR #{_pr_num} (no APPROVED review from a non-author "
                        "colleague exists yet)",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"baton flip: no colleague approval on PR #{_pr_num}. "
                        "A non-author colleague must review and approve this PR "
                        "before it goes to the maintainer. "
                        "Pass --force to override.",
                        file=sys.stderr,
                    )
                    sys.exit(EXIT_VALIDATION)
            else:
                # Single-agent mode: no counterpart to perform the review.
                # Warn and proceed — don't block on a review with no one to do it.
                print(
                    f"warning: no colleague approval on PR #{_pr_num} "
                    "(no colleague participant — single-agent mode; skipping colleague gate)",
                    file=sys.stderr,
                )
        elif _ap_state == "unknown":
            print(
                f"warning: could not verify post-approval state for PR "
                f"#{_pr_num} ({_ap_detail}) — proceeding anyway",
                file=sys.stderr,
            )
        # fresh: proceed silently

    # #3039 U2a: compute the phase this flip should result in (or the
    # legacy-conversion phase to stamp on a still-legacy baton), on the SAME
    # pre-POST `fields` this function already has from its GET above -- see
    # the design note's phase-write architecture and Kepler's U2a ruling.
    # (Round 3 splits this into transition-vs-conversion -- see below.)
    _flip_is_pr = _is_pr_baton(project_id, fields)
    _flip_current_phase, _ = _resolve_phase(fields, is_bunch=not _flip_is_pr)
    # The author's own "send it out for colleague review" verb -- NOT a new
    # subcommand (per Kepler's ruling): the ordinary flip, when its target
    # is the registered colleague AND the baton's current phase is "fairy"
    # (explicit) or its legacy-equivalent. A PR baton with no explicit phase
    # whose turn is NOT at the legacy sentinel resolves to "" under today's
    # _resolve_phase (the common in-flight case) -- treated as "still fairy"
    # for THIS purpose only: a PR baton that hasn't reached the sentinel has
    # not left the author's own authoring rounds.
    _fairy_to_peer = (
        _flip_is_pr
        and _colleague_name
        and to_agent == _colleague_name
        and _flip_current_phase in ("fairy", "")
    )
    # #3039 U2a round 3: classify THIS flip as a transition (step 2, an
    # unconditional phase overwrite) or a conversion-only call (step 1,
    # stamp-if-absent) -- see projects.py's flip() docstring for the
    # two-step design. A TRANSITION flip (merge-seat, fairy->peer) sends
    # BOTH fields -- `phase` = the legacy-conversion value (applied first,
    # stamp-if-absent, logged as a conversion) and `transition_phase` (then
    # overwrites); a conversion-only flip sends `phase` alone (round 4,
    # Borges's round-3 colleague review: transition-only sends converted a
    # legacy baton with no conversion record in the turn log).
    _flip_transition_phase = None
    _flip_conversion_phase = None
    if _pr_anchor_match and _is_maintainer_flip(to_agent, _current_holder, _colleague_name, _author_name):
        # Reached here only because the maintainer-flip-to-merge-seat guard
        # block above either found nothing to refuse or its CI-green /
        # post-approval-freshness / colleague-approval checks already
        # passed (a failing check exits before this point) -- the literal
        # case Kepler's invariant names: "the existing flip to the merge
        # seat ... writes phase: merge until U2b replaces that edge." The
        # gating logic itself (_is_maintainer_flip, the CI/approval/
        # colleague checks above) is UNCHANGED by U2a -- only this
        # phase-write side effect is new. A TRANSITION: unconditionally
        # overwrites whatever phase the baton is currently at -- after the
        # conversion value below has been applied (round 4).
        _flip_transition_phase = "merge"
        # Round 5: the conversion value is the KNOWN pre-state, never None.
        # On a legacy baton whose turn is a real agent (the realistic
        # in-flight case) _resolve_phase yields "" -- the baton was in its
        # peer round when it is flipped to the merge seat -- so fall back
        # to "peer". A non-empty resolution (explicit phase, or a legacy
        # sentinel/merge resolution) wins over the fallback.
        _flip_conversion_phase = _flip_current_phase or "peer"
    elif _fairy_to_peer:
        # Also a TRANSITION (conversion value sent alongside, as above).
        # Round 5: pre-state is "fairy" by construction of _fairy_to_peer
        # (it treats "" as still-fairy), so a legacy "" converts from
        # "fairy" rather than None.
        _flip_transition_phase = "peer"
        _flip_conversion_phase = _flip_current_phase or "fairy"
    else:
        # Every other ordinary flip (self-flip, flip-to-author/#2961, or a
        # flip when phase is already peer/merge): legacy-conversion only --
        # send whatever _resolve_phase already derives for this baton, so a
        # still-legacy baton converts without changing WHAT phase it
        # converts to. NOT a transition: stamp-if-absent only, never
        # overwrites an existing explicit phase.
        #
        # #2961 explicitly, since a reader could otherwise mistake this for
        # an oversight (reviewer-fairy finding on #3254, round 2): a
        # colleague returning the baton to the author after finding a
        # blocker does NOT transition phase to "fairy" here, and that is
        # CORRECT, not missing. Per the design note's target model (§2):
        #   phase: peer   turn: author <-> peer   ping-pong until the peer converges
        # `turn` moving back to the author is the OTHER direction of that
        # SAME ping-pong, still within `peer` phase -- not a phase
        # regression. So this branch deliberately preserves whatever phase
        # already resolves (e.g. "peer") for the #2961 case, same as any
        # other within-phase flip.
        _flip_conversion_phase = _flip_current_phase or None

    from_agent = fields.get("turn", agent_name or "unknown").strip().lower()

    try:
        client.post(f"/api/projects/{project_id}/flip", {
            "agent": agent_name or from_agent,
            "to_agent": to_agent,
            "reason": reason,
            "phase": _flip_conversion_phase,
            "transition_phase": _flip_transition_phase,
        })
    except ForumNetworkError as e:
        print(f"baton flip: {e}", file=sys.stderr)
        sys.exit(EXIT_IO)
    except ForumHttpError as e:
        if e.status == 404:
            print(f"baton flip: project not found: {project_id}", file=sys.stderr)
            sys.exit(EXIT_STATE)
        print(f"baton flip: server error {e.status}: {e.body}", file=sys.stderr)
        sys.exit(EXIT_IO if e.status >= 500 else EXIT_VALIDATION)
    print(f"baton flip: {project_id} → {to_agent}")
    print(f"  from:   {from_agent}")
    print(f"  reason: {reason}")


# ---------------------------------------------------------------------------
# Subcommand: claim  (Project layer — pool sentinel → self)
# ---------------------------------------------------------------------------

# Pool sentinel: the name held between active agent claims.
# Derived from $ENGRAM_HOME/config.json's primary_user field (issue #606 —
# the former hardcoded primary_user default was a fresh-install bug; any
# install with a different primary_user could not use the pool sentinel correctly).
_POOL_SENTINEL_CACHE: list = []  # one-element list; empty = not yet resolved


def _pool_sentinel() -> str:
    """Return the pool sentinel for this install.

    The sentinel is the value of primary_user in $ENGRAM_HOME/config.json —
    the same field the hooks read.  Result is cached after first call so
    repeated calls inside a single CLI invocation pay no extra I/O.

    Resolution order:
      1. Cached value (after first call).
      2. config.json primary_user field.
      3. BATON_POOL_SENTINEL env var (fallback only; config.json wins when present).
      4. Exit with EXIT_VALIDATION — fails loud rather than silently
         hardcoding a name.
    """
    if _POOL_SENTINEL_CACHE:
        return _POOL_SENTINEL_CACHE[0]

    # Read from config.json (same pattern as engram-surface-hook.py).
    engram_home = (
        os.environ.get("ENGRAM_HOME")
        or str(Path.home() / ".engram")
    )
    try:
        config_path = Path(engram_home) / "config.json"
        pu = json.loads(config_path.read_text()).get("primary_user")
        if pu:
            _POOL_SENTINEL_CACHE.append(str(pu).lower())
            return _POOL_SENTINEL_CACHE[0]
    except (OSError, ValueError, AttributeError):
        pass

    # Fallback: BATON_POOL_SENTINEL env var (config.json primary_user wins
    # when present; this path is only reached when config.json is absent or
    # lacks primary_user — useful for test fixtures and non-standard installs).
    env_val = os.environ.get("BATON_POOL_SENTINEL")
    if env_val:
        _POOL_SENTINEL_CACHE.append(env_val.lower())
        return _POOL_SENTINEL_CACHE[0]

    print(
        "baton: cannot determine pool sentinel — set primary_user in "
        "$ENGRAM_HOME/config.json (or set BATON_POOL_SENTINEL).",
        file=sys.stderr,
    )
    sys.exit(EXIT_VALIDATION)


def _is_legacy_pool_sentinel(name: str) -> bool:
    """Legacy fallback only -- see _resolve_phase. Exactly today's (pre-#3039)
    _is_pool_sentinel(): compares against THIS SEAT's own config.json
    primary_user, seat-DEPENDENT by construction -- that dependency is
    exactly the #3039 defect when used as a primary decision mechanism,
    which is why _resolve_phase() only reaches this as a fallback once no
    explicit `phase:` field is present.

    Accepts both the full sentinel ("lei shi") and its first token ("lei") so
    pools initialized with a short-form turn value work correctly (issue #1309).

    Renamed from _is_pool_sentinel() by #3039 U1. A handful of pre-existing
    call sites (cmd_init's initial-turn validation, cmd_flip's to_agent
    validation, _is_maintainer_flip's sentinel check below) still call this
    directly rather than through _resolve_phase() -- migrating those to a
    phase-aware check is out of U1's scope (per Kepler's unit cut on #3039:
    U2 is "one verb per phase advance", which is where cmd_flip and
    _is_maintainer_flip's phase-edge rework belongs); they are mechanical
    renames only, identical behavior to before this unit.
    """
    sentinel = _pool_sentinel()  # e.g. "lei shi"
    if name == sentinel:
        return True
    # Also accept the first whitespace-token of the sentinel ("lei" from "lei shi").
    first_token = sentinel.split()[0] if sentinel else ""
    return bool(first_token) and name == first_token


def _resolve_phase(fields: dict, *, is_bunch: bool) -> tuple[str, bool]:
    """Resolve a baton's current phase from its frontmatter fields.

    Returns (phase, used_legacy_fallback).

    Two-tier resolution (engram-alpha#3039, U1 -- read side only; nothing
    writes an explicit `phase:` field yet, that is U2's job, so for U1
    EVERY existing baton is "legacy" by definition):

      1. An explicit `phase:` field, if present -- read verbatim. This is
         seat-INDEPENDENT: no comparison against this install's own
         config.json primary_user, so the answer cannot move when
         primary_user changes. This is the property #3039 is about.
      2. No explicit `phase:` field -- LEGACY FALLBACK: derive from the old
         turn == pool-sentinel comparison (_is_legacy_pool_sentinel, exactly
         today's pre-#3039 _is_pool_sentinel(), seat-DEPENDENT), and log
         that the legacy path fired so the transition window (design note
         docs/design/3039-baton-phase-turn.md §7) can be watched and
         eventually retired. A bunch baton at the sentinel resolves to
         "pool" (free to claim); a PR baton at the sentinel resolves to
         "merge" (presented for merge) -- same sentinel value, two
         different meanings, per the design note §1's "the sentinel is
         doing two jobs."
    """
    explicit = fields.get("phase", "").strip().lower()
    if explicit:
        return explicit, False
    turn = fields.get("turn", "").strip().lower()
    if _is_legacy_pool_sentinel(turn):
        phase = "pool" if is_bunch else "merge"
        print(
            f"baton: legacy phase fallback -- no explicit 'phase:' field; "
            f"turn={turn!r} matches this seat's legacy pool sentinel, so "
            f"phase resolves to {phase!r} via the pre-#3039 turn-vs-sentinel "
            "comparison -- until #3039 U2 lands, no transition writes an "
            "explicit phase.",
            file=sys.stderr,
        )
        return phase, True
    return "", False


def _is_maintainer_flip(
    to_agent: str,
    current_holder: str,
    colleague_name: str,
    author: str = "",
) -> bool:
    """True when flipping the baton to `to_agent` represents presenting
    the PR for MERGE (to the maintainer) — the point where CI-green,
    approval-freshness, and colleague-approval-existence must all hold —
    as opposed to a review-stage flip (to the registered colleague, who
    has not reviewed yet), a self-flip (still working on it, no
    merge-readiness claim being made), or a colleague sending it BACK TO
    THE AUTHOR after finding a blocker (#2961 — see below).

    Fires on:
      - the literal pool sentinel (the single-human-owner convention
        `_is_legacy_pool_sentinel` already recognizes), OR
      - any OTHER agent — this repo's actual convention is to flip to a
        NAMED maintainer agent (e.g. "ariadne", "borges"), not the literal
        sentinel — PROVIDED it is not a self-flip, not the flip TO the
        registered colleague (that flip is the "please review" signal,
        not "ready to merge", and firing CI/approval checks on it would
        be premature — the colleague hasn't reviewed yet), and not a flip
        TO THE AUTHOR (#2961: a colleague returning the baton to the PR's
        own author after finding a blocker matches neither of the two
        exemptions above — the target is not the current holder (the
        colleague holds it) and not the colleague (the colleague IS the
        actor) — so without this third exemption it was misclassified as
        presenting-for-merge and demanded a colleague approval that cannot
        exist, since the colleague is the one objecting, not approving).

    `author` is optional and empty on a baton created before #2961 (no
    `author:` frontmatter field) — an empty/falsy `author` simply means
    this exemption cannot fire, identical to pre-#2961 behavior. It is
    populated once at `baton init` time from the INVOKING agent (#3042 --
    before that it was taken from the initial turn holder, which mis-attributed
    authorship whenever --turn named someone other than the initor) and never
    touched again by flip/claim/release, unlike `turn_by`, which IS overwritten
    on every turn-change and so cannot serve as an author proxy past the first
    flip.

    Caller guarantees `to_agent` is already either the pool sentinel or a
    genuine registered participant (validated earlier in cmd_flip) — this
    function does not re-validate participant membership.
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


def _is_pr_shaped(project_id: str, github: str) -> bool:
    """Anchor-first, name-only-when-unanchored PR-vs-bunch classification
    body, shared by `_is_pr_baton` (an EXISTING baton, reading `github` off
    its `fields` dict) and `cmd_init` (no baton exists yet to read `fields`
    from -- just the `project_id` under construction and its already-resolved
    `--github` value) (#3039 U2a). Factored out so both call sites share one
    definition of the anchor/name rule rather than a third hand-copy of it
    -- see `_is_pr_baton`'s own docstring for the full rule, its rationale,
    and the live-store measurement pinning it.

    `github` is the raw (already lowercased/stripped, or empty) anchor
    string -- callers pass `fields.get("github", "")` or an equivalent.
    """
    github = (github or "").strip().lower()
    parsed = _parse_github_anchor(github) if github else None
    if parsed is not None:
        return parsed[0] == "pr"
    if project_id.strip().lower().startswith("pool-"):
        return False
    return bool(_PR_BATON_NAME_RE.match(project_id.upper()))


def _is_pr_baton(project_id: str, fields: dict) -> bool:
    """True if this baton is a PR baton -- by github anchor, or (only when
    unanchored) by id shape -- for _resolve_phase's is_bunch derivation
    (engram-alpha#3039).

    Anchor-first, name-only-when-unanchored (Kepler's #3243 merge-seat
    conformance check, 2026-09-30, defect 1):
      1. An anchor decides when there is one: parse fields["github"] via
         _parse_github_anchor (shared with cmd_init/cmd_flip/etc. --
         see its own docstring for the (kind, repo, number) return shape).
         A "pr/..." anchor means this IS a PR baton; a "project/..." anchor
         means it is NOT (a bunch), regardless of what the id looks like.
      2. The name is consulted ONLY for an UNANCHORED baton (no github
         field, or one that doesn't parse as a known anchor shape). For an
         unanchored baton, a "pool-"-prefixed id is always a bunch -- the
         bunch-naming convention this project's CLAUDE.md documents
         ("Project-as-bunch coordination": bunch project IDs are named
         `pool-<topic>`) -- whatever else its name contains. Otherwise fall
         back to _PR_BATON_NAME_RE -- the same prefix-only id-shape gate
         cmd_gc already uses -- case-insensitively via .upper(): bare
         ("PR-3243"), prefixed ("UMWELT-PR-775", "PAPER-PR-121"), or
         PR-N-suffixed ("PR-92-paper") -- anything starting with an
         optional type-prefix followed by "PR-".

    The PRIOR rule (name-shape-first via _PR_BATON_NAME_RE.match(id.upper()),
    falling back to the "pr/" anchor prefix only as a second signal)
    misclassified two real bunch batons as PR batons on the live shared
    store: _PR_BATON_NAME_RE allows one upper-case type-prefix segment
    before "PR-", and after .upper() "POOL-" is such a prefix, so
    "POOL-PR-DISCIPLINE" and "POOL-PR-FOLLOWUPS" both matched. Kepler's
    live-store re-measurement of the SHIPPED (pre-this-fix) function
    (2026-09-30 merge-seat conformance check on PR #3243): of 12
    project/-anchored batons, 1 misclassified as PR
    ("pool-pr-discipline", anchor "project/11"); of 4 unanchored batons
    that were PR-shaped only because of .upper(), 1 misclassified bunch
    ("pool-pr-followups", no anchor) alongside 3 genuinely-PR batons
    correctly classified ("umwelt-PR-10", "umwelt-PR-2", "umwelt-PR-33").
    Borges has now re-run the SHIPPED function itself against the live
    shared store -- not a pattern typed beside it, and not taken on trust
    this time, unlike the prior round's docstring, which attributed his
    count without independently re-running it (2026-09-30 re-review of
    this PR at `42dbfdb4`; the store grew by one file since that prior
    count -- 1,774, not 1,773):

    |                                 | count | `_is_pr_baton` result |
    |---------------------------------|-------|------------------------|
    | `pr/`-anchored                  | 1,225 | all `True` |
    | `project/`-anchored             | 12    | all `False` (includes "pool-pr-discipline") |
    | anchor present but unparseable  | 0     | -- |
    | unanchored                      | 537   | 482 `True`, 55 `False`; all 55 are "pool-*", "ISSUE-*", "SLICE-*", "DESIGN-*" and similar bunch/issue ids, and "pool-pr-followups" is among them |

    Total 1,774. 0 anchored batons misclassified.
    """
    return _is_pr_shaped(project_id, fields.get("github", ""))


def _pool_sentinel_resolvable() -> bool:
    """True iff `_pool_sentinel()` would resolve WITHOUT exiting -- an
    already-cached value, `config.json`'s `primary_user`, or the
    `BATON_POOL_SENTINEL` env var. Read-only: never populates
    `_POOL_SENTINEL_CACHE` itself (call `_pool_sentinel()` for that, once
    this has confirmed it is safe to).

    Exists for `_conversion_phase` below (#3039 U2a round 3) -- see its
    docstring for why a plain, unconditional `_pool_sentinel()` call is
    wrong there.
    """
    if _POOL_SENTINEL_CACHE:
        return True
    engram_home = (
        os.environ.get("ENGRAM_HOME")
        or str(Path.home() / ".engram")
    )
    try:
        config_path = Path(engram_home) / "config.json"
        if json.loads(config_path.read_text()).get("primary_user"):
            return True
    except (OSError, ValueError, AttributeError):
        pass
    return bool(os.environ.get("BATON_POOL_SENTINEL"))


def _conversion_phase(project_id: str, fields: dict) -> Optional[str]:
    """Compute the legacy-conversion phase for a conversion-only write
    (#3039 U2a round 3) -- the SAME pattern cmd_claim/cmd_flip already use
    (`_is_pr_baton` + `_resolve_phase`), factored out so the eight
    newly-wired commands (close/reopen via the shared status endpoint, gc,
    rename, anchor, add-participant, set-colleague, merge) don't each
    hand-copy it a ninth time.

    Degrades to ``None`` (skip conversion on THIS call) rather than
    propagating a `_pool_sentinel()` exit, when the pool sentinel is not
    configured. This is deliberate, not an oversight: NONE of these eight
    commands previously depended on the pool sentinel being resolvable AT
    ALL -- `baton rename`,
    `baton anchor`, `baton merge`, etc. have never required `primary_user`
    to be configured, and conversion is a best-effort side computation for
    them (their OWN write-bodies write no transition phase, per Kepler's
    ruling -- conversion is the only phase-related work these commands do).
    Crashing the caller's actual operation -- a rename, an anchor set, a
    squash-merge -- over an unresolvable pool sentinel needed only for a
    LEGACY-CONVERSION side effect would be strictly worse than the
    pre-#3039 behavior these commands already had. The write-body's own
    no-clobber guard (`_maybe_stamp_phase`) already tolerates a missing/
    ``None`` conversion input by design -- skipping here just means this
    particular baton doesn't convert on THIS call; the next write against
    it (from any of the twelve write-bodies) tries again.

    Returns ``None`` (never an empty string) when `_resolve_phase` resolves
    nothing -- matches the existing "never send an empty string" convention
    already used at cmd_flip's own call site.
    """
    explicit = (fields.get("phase") or "").strip().lower()
    if explicit:
        return explicit
    if not _pool_sentinel_resolvable():
        return None
    _is_pr = _is_pr_baton(project_id, fields)
    _phase, _ = _resolve_phase(fields, is_bunch=not _is_pr)
    return _phase or None


def cmd_claim(args: argparse.Namespace, config: dict, agent_name: str, client: ForumClient) -> None:
    """Claim a Project-layer baton from the pool.

    Equivalent to 'baton flip <project-id> <self> "claimed"' but makes the
    Project-layer grab/ungrab vocabulary first-class. Only succeeds when
    turn == pool sentinel (the install's primary_user); refuses when the turn
    is not this seat's recognized pool marker -- which may be a colleague
    genuinely holding it, or a pool/maintainer marker written by a seat with a
    different primary_user (engram-alpha#3039).
    """
    _require_forum(config)

    project_id = args.project_id

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    raw = _api_get_raw(client, project_id)
    fields, body = _parse_frontmatter(raw)
    if not fields:
        print(
            f"baton claim: cannot parse frontmatter for '{project_id}'. "
            "The record may be malformed.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Validate: current phase must be "pool" (#3039 U1 -- was a bare
    # turn == pool-sentinel comparison; now routes through _resolve_phase(),
    # which reads an explicit `phase:` field first and falls back to the
    # legacy sentinel comparison only when none is present -- see
    # _resolve_phase's own docstring). is_bunch is derived via _is_pr_baton
    # (id shape OR github anchor -- see its docstring; #3039 U1 round-2,
    # Borges colleague review PR #3243 blocker 1: the narrow bare-PR-N-only
    # regex this replaced missed prefixed and oddly-named PR batons) to
    # distinguish a PR baton from a bunch/Project-layer baton. cmd_claim is
    # a Project-layer (bunch) verb by its own contract (see this function's
    # docstring), so a PR baton resolves to phase "merge" (not "pool") here
    # and is correctly refused below, same as before this unit for any
    # baton that was never claimable via this verb.
    current_turn = fields.get("turn", "").strip().lower()
    _is_pr = _is_pr_baton(project_id, fields)
    _phase, _ = _resolve_phase(fields, is_bunch=not _is_pr)
    if _phase and _phase != "pool":
        # A REAL resolved phase other than "pool" (the legacy fallback
        # fired and turn DID match the sentinel -- see _resolve_phase's own
        # docstring) is not indeterminate: there is nothing to hedge, so
        # this refusal is short and names the phase directly. The long
        # #3040 indeterminacy text below is reserved for the genuinely-
        # indeterminate case (_resolve_phase returned an empty phase) --
        # Kepler's #3243 merge-seat conformance check, 2026-09-30, defect 2.
        print(
            f"baton claim: '{project_id}' is at phase '{_phase}', not 'pool' -- "
            f"claim only takes a baton whose phase is 'pool'.",
            file=sys.stderr,
        )
        sys.exit(EXIT_STATE)
    if not _phase:
        # WHY THIS REFUSAL REPORTS INDETERMINACY RATHER THAN A VERDICT.
        # Rounds 2-4 of #3040 ended with "the baton may in fact be free"
        # whenever the scoped turn-log line was not a `claimed` one. That is
        # false in the common case, and false in the dangerous direction. Of
        # the two writers in src/forum/coordination/projects.py that can
        # produce a non-`claimed` line scoped to the turn value:
        #   release()  f"- {now} {self_name} → {sentinel}: {reason}"
        #   flip()     f"- {now} {from_agent} → {to}: {reason}"
        # the emitted text is IDENTICAL -- `- <ts> <name> → <name>: <text>`
        # in both, with no marker of which writer produced it. release's RHS
        # happens to be the RELEASING seat's own primary_user and flip's RHS
        # is any participant, but primary_user is per-install and this seat
        # cannot enumerate another install's, so the RHS is not classifiable
        # from here -- which IS #3039. Measured on batons built through the
        # real writers: init(turn=borges) + flip(→ariadne) yields
        # `borges → ariadne: please review` (ariadne HOLDS it) and
        # init(turn=clio) + release(→"lei shi") yields
        # `clio → lei shi: released` (FREE); both match "a line handing it
        # BACK", so round 4 reported the flip-held baton as possibly free --
        # the round-2 held-baton-reported-free inversion, one layer along.
        # "Handed BACK" was circular besides: judging it requires already
        # knowing whether the RHS is a pool marker, the question the reader
        # came with. turn_reason does not rescue it either -- release's
        # --reason (see p_release in build_parser) defaults to "released"
        # but is user-settable, so it hints and never settles, and flip's
        # reason is free text. The honest output is that this seat cannot
        # determine it, plus an action that resolves it OFF the log (ask the
        # named agent; check the anchored PR/project).
        #
        # WHY THE TURN-LOG READING RULE IS NOT PRINTED HERE (round 7). The
        # rule is real and still ships; it lives in the engram-baton skill,
        # which this message points at. Inlined here it was two bullets, and
        # those bullets were the site of the held-looks-free inversion in
        # rounds 2 and 4; repairing them grew the message from 709 chars to
        # 1049, past the 955 already flagged as too long for an error
        # stream. A reading rule is prose a reader consults deliberately,
        # not a wall that arrives mid-refusal. The pinned-message test
        # carries a hard length ceiling so it cannot grow back unnoticed.
        #
        # THE SCOPING AUDIT BEHIND THAT RULE, kept here because it is the
        # evidence for it and it is easy to get wrong. The rule scopes to
        # lines shaped `<A> → <B>: <reason>` whose <B> equals `turn`, NOT to
        # "the last arrow line": three turn-NEUTRAL writers also emit
        # arrows, so "the last arrow line" can point a reader at a named
        # agent who does not hold the baton -- #3039's own false-holder
        # defect one hop downstream. Checked against every arrow-emitting
        # writer in projects.py:
        #   flip()    `<from> → <to>: <reason>`         RHS == turn (to)
        #   claim()   `<sentinel> → <claimer>: claimed` RHS == turn
        #   release() `<holder> → <sentinel>: <reason>` RHS == turn
        #   init()    `initialized → <turn>: <reason>`  RHS == turn
        #   reopen()  `reopened → <invoker>: status was <X> → <Y>`
        #             -- updates ONLY status, never turn, so RHS != turn
        #             unless the invoker IS the turn holder, in which case
        #             the line truthfully names the holder and no false
        #             diagnosis results. (Its SECOND arrow is why the skill
        #             says "just after the FIRST arrow".)
        #   anchor()  `anchor set: github → <anchor>`   no `: <reason>`
        #             after the arrow, so the shape excludes it
        #   set_colleague() `<agent> set colleague → <colleague>`
        #             same -- no `: <reason>` after the arrow
        # The remaining append_turn_log sites (set_status(), close(),
        # merge(), rename(), add_participant()) emit no arrow. init() is the
        # only scoped writer that can emit a line for a baton parked in a
        # pool and NEVER claimed -- #3039's own motivating case -- so the
        # skill enumerates it alongside claim, flip and release.
        #
        # `baton flip` is deliberately NOT offered here. It is the right
        # remedy only for the colleague case, and this message no longer
        # claims to identify that case.
        print(
            f"baton claim: this baton's turn is '{current_turn}', which this "
            f"seat does not recognize as its pool marker (this seat resolves "
            f"'{_pool_sentinel()}'). This seat cannot tell from the turn log "
            f"whether '{current_turn}' is a colleague who holds this baton or "
            f"a pool marker belonging to an install whose primary_user this "
            f"seat cannot resolve -- that is engram-alpha#3039. Settle it off "
            f"the log: ask '{current_turn}', or check the PR or project this "
            f"baton anchors. To read the log yourself, run\n"
            f"    baton show {project_id}\n"
            "and see the engram-baton skill for how to scope a turn-log line "
            "to the turn value.",
            file=sys.stderr,
        )
        sys.exit(EXIT_STATE)

    # Validate: agent must be in participants
    if not agent_name:
        print(
            "baton claim: cannot determine agent name. "
            "Set agent_name in config.json or ensure $USER is set.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    participants_str = fields.get("participants", "").strip()
    participants = _parse_participants(participants_str) if participants_str else []
    if agent_name.lower() not in participants:
        print(
            f"baton claim: '{agent_name}' is not a participant in {project_id}. "
            f"Participants: {participants}.\n"
            f"add-participant is participant-only, so ask a participant of "
            f"{project_id} to run:\n"
            f"  baton add-participant {shlex.quote(project_id)} {shlex.quote(agent_name.lower())}",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Check project is not closed
    current_status = fields.get("status", "").strip().lower()
    if current_status in CLOSED_STATUSES:
        print(
            f"baton claim: project '{project_id}' is {current_status} — "
            "cannot claim a closed project.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    self_name = agent_name.lower()
    try:
        client.post(f"/api/projects/{project_id}/claim", {
            "agent": self_name,
            "pool_sentinel": _pool_sentinel(),
            # #3039 U2a: send the RESULT phase of this claim, not the
            # pre-claim gate-check value (_phase, guaranteed "pool" here by
            # the two guard clauses above). Per Kepler's ruling ("the bunch
            # machine's writes: claim writes claimed, release writes pool,
            # release --done writes done") the bunch machine is
            # pool -> claimed -> done, and "claimed" is the state a bunch
            # baton is actually IN once this call succeeds.
            #
            # Round 3 (Borges's colleague review on #3254, the live
            # double-claim race, Kepler's round-3 two-step ruling): sent as
            # `transition_phase`, NOT `phase` (conversion) -- round 2 sent
            # this exact value through the conversion (stamp-if-absent)
            # channel, which was the bug itself. `init()` already stamps an
            # explicit `phase: pool` at baton creation; stamp-if-absent
            # never overwrites an existing explicit phase, so round 2's
            # claim never actually advanced the stored phase to "claimed" --
            # it stayed frozen at "pool" forever, and a frozen `phase: pool`
            # reads as an explicit (not legacy-fallback) value, so a SECOND
            # `claim` on an already-claimed baton was never refused. Routing
            # this value through `transition_phase` (an unconditional
            # overwrite, per projects.py's two-step design) is what actually
            # closes the race: the stored phase now genuinely advances to
            # "claimed", so a second claim's own phase check correctly
            # refuses it.
            #
            # Round 4 (Borges, round-3 colleague review): a transition sends
            # BOTH fields -- `phase` (the legacy-conversion value, computed
            # from the same pre-POST `fields`) AND `transition_phase`. The
            # write-body applies conversion first (stamp-if-absent, logged
            # "phase stamped ... (legacy conversion)"), then the transition
            # overwrites (logged "(transition)"). Sending only the
            # transition (round 3) converted a legacy baton with NO
            # conversion record in the turn log. On a baton that already has
            # an explicit `phase:` the conversion is a server-side no-op.
            # `_pool_sentinel()` above already hard-exits if unresolvable.
            "phase": _conversion_phase(project_id, fields),
            "transition_phase": "claimed",
        })
    except ForumNetworkError as e:
        print(f"baton claim: {e}", file=sys.stderr)
        sys.exit(EXIT_IO)
    except ForumHttpError as e:
        if e.status == 404:
            print(f"baton claim: project not found: {project_id}", file=sys.stderr)
            sys.exit(EXIT_STATE)
        print(f"baton claim: server error {e.status}: {e.body}", file=sys.stderr)
        sys.exit(EXIT_IO if e.status >= 500 else EXIT_VALIDATION)
    print(f"baton claim: {project_id} → {self_name}")


# ---------------------------------------------------------------------------
# Subcommand: release  (Project layer — self → pool sentinel)
# ---------------------------------------------------------------------------

def cmd_release(args: argparse.Namespace, config: dict, agent_name: str, client: ForumClient) -> None:
    """Release a Project-layer baton back to the pool.

    Equivalent to 'baton flip <project-id> <pool-sentinel> <reason>' but
    restricted to the current holder (only you can release what you hold).
    With --done, also appends '(done)' to the project title (idempotent).
    """
    _require_forum(config)

    project_id = args.project_id
    mark_done = args.done
    reason = args.reason

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    raw = _api_get_raw(client, project_id)
    fields, body = _parse_frontmatter(raw)
    if not fields:
        print(
            f"baton release: cannot parse frontmatter for '{project_id}'. "
            "The record may be malformed.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Validate: current turn must be this agent (only the holder can release)
    if not agent_name:
        print(
            "baton release: cannot determine agent name. "
            "Set agent_name in config.json or ensure $USER is set.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    current_turn = fields.get("turn", "").strip().lower()
    self_name = agent_name.lower()
    if current_turn != self_name:
        print(
            f"baton release: only the current holder ({current_turn}) can release.",
            file=sys.stderr,
        )
        sys.exit(EXIT_STATE)

    # Check project is not closed
    current_status = fields.get("status", "").strip().lower()
    if current_status in CLOSED_STATUSES:
        print(
            f"baton release: project '{project_id}' is {current_status} — "
            "cannot release a closed project.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    sentinel = _pool_sentinel()
    # #3039 U2a: release is a bunch-machine verb with its own intrinsic
    # result phase (pool -> claimed -> done) -- "done" when --done was
    # passed, else "pool" (an ordinary release just frees the baton back to
    # the pool, matching Kepler's ruling: "release writes pool, release
    # --done writes done"). Round 3: ALWAYS a transition -- sent as
    # `transition_phase` (unconditional overwrite) for the same reason
    # claim() is -- see cmd_claim's own comment for the full double-claim-
    # race trace this fixes. Round 4: ALSO sends `phase` (the legacy-
    # conversion value from `_conversion_phase`) so a still-legacy baton is
    # converted, and that conversion recorded in the turn log, BEFORE the
    # transition overwrites it -- see cmd_claim's round-4 comment.
    _release_phase = "done" if mark_done else "pool"
    # Round 5: the conversion value is the KNOWN pre-state, never None --
    # release is only reachable from a held ("claimed") baton, so a legacy
    # baton whose turn is a real agent (`_conversion_phase` -> None) converts
    # from "claimed". A non-empty `_conversion_phase` result wins.
    _release_conversion_phase = _conversion_phase(project_id, fields) or "claimed"
    try:
        client.post(f"/api/projects/{project_id}/release", {
            "agent": self_name,
            "pool_sentinel": sentinel,
            "reason": reason,
            "done": mark_done,
            "phase": _release_conversion_phase,
            "transition_phase": _release_phase,
        })
    except ForumNetworkError as e:
        print(f"baton release: {e}", file=sys.stderr)
        sys.exit(EXIT_IO)
    except ForumHttpError as e:
        if e.status == 404:
            print(f"baton release: project not found: {project_id}", file=sys.stderr)
            sys.exit(EXIT_STATE)
        print(f"baton release: server error {e.status}: {e.body}", file=sys.stderr)
        sys.exit(EXIT_IO if e.status >= 500 else EXIT_VALIDATION)
    print(f"baton release: {project_id} → {sentinel}")
    if mark_done:
        print("  marked (done)")


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace, config: dict, agent_name: str, client: ForumClient) -> None:
    if not _is_forum_configured(config):
        # Single-agent or not deployed — silent exit per spec
        sys.exit(EXIT_OK)

    resp = _api_get(client, "/api/projects", params={"active_only": "true"})
    projects = resp.get("projects", [])

    # Parse turn_since strings to datetime for _age_str / sorting
    for p in projects:
        p["turn_since"] = _parse_iso(p.get("turn_since") or "")

    projects.sort(key=lambda p: p["turn_since"] or datetime.min.replace(tzinfo=timezone.utc))

    mine_only = getattr(args, "mine", False)
    if mine_only and agent_name:
        # Filter client-side by turn (server agent= param filters by PARTICIPANT)
        projects = [p for p in projects if p["turn"] == agent_name.lower()]

    if not projects:
        if mine_only:
            print("baton mine: no batons in your court.")
        else:
            print("baton status: no active projects.")
        return

    label = "MINE" if mine_only else "ACTIVE"
    print(f"{label} ({len(projects)}):")
    for p in projects:
        ts_age = _age_str(p["turn_since"])
        age_part = f" ({ts_age})" if ts_age else ""
        turn_info = f"turn: {p['turn']}{age_part}"
        turn_reason = p.get("turn_reason", "")
        # Defensively strip surrounding quotes from turn_reason in case the
        # server echoes raw YAML values (correct servers return clean strings).
        if turn_reason.startswith('"') and turn_reason.endswith('"'):
            turn_reason = turn_reason[1:-1]
        reason_part = f" — {turn_reason}" if turn_reason else ""
        print(f"  {p['project_id']:<28} {turn_info}{reason_part}")
        title = p.get("title", "")
        if title and title != p["project_id"]:
            print(f"    {title}")


# ---------------------------------------------------------------------------
# Subcommand: mine
# ---------------------------------------------------------------------------

def cmd_mine(args: argparse.Namespace, config: dict, agent_name: str, client: ForumClient) -> None:
    """Shorthand for status --mine."""
    # Inject mine=True and delegate to cmd_status
    args.mine = True
    cmd_status(args, config, agent_name, client)


# ---------------------------------------------------------------------------
# Subcommand: show
# ---------------------------------------------------------------------------

def cmd_show(args: argparse.Namespace, config: dict, agent_name: str, client: ForumClient) -> None:
    if not _is_forum_configured(config):
        # Single-agent or not deployed — silent exit per spec
        sys.exit(EXIT_OK)

    project_id = args.project_id

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    raw = _api_get_raw(client, project_id)
    print(raw, end="" if raw.endswith("\n") else "\n")


# ---------------------------------------------------------------------------
# Subcommand: close
# ---------------------------------------------------------------------------

def cmd_close(args: argparse.Namespace, config: dict, agent_name: str, client: ForumClient) -> None:
    _require_forum(config)

    project_id = args.project_id
    new_status = args.status.strip().lower()

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    if new_status not in CLOSED_STATUSES:
        print(
            f"baton close: --status must be one of: {', '.join(sorted(CLOSED_STATUSES))}. "
            f"Got: '{new_status}'.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    raw = _api_get_raw(client, project_id)
    fields, _body = _parse_frontmatter(raw)
    if not fields:
        print(
            f"baton close: cannot parse frontmatter for '{project_id}'.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    current_status = fields.get("status", "").strip().lower()
    if current_status in CLOSED_STATUSES:
        print(
            f"baton close: project '{project_id}' is already {current_status}.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    close_reason = f"closed by {agent_name or 'agent'}"
    # #3039 U2a round 3: conversion-only (close writes no transition phase,
    # per Kepler's ruling) -- reuses the `fields` already GET+parsed above.
    _close_phase = _conversion_phase(project_id, fields)
    try:
        client.post(f"/api/projects/{project_id}/status", {
            "agent": agent_name or "baton",
            "new_status": new_status,
            "reason": close_reason,
            "phase": _close_phase,
        })
    except ForumNetworkError as e:
        print(f"baton close: {e}", file=sys.stderr)
        sys.exit(EXIT_IO)
    except ForumHttpError as e:
        if e.status == 404:
            print(f"baton close: project not found: {project_id}", file=sys.stderr)
            sys.exit(EXIT_STATE)
        print(f"baton close: server error {e.status}: {e.body}", file=sys.stderr)
        sys.exit(EXIT_IO if e.status >= 500 else EXIT_VALIDATION)
    print(f"baton close: {project_id} → {new_status}")


# ---------------------------------------------------------------------------
# Subcommand: reopen
# ---------------------------------------------------------------------------

# Active statuses that reopen can target (complement of CLOSED_STATUSES).
ACTIVE_STATUSES = {"planning", "in-progress", "in-review"}


def cmd_reopen(args: argparse.Namespace, config: dict, agent_name: str, client: ForumClient) -> None:
    """Flip a closed baton back to an active status.

    Inverse of cmd_close. Only operates on batons with status: merged or
    cancelled. Refuses if the baton is already in a non-closed status.
    """
    _require_forum(config)

    project_id = args.project_id
    new_status = args.status.strip().lower()

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    if new_status not in ACTIVE_STATUSES:
        print(
            f"baton reopen: --status must be one of: {', '.join(sorted(ACTIVE_STATUSES))}. "
            f"Got: '{new_status}'. Use 'baton close' for merged/cancelled transitions.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    raw = _api_get_raw(client, project_id)
    fields, _body = _parse_frontmatter(raw)
    if not fields:
        print(
            f"baton reopen: cannot parse frontmatter for '{project_id}'.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    current_status = fields.get("status", "").strip().lower()
    if current_status not in CLOSED_STATUSES:
        print(
            f"baton reopen: project {project_id} is not closed (status: {current_status}); "
            "use 'baton flip' to change turn or 'baton close' to archive.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    invoker = agent_name or "unknown"
    reopen_reason = f"reopened by {invoker}: status was {current_status} → {new_status}"
    # #3039 U2a round 3: conversion-only (reopen writes no transition phase,
    # per Kepler's ruling) -- reuses the `fields` already GET+parsed above.
    _reopen_phase = _conversion_phase(project_id, fields)
    try:
        client.post(f"/api/projects/{project_id}/status", {
            "agent": invoker,
            "new_status": new_status,
            "reason": reopen_reason,
            "phase": _reopen_phase,
        })
    except ForumNetworkError as e:
        print(f"baton reopen: {e}", file=sys.stderr)
        sys.exit(EXIT_IO)
    except ForumHttpError as e:
        if e.status == 404:
            print(f"baton reopen: project not found: {project_id}", file=sys.stderr)
            sys.exit(EXIT_STATE)
        print(f"baton reopen: server error {e.status}: {e.body}", file=sys.stderr)
        sys.exit(EXIT_IO if e.status >= 500 else EXIT_VALIDATION)
    print(f"baton reopen: {project_id} → {new_status}")


# ---------------------------------------------------------------------------
# Subcommand: gc
# ---------------------------------------------------------------------------

def cmd_gc(args: argparse.Namespace, config: dict, agent_name: str, client: ForumClient) -> None:
    """Batch-close PR-batons whose GitHub PR is MERGED or CLOSED.

    Scans all open PR-batons and queries the live GitHub state for each.
    PRs in MERGED state are closed with status ``merged``; PRs in CLOSED
    state (without merge) are closed with status ``cancelled``.

    Use ``--dry-run`` to preview changes without applying them.
    Use ``--limit N`` to cap the number of PR-batons processed per run
    (default: 30) and bound wall-clock runtime.

    PR-baton detection is ANCHOR-FIRST (#2010 rider 1, Kepler DM
    2026-09-21): a project whose ``github:`` field resolves to a
    repo-qualified PR anchor is a PR-baton unconditionally, whatever its
    ``project_id`` looks like.  The ``_PR_BATON_NAME_RE`` name-shape gate is
    consulted only as a FALLBACK, to classify batons with no qualified
    anchor (near-miss vs. genuinely unrelated).  Do not read the older
    "match by suffix, not prefix" framing as still-primary; that governs
    only the fallback path now.

    This is the explicit full-sweep complement to the prompt hook's
    cache-only auto-archive pass.  Agents should run ``baton gc`` at
    loop-start to drain the backlog of merged/closed PR-batons that the
    hook cannot reach (uncached anchors).
    """
    _require_forum(config)

    if not shutil.which("gh"):
        print("baton gc: gh CLI not found — cannot query live PR state", file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    dry_run = args.dry_run
    limit = args.limit

    # Fetch active projects from API (active_only=true filters closed statuses)
    resp = _api_get(client, "/api/projects", params={"active_only": "true"})
    all_projects = resp.get("projects", [])

    # Filter to PR-batons, capped at limit. #1715: the PR number (and repo)
    # come from the stored `github` anchor, NOT re-derived from project_id
    # via regex -- that re-derivation had NO repo awareness at all and
    # queried gh with a bare number, silently matching whatever repo gh's
    # ambient cwd happened to resolve to.
    #
    # #1918: a PR-baton with no anchor at all, an unparseable one, OR an
    # UNQUALIFIED pr/<N> anchor (no owner/repo) is skipped rather than
    # guessed at -- there is no default repo to fall back to (see the
    # DEFAULT_GITHUB_REPO removal note near the top of this file). Ported
    # from umwelt's gh_reconcile.py: "skip and count, never resolve against
    # a default or ambient repo."
    #
    # #2010 rider 1 (Kepler DM, 2026-09-21): ANCHOR FIRST, name-regex only
    # as the no-anchor fallback. A 2026-09-02 census (Kepler) found that
    # matching the name FIRST (as this loop used to) made a baton with a
    # perfectly valid, repo-qualified `github:` anchor to an already-MERGED
    # PR silently invisible to this whole pass whenever its project_id
    # didn't happen to match the name pattern -- exactly the discriminating
    # case tests/test_baton.py::TestGcAnchorFirst pins. So: for every
    # project, try the anchor FIRST. Any project_id whose `github:` field
    # resolves to a repo-qualified PR anchor (`_parse_github_anchor` returns
    # `("pr", <owner/repo>, <N>)` with a non-None repo) IS a PR-baton,
    # whatever its name looks like -- the name gate (_PR_BATON_NAME_RE) is
    # consulted only as a FALLBACK, for batons with no qualified anchor
    # (no anchor at all, an unparseable one, a non-PR anchor, or an
    # UNQUALIFIED bare `pr/<N>` -- #1918 above still applies to that last
    # case: it is never gc-eligible, whether reached via the anchor path or
    # the name-fallback path). A project_id that merely *contains* "PR-"
    # without matching the name pattern at all (e.g. a malformed "PR-abc")
    # is not silently dropped into the same continue as an unrelated
    # non-PR id -- it is tracked separately in near_miss_ids below so a
    # completeness read can see it, per the issue's own "Suggested fix" #2.
    pr_batons = []
    ungauged = 0
    near_miss_ids = []
    for p in all_projects:
        pid = p.get("project_id", "")
        parsed = _parse_github_anchor((p.get("github") or "").strip().lower())
        if parsed is not None and parsed[0] == "pr" and parsed[1] is not None:
            # Qualified PR anchor -- a PR-baton unconditionally, name shape
            # never consulted.
            _, repo, pr_num = parsed
            # `p` (this project's listing dict) is carried alongside so the
            # per-baton close() call below can compute its own #3039 U2a
            # round 3 conversion phase (_conversion_phase needs `turn` +
            # `github`, both present on the /api/projects listing; `phase`
            # itself is never surfaced by that listing, which is harmless --
            # see _conversion_phase's own callers for why a stale/incomplete
            # conversion INPUT can never cause a wrong WRITE: the write-body
            # re-reads fresh fields from disk and only stamps when its own
            # fresh read has no explicit phase yet).
            pr_batons.append((pid, repo, pr_num, p))
            if len(pr_batons) >= limit:
                break
            continue
        # No QUALIFIED PR anchor reached the branch above -- fall back to
        # _is_pr_baton (#3039 U2a), the same anchor-first, `pool-`-prefix-
        # wins-when-unanchored helper cmd_claim already uses, retiring this
        # function's own narrower, order-swapped `_PR_BATON_NAME_RE.match(pid
        # .upper())` test (Kepler's #3243 merge-seat conformance check,
        # 2026-09-30: that inline test misclassifies `pool-pr-*`-shaped bunch
        # ids as PR batons after `.upper()`, exactly the defect `_is_pr_baton`
        # fixes -- see its own docstring for the live-store measurement). `p`
        # is this loop's own project dict, which already carries the same
        # `github` key `_is_pr_baton` reads.
        if not _is_pr_baton(pid, p):
            if "PR-" in pid.upper():
                near_miss_ids.append(pid)
            continue
        ungauged += 1

    if near_miss_ids:
        print(
            f"baton gc: {len(near_miss_ids)} baton(s) look PR-related but did "
            f"not match the name pattern -- ids: {near_miss_ids}",
            file=sys.stderr,
        )

    if ungauged:
        print(
            f"baton gc: ungauged {ungauged} PR-baton(s) with no/invalid/"
            "unqualified github anchor -- run "
            "'baton anchor <id> --github pr/<owner>/<repo>/<N>' "
            "to make them gc-eligible.",
            file=sys.stderr,
        )

    closed = still_open = failed = 0

    for project_id, repo, pr_num, _p_fields in pr_batons:
        try:
            result = subprocess.run(
                ["gh", "pr", "view", pr_num, "--repo", repo, "--json", "state", "-q", ".state"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                # #1918: a query failure is a FAILURE, not a skip -- the old
                # code bucketed this under `skipped`, so a run's own summary
                # line could read "failed 0" immediately after printing 33
                # of these lines, masking a completely inert sweep as clean.
                print(f"baton gc: gh query failed for {project_id} (repo {repo}, pr #{pr_num})", file=sys.stderr)
                failed += 1
                continue
            state = result.stdout.strip().upper()
        except Exception as e:
            print(f"baton gc: error querying {project_id}: {e!r}", file=sys.stderr)
            failed += 1
            continue

        if state == "MERGED":
            new_status = "merged"
        elif state == "CLOSED":
            new_status = "cancelled"
        else:
            still_open += 1
            continue

        if dry_run:
            print(f"baton gc: would close {project_id} → {new_status} ({repo}#{pr_num} is {state})")
            closed += 1
            continue

        gc_reason = f"gc: {repo}#{pr_num} is {state}"
        # #3039 U2a round 3: conversion-only (this calls the SAME close()
        # write-body as cmd_close's /status dispatch -- close writes no
        # transition phase, per Kepler's ruling).
        _gc_phase = _conversion_phase(project_id, _p_fields)
        try:
            client.post(f"/api/projects/{project_id}/gc", {
                "agent": agent_name or "baton",
                "new_status": new_status,
                "reason": gc_reason,
                "phase": _gc_phase,
            })
            print(f"baton gc: closed {project_id} → {new_status} ({repo}#{pr_num})")
            closed += 1
        except (ForumNetworkError, ForumHttpError) as e:
            print(f"baton gc: write failed for {project_id}: {e!r}", file=sys.stderr)
            failed += 1
        except Exception as e:
            print(f"baton gc: unexpected error for {project_id}: {e!r}", file=sys.stderr)
            failed += 1

    action = "would close" if dry_run else "closed"
    print(
        f"baton gc: {action} {closed}, still-open {still_open}, "
        f"ungauged {ungauged}, failed {failed}"
    )

    # #1918: a non-zero exit when any lookup failed -- the prior all-exit-0
    # behavior is what let "failed 0, 33 failure lines printed above it" go
    # unnoticed for weeks. A cron/timer invocation (this command's own
    # docstring recommends one) needs the exit code to reflect this.
    if failed:
        sys.exit(EXIT_IO)


# ---------------------------------------------------------------------------
# Subcommand: rename
# ---------------------------------------------------------------------------

def cmd_rename(args: argparse.Namespace, config: dict, agent_name: str, client: ForumClient) -> None:
    _require_forum(config)

    project_id = args.project_id
    new_title = args.title

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    # Validate title before touching the API
    stripped_title = new_title.strip()
    if not stripped_title:
        print(
            "baton rename: --title must be non-empty.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)
    if len(stripped_title) > 200:
        print(
            f"baton rename: --title too long ({len(stripped_title)} chars); "
            "max 200. Did you accidentally paste a large block?",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)
    if "\n" in stripped_title or "\r" in stripped_title:
        print(
            "baton rename: --title must not contain newlines (YAML safety).",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    raw = _api_get_raw(client, project_id)
    fields, body = _parse_frontmatter(raw)
    if not fields:
        print(
            f"baton rename: cannot parse frontmatter for '{project_id}'. "
            "The record may be malformed.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    if "title" not in fields:
        print(
            f"baton rename: 'title' field absent in '{project_id}' — "
            "refusing to write to a possibly corrupted record.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # #3039 U2a round 3: conversion-only (rename was never a phase-machine
    # edge) -- reuses the `fields` already GET+parsed above.
    _rename_phase = _conversion_phase(project_id, fields)
    try:
        client.post(f"/api/projects/{project_id}/rename", {
            "agent": agent_name or "baton",
            "new_title": stripped_title,
            "phase": _rename_phase,
        })
    except ForumNetworkError as e:
        print(f"baton rename: {e}", file=sys.stderr)
        sys.exit(EXIT_IO)
    except ForumHttpError as e:
        if e.status == 404:
            print(f"baton rename: project not found: {project_id}", file=sys.stderr)
            sys.exit(EXIT_STATE)
        print(f"baton rename: server error {e.status}: {e.body}", file=sys.stderr)
        sys.exit(EXIT_IO if e.status >= 500 else EXIT_VALIDATION)
    print(f'baton rename: {project_id} title → "{stripped_title}"')


# ---------------------------------------------------------------------------
# Subcommand: anchor
# ---------------------------------------------------------------------------

def cmd_anchor(args: argparse.Namespace, config: dict, agent_name: str, client: ForumClient) -> None:
    """Set or update the github: anchor on an existing baton.

    Reads the project via the forum API for pre-flight validation (project
    exists, parseable frontmatter), then delegates the write to the forum
    API via client.post.
    """
    _require_forum(config)

    project_id = args.project_id
    github_anchor = args.github.strip().lower()

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    # Validate anchor value
    if not _GITHUB_ANCHOR_RE.match(github_anchor):
        print(
            f"baton anchor: invalid --github value '{github_anchor}'. "
            "Expected pr/<N>, pr/<owner>/<repo>/<N>, or project/<N> "
            "(e.g. pr/490, pr/engram-agents/engram-paper/22, project/4).",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    raw = _api_get_raw(client, project_id)
    fields, body = _parse_frontmatter(raw)
    if not fields:
        print(
            f"baton anchor: cannot parse frontmatter for '{project_id}'. "
            "The record may be malformed.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # #3039 U2a round 3: conversion-only (anchor was never a phase-machine
    # edge) -- reuses the `fields` already GET+parsed above.
    _anchor_phase = _conversion_phase(project_id, fields)
    try:
        client.post(f"/api/projects/{project_id}/anchor", {
            "agent": agent_name or "baton",
            "github": github_anchor,
            "phase": _anchor_phase,
        })
    except ForumNetworkError as e:
        print(f"baton anchor: {e}", file=sys.stderr)
        sys.exit(EXIT_IO)
    except ForumHttpError as e:
        if e.status == 404:
            print(f"baton anchor: project not found: {project_id}", file=sys.stderr)
            sys.exit(EXIT_STATE)
        print(f"baton anchor: server error {e.status}: {e.body}", file=sys.stderr)
        sys.exit(EXIT_IO if e.status >= 500 else EXIT_VALIDATION)
    print(f"baton anchor: {project_id} github → {github_anchor}")


# ---------------------------------------------------------------------------
# Subcommand: add-participant
# ---------------------------------------------------------------------------

def cmd_add_participant(args: argparse.Namespace, config: dict, agent_name: str, client: ForumClient) -> None:
    """Add a participant to an existing baton.

    Reads the project via the forum API for pre-flight validation (project
    exists, parseable frontmatter) and a friendly client-side already-a-
    participant hint, then delegates the write to the forum API via
    client.post. The AUTHORITATIVE dedup + agent-is-participant checks are
    server-side (add_participant() in coordination.projects) — this CLI's
    own already-present check is a UX nicety only, never the source of truth.
    """
    _require_forum(config)

    project_id = args.project_id
    new_participant = args.participant.strip().lower()

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    if not new_participant:
        print(
            "baton add-participant: <participant> must be non-empty.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    raw = _api_get_raw(client, project_id)
    fields, body = _parse_frontmatter(raw)
    if not fields:
        print(
            f"baton add-participant: cannot parse frontmatter for '{project_id}'. "
            "The record may be malformed.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Friendly client-side hint only — see docstring.
    participants_str = fields.get("participants", "").strip()
    current_participants = _parse_participants(participants_str) if participants_str else []
    already_present = new_participant in current_participants

    # #3039 U2a round 3: conversion-only (add_participant was never a
    # phase-machine edge) -- reuses the `fields` already GET+parsed above.
    _add_participant_phase = _conversion_phase(project_id, fields)
    try:
        resp = client.post(f"/api/projects/{project_id}/participants", {
            "agent": agent_name or "baton",
            "participant": new_participant,
            "phase": _add_participant_phase,
        })
    except ForumNetworkError as e:
        print(f"baton add-participant: {e}", file=sys.stderr)
        sys.exit(EXIT_IO)
    except ForumHttpError as e:
        if e.status == 404:
            print(f"baton add-participant: project not found: {project_id}", file=sys.stderr)
            sys.exit(EXIT_STATE)
        if e.status == 403:
            print(
                f"baton add-participant: '{agent_name or 'you'}' must already be a "
                f"participant of '{project_id}' to add others.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)
        print(f"baton add-participant: server error {e.status}: {e.body}", file=sys.stderr)
        sys.exit(EXIT_IO if e.status >= 500 else EXIT_VALIDATION)

    added = resp.get("added", True)
    if already_present or not added:
        print(f"baton add-participant: {new_participant} is already a participant of {project_id}.")
    else:
        print(f"baton add-participant: {project_id} + participant {new_participant}")


# ---------------------------------------------------------------------------
# Subcommand: set-colleague  (#2764)
# ---------------------------------------------------------------------------

def cmd_set_colleague(args: argparse.Namespace, config: dict, agent_name: str, client: ForumClient) -> None:
    """Set or change the colleague: field on an existing baton.

    Reads the project via the forum API for pre-flight validation (project
    exists, parseable frontmatter), then delegates the write to the forum
    API via client.post — the AUTHORITATIVE authorization check (caller must
    already be a participant OR the current turn holder) is server-side
    (set_colleague() in coordination.projects), same split as
    cmd_add_participant.
    """
    _require_forum(config)

    project_id = args.project_id
    colleague = args.colleague.strip().lower()

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    if not colleague:
        print(
            "baton set-colleague: <colleague> must be non-empty.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    raw = _api_get_raw(client, project_id)
    fields, _body = _parse_frontmatter(raw)
    if not fields:
        print(
            f"baton set-colleague: cannot parse frontmatter for '{project_id}'. "
            "The record may be malformed.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # #3039 U2a round 3: conversion-only (set_colleague was never a
    # phase-machine edge) -- reuses the `fields` already GET+parsed above.
    _set_colleague_phase = _conversion_phase(project_id, fields)
    try:
        resp = client.post(f"/api/projects/{project_id}/colleague", {
            "agent": agent_name or "baton",
            "colleague": colleague,
            "phase": _set_colleague_phase,
        })
    except ForumNetworkError as e:
        print(f"baton set-colleague: {e}", file=sys.stderr)
        sys.exit(EXIT_IO)
    except ForumHttpError as e:
        if e.status == 404:
            print(f"baton set-colleague: project not found: {project_id}", file=sys.stderr)
            sys.exit(EXIT_STATE)
        if e.status == 403:
            print(
                f"baton set-colleague: '{agent_name or 'you'}' must already be a "
                f"participant of '{project_id}', or its current turn holder, "
                "to set colleague.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)
        print(f"baton set-colleague: server error {e.status}: {e.body}", file=sys.stderr)
        sys.exit(EXIT_IO if e.status >= 500 else EXIT_VALIDATION)

    participant_added = resp.get("participant_added", False)
    suffix = " (added as participant)" if participant_added else ""
    print(f"baton set-colleague: {project_id} colleague → {colleague}{suffix}")


# ---------------------------------------------------------------------------
# Subcommand: merge  (gate-checked merge verb — #999 + #1000)
# ---------------------------------------------------------------------------

def cmd_merge(args: argparse.Namespace, config: dict, agent_name: str, client: ForumClient) -> None:
    """Squash-merge a PR through the baton gate ladder.

    Gate ladder (all must pass in order):
      1. Baton exists (via forum API) for PR-N.         (never forceable)
      2. turn is a maintainer-role holder -- the pool     (never forceable)
         sentinel OR any named agent that is not the
         registered colleague or the PR's own author
         (#2913; see Gate 2's own comment for the reused
         _is_maintainer_flip predicate and why the check
         reduces cleanly to a terminal-state read).
      3. CI green via _pr_ci_state.                      (--force skips)
      4. Approval fresh via _pr_approval_state (oid).    (--force skips)
      5. gh pr merge <N> --squash.
      6. On success: baton status → merged via API.

    --force skips gates 3-4 only; never skips 1-2.
    --dry-run prints the ladder verdict without merging.
    """
    _require_forum(config)

    # Resolve project_id and pr_number from the argument (PR-N or bare N)
    raw_arg = args.pr.strip()
    m = re.match(r"^(?:PR-|pr-)(\d+)$", raw_arg)
    if m:
        pr_number = m.group(1)
        project_id = f"PR-{pr_number}"
    else:
        pr_number = raw_arg.lstrip("#")
        project_id = f"PR-{pr_number}"

    if not pr_number.isdigit():
        print(
            f"baton merge: cannot resolve PR number from '{args.pr}'. "
            "Expected 'PR-123' or a bare number.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    force = getattr(args, "force", False)
    dry_run = getattr(args, "dry_run", False)

    # ---- Gate 1: baton exists ----------------------------------------
    raw = _api_get_raw(client, project_id)
    fields, body = _parse_frontmatter(raw)
    if not fields:
        print(
            f"baton merge: cannot parse frontmatter for '{project_id}'. "
            "The record may be malformed.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Reject already-closed batons (merging a merged/cancelled baton is
    # almost certainly an operator error)
    current_status = fields.get("status", "").strip().lower()
    if current_status in CLOSED_STATUSES:
        print(
            f"baton merge: project '{project_id}' is already {current_status} — "
            "nothing to merge.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # ---- Gate 2: turn is a maintainer-role holder, not the literal
    # pool sentinel specifically --------------------------------------
    #
    # #2913 (same root cause as #2912's cmd_flip fix): the bare
    # `_is_pool_sentinel(current_turn)` check requires the baton to be
    # flipped to the LITERAL sentinel string (e.g. "lei shi") before
    # `baton merge` will proceed. But this team's actual convention --
    # every flip performed this session, and CLAUDE.md's own baton
    # lifecycle -- presents a PR to a NAMED maintainer agent (e.g.
    # "kepler", "ariadne"), never the literal sentinel. So this gate, as
    # written, would refuse to merge a baton in the exact state the real
    # pipeline actually leaves it in.
    #
    # Reuses `_is_maintainer_flip` (already defined above, the same
    # function `cmd_flip` calls) rather than adding a fourth copy of this
    # classification logic. The DIRECTION differs from cmd_flip's own use:
    # cmd_flip asks "is flipping TO to_agent a merge presentation" (a
    # transition); Gate 2 asks "IS the current holder a merge-presented
    # maintainer" (a terminal-state check, no transition in view). Passed
    # as `_is_maintainer_flip(current_turn, "", colleague_name, author_name)`
    # -- the empty string for `current_holder` deliberately disables the
    # self-flip branch, which has no meaning here (there is no "flipping
    # away from" state to compare against; current_turn already IS both
    # the value being classified and the only state in hand). This is
    # exactly the reduction _is_maintainer_flip needs to fall through to
    # its actual "still with the colleague" / "still with the author" /
    # "everyone else, including the sentinel" logic, and the guaranteed-
    # non-empty check on current_turn just above rules out the one input
    # where an empty `current_holder` sentinel could spuriously self-match.
    sentinel = _pool_sentinel()
    current_turn = fields.get("turn", "").strip().lower()
    if not current_turn:
        print(
            "baton merge: turn is with (unknown) — not presented for merge. "
            f"The baton must be flipped to the pool sentinel ('{sentinel}') "
            "or a maintainer agent before merging.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)
    _merge_colleague_name = fields.get("colleague", "").strip().lower()
    _merge_author_name = fields.get("author", "").strip().lower() or _derive_legacy_author(body)
    if not _is_maintainer_flip(current_turn, "", _merge_colleague_name, _merge_author_name):
        print(
            f"baton merge: turn is with {current_turn} — not presented for "
            "merge (still with the registered colleague or the PR's own "
            f"author). The baton must be flipped to the pool sentinel "
            f"('{sentinel}') or a maintainer agent before merging.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # #1715 (reviewer-fairy-caught, round 2) / #1918: resolve the repo from
    # the baton's stored `github` anchor, the SAME pattern already used in
    # cmd_flip -- never trust gh's ambient-cwd inference for a genuinely
    # cross-repo PR-baton. This gate's stakes are higher than cmd_flip's:
    # gate 5 below performs an IRREVERSIBLE `gh pr merge --squash`, so
    # silently resolving the wrong repo here doesn't just misreport a
    # status, it can squash-merge an unrelated PR that happens to share
    # this baton's PR number in whatever repo the resolution guessed.
    #
    # #1918: there is no default repo to fall back to, so this is now a
    # hard, NEVER-forceable gate (same tier as gates 1-2 above) -- a baton
    # with no anchor, an unparseable one, or an unqualified pr/<N> anchor
    # refuses rather than merging on a guessed repo + a trusted CLI number.
    # --force skips CI/approval quality gates (3-4); it has never meant
    # "merge without knowing which repo," and this gate does not change
    # that contract.
    _merge_anchor = _parse_github_anchor(fields.get("github", "").strip().lower())
    if _merge_anchor is None or _merge_anchor[0] != "pr" or _merge_anchor[1] is None:
        print(
            f"baton merge: '{project_id}' has no repo-qualified github anchor "
            f"-- refusing to guess which repo's PR #{pr_number} to squash-merge. "
            f"Run 'baton anchor {project_id} --github pr/<owner>/<repo>/{pr_number}' "
            "first. This is not skippable with --force.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)
    _merge_repo = _merge_anchor[1]
    if _merge_anchor[2] != pr_number:
        print(
            f"baton merge: PR number mismatch -- '{args.pr}' resolves to "
            f"#{pr_number}, but {project_id}'s stored github anchor "
            f"points at #{_merge_anchor[2]}. Refusing rather than "
            "guessing which is correct.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # ---- Gate 3: CI green (skippable with --force) --------------------
    _ci_verdict = "green"
    if force:
        # Audit-trail parity with cmd_flip --force: still query the actual
        # state so the forced merge's warning (and turn-log) record what was
        # bypassed, not just that something was.
        _ci_state, _ci_detail = _pr_ci_state(pr_number, repo=_merge_repo)
        print(
            f"warning: --force — skipping CI-green check for PR #{pr_number} "
            f"(actual: {_ci_state} — {_ci_detail})",
            file=sys.stderr,
        )
        _ci_verdict = f"skipped (--force; actual: {_ci_state})"
    else:
        _ci_state, _ci_detail = _pr_ci_state(pr_number, repo=_merge_repo)
        if _ci_state in ("red", "pending"):
            print(
                f"baton merge: CI is {_ci_state} on PR #{pr_number} "
                f"({_ci_detail}). "
                "Re-merge when checks are green, or pass --force to override.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)
        elif _ci_state == "unknown":
            print(
                f"warning: could not verify CI state for PR #{pr_number} "
                f"({_ci_detail}) — proceeding anyway",
                file=sys.stderr,
            )
            _ci_verdict = f"unknown ({_ci_detail})"
        else:
            _ci_verdict = f"green ({_ci_detail})"

    # ---- Gate 4: approval fresh (skippable with --force) --------------
    _ap_verdict = "fresh"
    if force:
        _ap_state, _ap_detail = _pr_approval_state(pr_number, repo=_merge_repo)
        print(
            f"warning: --force — skipping approval-fresh check for PR "
            f"#{pr_number} (actual: {_ap_state} — {_ap_detail})",
            file=sys.stderr,
        )
        _ap_verdict = f"skipped (--force; actual: {_ap_state})"
    else:
        _ap_state, _ap_detail = _pr_approval_state(pr_number, repo=_merge_repo)
        if _ap_state == "stale":
            print(
                f"baton merge: tip moved after approval on PR #{pr_number} "
                f"({_ap_detail}). The approved commit is not the commit "
                "that would be merged — request a re-review of the new tip, "
                "or pass --force to override.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)
        elif _ap_state == "unknown":
            print(
                f"warning: could not verify post-approval state for PR "
                f"#{pr_number} ({_ap_detail}) — proceeding anyway",
                file=sys.stderr,
            )
            _ap_verdict = f"unknown ({_ap_detail})"
        else:
            # fresh or no_approval — both pass through
            _ap_verdict = f"{_ap_state} ({_ap_detail})"

    # ---- dry-run: print verdict and exit cleanly ---------------------
    if dry_run:
        print(f"baton merge --dry-run: PR #{pr_number} ({project_id})")
        print(f"  gate 1 (baton exists): PASS")
        print(f"  gate 2 (turn=maintainer): PASS — turn={current_turn}")
        print(f"  gate 3 (CI):            {_ci_verdict}")
        print(f"  gate 4 (approval):      {_ap_verdict}")
        print(f"  merge: DRY-RUN — no merge performed")
        return

    # ---- Gate 5: gh pr merge --squash --------------------------------
    # #1715: --repo is REQUIRED here, not optional -- this is the single
    # most consequential gh call in this whole bug class (an irreversible
    # squash-merge). Omitting --repo would let gh's ambient-cwd guess
    # decide which repo's PR #N gets merged.
    try:
        merge_result = subprocess.run(
            ["gh", "pr", "merge", pr_number, "--squash", "--repo", _merge_repo],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        print(
            f"baton merge: gh not found on PATH — cannot merge PR #{pr_number}. "
            "Baton state unchanged.",
            file=sys.stderr,
        )
        sys.exit(EXIT_IO)
    except subprocess.TimeoutExpired:
        print(
            f"baton merge: gh timed out after 60s merging PR #{pr_number}. "
            "Baton state unchanged.",
            file=sys.stderr,
        )
        sys.exit(EXIT_IO)
    except OSError as exc:
        print(
            f"baton merge: gh exec error: {exc}. "
            "Baton state unchanged.",
            file=sys.stderr,
        )
        sys.exit(EXIT_IO)

    if merge_result.returncode != 0:
        gh_err = (merge_result.stderr or "").strip() or f"gh exit {merge_result.returncode}"
        print(
            f"baton merge: gh pr merge failed for PR #{pr_number}: {gh_err}. "
            "Baton state unchanged.",
            file=sys.stderr,
        )
        sys.exit(EXIT_IO)

    # ---- Gate 6: post-merge baton closure via API ----------------------------
    merge_by = agent_name or "unknown"

    # The GitHub merge above is irreversible; say so BEFORE the API write so
    # a failure is diagnosable as merged-on-GitHub/baton-stale.
    print(f"baton merge: PR #{pr_number} merged on GitHub; writing baton closure...")
    # #3039 U2a round 3: conversion-only (merge writes no transition phase,
    # per Kepler's ruling) -- reuses the `fields` already GET+parsed at
    # Gate 1 above.
    _merge_phase = _conversion_phase(project_id, fields)
    try:
        client.post(f"/api/projects/{project_id}/merge", {
            "agent": merge_by,
            "forced": force,
            "phase": _merge_phase,
        })
    except ForumNetworkError as e:
        print(f"baton merge: server unreachable writing closure: {e}", file=sys.stderr)
        print(
            f"  (GitHub merge succeeded; close baton manually: "
            f"baton close {project_id} --status merged)",
            file=sys.stderr,
        )
        sys.exit(EXIT_IO)
    except ForumHttpError as e:
        print(f"baton merge: baton closure failed ({e.status}): {e.body}", file=sys.stderr)
        sys.exit(EXIT_IO)

    print(f"baton merge: PR #{pr_number} merged (squash)")
    print(f"  baton: {project_id} → merged")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="baton",
        description=(
            "Multi-agent turn-state CLI — declare whose move it is on a "
            "shared project via explicit baton files."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Protocol docs: docs/baton-protocol.md\n"
            "Projects dir:  /home/agents-shared/forum/projects/ (manual deploy)\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="subcommand", metavar="subcommand")
    subparsers.required = True

    # -- init --
    p_init = subparsers.add_parser(
        "init",
        help="Create a new project baton file",
        description=(
            "Create a baton file for a shared project. Fails if the file "
            "already exists. Turn defaults to the invoking agent if they are "
            "in the participants list."
        ),
    )
    p_init.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier in TYPE-ID format (e.g. PR-425, DESIGN-trust-tier-v2)",
    )
    p_init.add_argument(
        "--title", metavar="TEXT", required=True,
        help="Human-readable project title",
    )
    p_init.add_argument(
        "--participants", metavar="AGENTS", required=True,
        help="Comma-separated list of participant agent names (e.g. borges,ariadne)",
    )
    p_init.add_argument(
        "--status",
        choices=list(VALID_STATUSES),
        default="planning",
        help="Initial project status (default: planning)",
    )
    p_init.add_argument(
        "--turn", metavar="AGENT",
        help="Who holds the baton initially (default: invoking agent if in participants)",
    )
    p_init.add_argument(
        "--github", metavar="ANCHOR",
        help=(
            "GitHub anchor for live status in the auto-pull hook. "
            "Format: pr/<N>, pr/<owner>/<repo>/<N>, or project/<N> "
            "(e.g. pr/490, pr/engram-agents/engram-paper/22, project/4). "
            "A PR-<N> project_id auto-derives pr/<N> if this is omitted."
        ),
    )
    p_init.add_argument(
        "--repo", metavar="OWNER/REPO",
        help=(
            "Repo-qualify a PR anchor (e.g. engram-agents/engram-paper). "
            "Effectively REQUIRED for every PR-baton (#1918: there is no "
            "default repo) -- an unqualified pr/<N> anchor is refused and "
            "counted (never guessed against) by gc/flip/merge."
        ),
    )
    p_init.add_argument(
        "--colleague", metavar="AGENT",
        help=(
            "Name the expected colleague reviewer. Added to participants (dedup) "
            "and noted in the baton body. Enables the colleague-gate at flip time: "
            "baton flip PR-N <sentinel> will be rejected if no non-author APPROVED "
            "review exists. Without this, the gate falls back to single-agent "
            "(warn-only) mode if no colleague appears in --participants."
        ),
    )

    # -- flip --
    p_flip = subparsers.add_parser(
        "flip",
        help="Pass the baton to another participant",
        description=(
            "Atomically update turn, turn_since, and turn_reason in the "
            "project file, and append a turn-log line. "
            "TO must be a participant or the pool sentinel (the install's primary_user)."
        ),
    )
    p_flip.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier",
    )
    p_flip.add_argument(
        "to",
        metavar="TO",
        help="Agent or the pool sentinel (primary_user) to pass the baton to",
    )
    p_flip.add_argument(
        "reason",
        metavar="REASON",
        help="Short description of why the baton is being passed",
    )
    p_flip.add_argument(
        "--force", action="store_true", default=False,
        help="skip the CI-green guard (override a flaky/known-red check)",
    )

    # -- claim --
    p_claim = subparsers.add_parser(
        "claim",
        help="Claim a Project-layer baton from the pool (pool → self)",
        description=(
            "Take a Project-layer baton for yourself. Only succeeds when the "
            "current turn is the pool sentinel (the install's primary_user). "
            "Refuses when the turn is not this seat's recognized pool marker "
            "— which may be a colleague genuinely holding it, or a pool or "
            "maintainer marker written by a seat with a different "
            "primary_user (engram-alpha#3039). To see what the turn log "
            "records, run 'baton show <project-id>' and find the LAST turn "
            "log line shaped '<A> → <B>: <reason>' whose <B> -- the name "
            "just after the FIRST arrow -- is the turn value. Only such a "
            "line records who the turn was handed to: anchor, colleague and "
            "reopen lines carry arrows of their own and name no holder, and "
            "turn_reason does not discriminate (every claim writes "
            "'claimed'). '<pool> → <B>: claimed' means a colleague took it "
            "-- use 'baton flip' with an explicit handoff agreement. ANY "
            "OTHER such line leaves this seat unable to tell which case it "
            "is: release and flip emit the identical text, so the line is "
            "either a colleague taking it "
            "on a flip or a return to a pool marker belonging to a seat "
            "whose primary_user this one cannot resolve -- that is #3039. A "
            "<reason> of 'released' is release's default but is "
            "user-settable, so it hints and never settles. Settle it off "
            "the log: ask <B>, or check the PR or project this baton "
            "anchors -- do not flip on a guess.\n\n"
            "Vocabulary distinction: claim/release are Project-layer verbs "
            "(single driver, rare passes). flip is the PR-layer verb (frequent "
            "passing through fairy → reviewer → colleague → maintainer)."
        ),
    )
    p_claim.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier (e.g. pool-cli-gap, DESIGN-trust-tier-v2)",
    )

    # -- release --
    p_release = subparsers.add_parser(
        "release",
        help="Return a Project-layer baton to the pool (self → pool sentinel)",
        description=(
            "Release a Project-layer baton back to the pool sentinel "
            "(the install's primary_user). "
            "Only the current holder can release. Use --done to also append "
            "'(done)' to the project title (idempotent).\n\n"
            "Vocabulary distinction: claim/release are Project-layer verbs "
            "(single driver, rare passes). flip is the PR-layer verb (frequent "
            "passing through fairy → reviewer → colleague → maintainer)."
        ),
    )
    p_release.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier",
    )
    p_release.add_argument(
        "--done", action="store_true", default=False,
        help="Also append '(done)' to the project title (idempotent)",
    )
    p_release.add_argument(
        "--reason", metavar="TEXT", default="released",
        help="Short description of why the baton is being released (default: released)",
    )

    # -- status --
    p_status = subparsers.add_parser(
        "status",
        help="List active projects (sorted by turn_since ascending)",
        description=(
            "List all active projects (status not in merged, cancelled). "
            "Use --mine to filter to projects where turn == invoking agent."
        ),
    )
    p_status.add_argument(
        "--mine", action="store_true", default=False,
        help="Only show projects where the baton is in my court",
    )

    # -- mine --
    p_mine = subparsers.add_parser(
        "mine",
        help="Shorthand for 'baton status --mine'",
        description="Show projects where the baton is currently in your court.",
    )

    # -- show --
    p_show = subparsers.add_parser(
        "show",
        help="Display a project file (read-only)",
        description="Print the full contents of a project baton file.",
    )
    p_show.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier",
    )

    # -- close --
    p_close = subparsers.add_parser(
        "close",
        help="Mark a project merged or cancelled",
        description=(
            "Update the project's status to 'merged' or 'cancelled'. "
            "Closed projects no longer appear in 'baton status' or 'baton mine'."
        ),
    )
    p_close.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier",
    )
    p_close.add_argument(
        "--status",
        required=True,
        choices=list(CLOSED_STATUSES),
        help="Final status: merged or cancelled",
    )

    # -- reopen --
    p_reopen = subparsers.add_parser(
        "reopen",
        help="Flip a closed baton back to an active status (inverse of close)",
        description=(
            "Flip a closed baton (status: merged or cancelled) back to an active status.\n"
            "Inverse of 'baton close'. Refuses if the baton is already active — use\n"
            "'baton flip' instead."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_reopen.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier",
    )
    p_reopen.add_argument(
        "--status",
        choices=sorted(ACTIVE_STATUSES),
        default="in-progress",
        help="Active status to set (default: in-progress)",
    )

    # -- gc --
    p_gc = subparsers.add_parser(
        "gc",
        help="batch-close PR-batons whose GitHub PR is merged/closed",
        description=(
            "Scan all open PR-batons and close any whose live GitHub PR state\n"
            "is terminal (MERGED → status:merged; CLOSED → status:cancelled).\n\n"
            "Use --dry-run to preview without applying changes.\n"
            "Use --limit N to cap the number of PR-batons processed per run\n"
            "(default: 30) to bound wall-clock runtime.\n\n"
            "This is the explicit full-sweep complement to the prompt hook's\n"
            "cache-only auto-archive pass.  Run at loop-start to drain the\n"
            "backlog of merged/closed PR-batons the hook cannot reach."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_gc.add_argument(
        "--dry-run", action="store_true", default=False,
        dest="dry_run",
        help="list changes without applying them",
    )
    p_gc.add_argument(
        "--limit", type=int, default=30,
        help="max PR-batons to process per run (default: 30)",
    )
    p_gc.set_defaults(func=cmd_gc)

    # -- rename --
    p_rename = subparsers.add_parser(
        "rename",
        help="Update the human-readable project title",
        description=(
            "Replace the title: field in the project frontmatter with a new "
            "value, atomically. Appends an audit line to the turn log. "
            "Does not change the project ID or file name."
        ),
    )
    p_rename.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier",
    )
    p_rename.add_argument(
        "--title", metavar="TEXT", required=True,
        help="New human-readable title (max 200 chars, no newlines)",
    )

    # -- anchor --
    p_anchor = subparsers.add_parser(
        "anchor",
        help="Set or update the github: anchor on an existing baton",
        description=(
            "Set or replace the github: frontmatter field on an existing baton "
            "file, enabling live GitHub status in the auto-pull hook. "
            "Atomically updates the field and appends an audit line. "
            "Use this for batons already created without --github, or to "
            "correct an existing anchor."
        ),
    )
    p_anchor.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier",
    )
    p_anchor.add_argument(
        "--github", metavar="ANCHOR", required=True,
        help=(
            "GitHub anchor value: pr/<N>, pr/<owner>/<repo>/<N>, or project/<N> "
            "(e.g. pr/490, pr/engram-agents/engram-paper/22, project/4). "
            "Repo-qualify (#1715, #1918) every PR anchor -- there is no "
            "default repo, and gc/flip/merge refuse an unqualified one."
        ),
    )

    # -- add-participant --
    p_add_participant = subparsers.add_parser(
        "add-participant",
        help="Add an agent to an existing baton's participants list",
        description=(
            "Add <participant> to the project's participants list, atomically. "
            "Appends an audit line to the turn log. Idempotent: adding an "
            "agent who is already a participant is a no-op, not an error. "
            "The invoking agent must already be a participant of the baton "
            "(server-side authorization) — non-participants cannot add "
            "others. Fixes the 2-reviewer-pipeline friction where 'baton "
            "flip' to a not-yet-participant refuses with 'not a participant'."
        ),
    )
    p_add_participant.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier",
    )
    p_add_participant.add_argument(
        "participant",
        metavar="PARTICIPANT",
        help="Agent name to add as a participant",
    )

    # -- set-colleague --
    p_set_colleague = subparsers.add_parser(
        "set-colleague",
        help="Set or change the colleague: field on an existing baton (#2764)",
        description=(
            "Set or replace the colleague: frontmatter field on an existing "
            "baton, atomically, and add <colleague> to participants (dedup) "
            "in the same write -- exactly the behavior 'baton init "
            "--colleague' already has, now available after init too, and "
            "now persisted (pre-#2764, --colleague at init never wrote "
            "anything server-side, so 'baton show' could never render it). "
            "The invoking agent must already be a participant of the baton, "
            "OR its current turn holder (server-side authorization) -- this "
            "is what lets the baton's holder name a colleague who then has "
            "no need to add themselves. Does not change what the flip-time "
            "colleague-gate enforces (still participants-derived); this is "
            "about making the colleague identity settable and visible, not "
            "about changing enforcement."
        ),
    )
    p_set_colleague.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier",
    )
    p_set_colleague.add_argument(
        "colleague",
        metavar="COLLEAGUE",
        help="Agent name to set as the expected colleague reviewer",
    )

    # -- merge --
    p_merge = subparsers.add_parser(
        "merge",
        help="Gate-checked squash merge for a PR baton",
        description=(
            "Squash-merge a PR after passing the baton gate ladder:\n"
            "  1. Baton exists for PR-N          (never forceable)\n"
            "  2. turn == pool sentinel            (never forceable)\n"
            "  3. CI green                         (--force skips)\n"
            "  4. Approval fresh (oid-compare)     (--force skips)\n"
            "  5. gh pr merge N --squash\n"
            "  6. Baton status → merged\n\n"
            "Prevents merging a PR that hasn't been presented to the maintainer\n"
            "via the baton flip pipeline, or merging with failing CI or a stale\n"
            "approval. --force overrides only the CI and approval gates (3-4);\n"
            "a missing baton or unflipped turn is never forceable."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_merge.add_argument(
        "pr",
        metavar="PR-N",
        help="PR number in PR-N or bare-N form (e.g. PR-123 or 123)",
    )
    p_merge.add_argument(
        "--force", action="store_true", default=False,
        help=(
            "Skip CI-green and approval-fresh gates (3-4). "
            "Does NOT skip baton-exists or turn-sentinel gates (1-2)."
        ),
    )
    p_merge.add_argument(
        "--dry-run", action="store_true", default=False,
        dest="dry_run",
        help="Evaluate all gates and print the verdict; do not merge.",
    )

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    config = _load_config()
    agent_name = _get_agent_name(config)
    client = ForumClient.from_config(config)

    # Dispatch — all commands receive client (read commands use it for API
    # reads; write commands use it for both reads and writes).
    if args.subcommand == "init":
        cmd_init(args, config, agent_name, client)
    elif args.subcommand == "flip":
        cmd_flip(args, config, agent_name, client)
    elif args.subcommand == "claim":
        cmd_claim(args, config, agent_name, client)
    elif args.subcommand == "release":
        cmd_release(args, config, agent_name, client)
    elif args.subcommand == "status":
        cmd_status(args, config, agent_name, client)
    elif args.subcommand == "mine":
        cmd_mine(args, config, agent_name, client)
    elif args.subcommand == "show":
        cmd_show(args, config, agent_name, client)
    elif args.subcommand == "close":
        cmd_close(args, config, agent_name, client)
    elif args.subcommand == "reopen":
        cmd_reopen(args, config, agent_name, client)
    elif args.subcommand == "gc":
        cmd_gc(args, config, agent_name, client)
    elif args.subcommand == "rename":
        cmd_rename(args, config, agent_name, client)
    elif args.subcommand == "anchor":
        cmd_anchor(args, config, agent_name, client)
    elif args.subcommand == "add-participant":
        cmd_add_participant(args, config, agent_name, client)
    elif args.subcommand == "set-colleague":
        cmd_set_colleague(args, config, agent_name, client)
    elif args.subcommand == "merge":
        cmd_merge(args, config, agent_name, client)
    else:
        parser.print_help()
        sys.exit(EXIT_VALIDATION)


if __name__ == "__main__":
    main()
