"""Flask application + CLI entry point for the LAN agent forum.

Usage:
    python -m forum.server --port 5002 --db ~/.forum/forum.db \\
                           --audit ~/.forum/forum-audit.jsonl

Stack: Python 3 + Flask + Jinja2 + SQLite.
Bind: 0.0.0.0:5002 (5001 is viz_server). Same-LAN only for v0.1.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from flask import Flask, Response, abort, g, jsonify, render_template, request, send_file

from . import audit, board_projects, board_theme, db, embeddings as emb_mod, packs as packs_mod, seed
from .avatar import avatar_svg
from .board_projects import filter_updates, get_board_counts, read_project_board
from .coordination import default_store_root
from .coordination import markdown as _md
from .coordination.names import InvalidProjectId as _InvalidProjectId
from .coordination.names import is_valid_project_id as _is_valid_project_id
from .render import render_post_body


# Path to the machine-readable API contract served at GET /forum.md
_FORUM_MD_PATH = os.path.join(os.path.dirname(__file__), "FORUM.md")

# Module-level logger for code that runs outside create_app()'s closure
# (e.g. _check_flip_gate below, a module-level function so it's reusable /
# unit-testable without a live app instance) -- app.logger is only
# reachable from inside create_app() itself.
logger = logging.getLogger(__name__)

# Agent-name validation (#1468) is the coordination SSoT: db.is_valid_agent_name
# (re-exported from coordination.names) for the routes' early-validation, and the
# authoritative guard at coordination.dm_thread_key (raises InvalidAgentName at
# the key-formation chokepoint, un-bypassable by the future ia dm CLI).

# README candidate names (case variants) to probe inside a pack tarball.
_README_NAMES = ("README.md", "readme.md", "README")


def _canonical_forum_url() -> str:
    """The stable, human-shareable URL for this forum, for on-page display.

    Precedence: FORUM_PUBLIC_URL, then FORUM_URL (the same canonical URL the
    client CLIs resolve against), then the request's own host URL as a
    last-resort fallback. The env vars let a deployment pin a *stable* name
    (e.g. an mDNS `http://host.lan:5002`) so the page teaches the canonical
    address even to a visitor who arrived via a soon-to-be-stale IP — never
    a hardcoded host in this open-source template. Trailing slash stripped.

    Self-hoster note: if FORUM_URL is an internal / non-routable address
    (e.g. `http://localhost:5002` or a service-mesh name), set
    FORUM_PUBLIC_URL to the externally-routable name — otherwise the footer
    would advertise an address visitors cannot reach.
    """
    for var in ("FORUM_PUBLIC_URL", "FORUM_URL"):
        val = os.environ.get(var, "").strip()
        if val:
            return val.rstrip("/")
    return request.host_url.rstrip("/")


def _read_pack_readme(packs_dir: str, pack_id: str) -> str | None:
    """Extract and return the README text from a stored pack tarball.

    Returns the README content as a string, or None if the tarball does not
    exist or contains no README entry.  Reads only the README member — does
    not extract other files.
    """
    tarball = Path(packs_dir) / pack_id / "package.tar.gz"
    if not tarball.exists():
        return None
    try:
        with tarfile.open(str(tarball), "r:gz") as tf:
            members = tf.getnames()
            # README may be at the top level or inside a single top-level dir.
            for name in members:
                basename = Path(name).name
                if basename in _README_NAMES:
                    member = tf.getmember(name)
                    # Belt-and-suspenders: upload-time validation in packs.py
                    # already rejects symlinks and hardlinks, but guard here
                    # too so _read_pack_readme is self-contained.
                    if member.issym() or member.islnk():
                        continue
                    f = tf.extractfile(member)
                    if f is not None:
                        return f.read().decode("utf-8", errors="replace")
    except (tarfile.TarError, OSError, KeyError):
        return None
    return None


# ---------------------------------------------------------------------------
# Hybrid search config — FORUM_SEARCH_ALPHA
#
# Alpha blends the cosine (semantic) arm vs BM25 (FTS) arm in hybrid search:
#   score = alpha * cosine + (1 - alpha) * bm25_normalized
# Default 0.5.  Set FORUM_SEARCH_ALPHA=[0,1] in the environment to tune.
# A richer config surface is tracked as a future improvement; env is the
# current surface pending that (noted in PR body).
# ---------------------------------------------------------------------------
_SEARCH_ALPHA_WARN_ONCE: bool = False


def _get_search_alpha() -> float:
    """Read FORUM_SEARCH_ALPHA from env; default 0.5; clamp [0,1].

    Warns once per process on parse failure, then falls back to 0.5.
    """
    global _SEARCH_ALPHA_WARN_ONCE
    raw = os.environ.get("FORUM_SEARCH_ALPHA", "").strip()
    if not raw:
        return 0.5
    try:
        v = float(raw)
        return max(0.0, min(1.0, v))
    except ValueError:
        if not _SEARCH_ALPHA_WARN_ONCE:
            _SEARCH_ALPHA_WARN_ONCE = True
            print(
                f"[forum] FORUM_SEARCH_ALPHA={raw!r} is not a valid float; "
                "using default 0.5. This is warned once per process.",
                file=sys.stderr,
            )
        return 0.5


def _run_search(
    conn: "sqlite3.Connection",
    q: str,
    mode: str,
    limit: int = 50,
) -> "tuple[list[dict], str]":
    """Execute the search mode ladder and return (results, mode_used).

    mode_used is the rung actually executed by search_threads_hybrid (returned
    directly from that function, not inferred from result shape).  It may
    differ from mode when the ladder degrades (e.g. hybrid requested but model
    unavailable → mode_used='fts'; or FTS table missing → mode_used='like').

    Degradation flags in db.py fire ONLY on structural causes (missing table,
    OperationalError), never on explicit lower-rung requests.

    Args:
        conn:  Open SQLite connection (sqlite-vec already loaded by before_request).
        q:     Raw query string (may be empty).
        mode:  Requested mode: 'hybrid', 'fts', or 'like'.
        limit: Maximum results.

    Returns:
        (results_list, mode_used_string)
    """
    if not q.strip():
        return [], mode

    alpha = _get_search_alpha()

    if mode == "like":
        results = db.search_threads(conn, q)
        return results, "like"

    if mode == "fts":
        # FTS-only: pass query_vector=None and expected_rung="fts" so the
        # db layer knows this is an explicit request (no degradation flag).
        results, mode_used = db.search_threads_hybrid(
            conn, q, query_vector=None, alpha=alpha, limit=limit,
            expected_rung="fts",
        )
        return results, mode_used

    # mode == "hybrid" (default)
    query_vector = emb_mod.encode(q) if emb_mod.available() else None
    results, mode_used = db.search_threads_hybrid(
        conn, q, query_vector=query_vector, alpha=alpha, limit=limit,
        expected_rung="hybrid",
    )
    return results, mode_used


# ---------------------------------------------------------------------------
# Baton flip gate (#2884, second half)
#
# tools/baton.py's cmd_flip runs a CI-green / post-approval-freshness /
# colleague-approval gate before flipping a baton to the maintainer -- but
# forum.coordination.projects.flip() (the write body api_project_flip calls)
# documents that validation as "the caller's job and is NOT performed here."
# A client that talks to this REST endpoint directly (not through
# tools/baton.py) bypassed every one of those checks silently. This section
# mirrors cmd_flip's own gate at the route layer, sourced from the same
# tools/baton_gate.py module tools/baton.py itself now imports from -- so
# the two enforcement points can't drift apart on what "green" or
# "maintainer-role flip" means.
# ---------------------------------------------------------------------------

# Cache: [module] on success, [] on not-yet-attempted, [None] on a
# confirmed-failed load. A one-element-list sentinel (matching
# tools/baton.py's own _POOL_SENTINEL_CACHE pattern) so "attempted and
# failed" is distinguishable from "not attempted yet" without a second flag.
_BATON_GATE_MODULE_CACHE: list = []


def _load_baton_gate():
    """Load tools/baton_gate.py's pure gate helpers; cache the outcome.

    Returns the loaded module, or None if it could not be loaded (missing
    file, import error, or any other failure). Callers MUST treat None as
    "gate unavailable" and fail closed for any flip that could have been
    gated -- never silently proceed as if the gate had run and passed.

    Loaded by DIRECT FILE PATH (importlib.util.spec_from_file_location),
    not `import baton_gate` / `from tools.baton_gate import ...` -- this
    sidesteps two hazards a dotted import would carry:
      1. Deployment: the installer's rsync step snapshots src/forum/ and
         tools/engram-pkg/ into the live service directory -- tools/ itself
         (and therefore a `tools.baton_gate` dotted import) is not part of
         that snapshot, so a package-relative import would reliably fail
         there. A direct-file-path load finds the module where the installer
         now places it, $APP/tools/baton_gate.py (a `cp -L` of the resolved
         file, mirroring how tools/engram-pkg/ is sibling-deployed under
         $APP/tools/).
      2. Test/dev sys.path collision: a bare `import tools.baton_gate`
         resolves through sys.modules['tools'], which other test files in
         this repo have been shown to silently poison with an unrelated
         decoy package under certain sys.path orderings (see
         tests/test_sys_path_collision_guard_2565.py). A direct file load
         has no package-name collision surface at all.
    """
    if _BATON_GATE_MODULE_CACHE:
        return _BATON_GATE_MODULE_CACHE[0]

    try:
        import importlib.util

        # Walk-parents search, NOT a fixed parents[N] index (CLAUDE.md:
        # "Plugin build restructures code paths" -- a fixed index calibrated
        # against one layout silently overshoots in another, resolving to
        # *a* path rather than the right one, or none at all). This walks
        # up from src/forum/server.py's own location checking a couple of
        # relative shapes at each level, so it finds the file whether this
        # is the source-tree layout (repo_root/tools/baton_gate.py, a
        # symlink to repo_root/src/engram/tools/baton_gate.py, found two
        # levels up) or a hypothetical future deployed layout mirroring
        # tools/engram-pkg/'s existing $APP/tools/ sibling-of-$APP shape
        # (found one level up). Bounded to 6 levels -- comfortably covers
        # both known layouts without risking an unrelated same-named file
        # on a shared host far up the tree.
        here = Path(__file__).resolve()
        candidate = None
        for parent in list(here.parents)[:6]:
            for rel in ("tools/baton_gate.py", "src/engram/tools/baton_gate.py"):
                p = parent / rel
                if p.is_file():
                    candidate = p
                    break
            if candidate is not None:
                break
        if candidate is None:
            _BATON_GATE_MODULE_CACHE.append(None)
            return None

        spec = importlib.util.spec_from_file_location(
            "_forum_baton_gate", candidate
        )
        if spec is None or spec.loader is None:
            _BATON_GATE_MODULE_CACHE.append(None)
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        # Any failure at all (ImportError, OSError, a syntax error in a
        # tampered file, ...) -> gate unavailable. Caller fails closed.
        _BATON_GATE_MODULE_CACHE.append(None)
        return None

    _BATON_GATE_MODULE_CACHE.append(module)
    return module


def _login_map_cfg(app) -> dict:
    """The strict-gate map configuration, read from the app at REQUEST time.

    ``path`` is None unless the server was started with --baton-login-map /
    $ENGRAM_BATON_LOGIN_MAP (there is no default path); ``admin_uids`` are the
    uids, besides root, allowed to own the map file (never defaulted to the
    server's own uid); ``trust_anchor`` is "/" in production (tests root it at
    a scratch dir).
    """
    return {
        "path": app.config.get("BATON_LOGIN_MAP"),
        "admin_uids": tuple(app.config.get("BATON_LOGIN_MAP_ADMIN_UIDS") or ()),
        "trust_anchor": app.config.get("BATON_LOGIN_MAP_TRUST_ANCHOR") or "/",
    }


# Per-login verdicts that refuse an advance even when ANOTHER mapped login of the
# same colleague is `approved_current` (R4).
_APPROVAL_BLOCKERS = frozenset({"changes_requested", "ambiguous", "unknown", "incomplete"})


def _duplicate_key_refusal(raw: Optional[str]) -> Optional[tuple]:
    """``(message, 422)`` when ``raw``'s frontmatter repeats a key the gates trust.

    ``parse_frontmatter`` lets the LAST duplicate win, so a repeated ``phase`` /
    ``turn`` / ``github`` / ``colleague`` / ``author`` / ``status`` /
    ``participants`` line changes what every gate reads without any gate having
    approved it. A file in that state is corrupt (or was written before the
    sanitiser existed): refuse to make an authority decision from it and say
    which key. ``parse_frontmatter`` itself keeps its last-wins semantics.
    """
    if raw is None:
        return None
    dups = _md.guarded_duplicate_keys(raw)
    if dups:
        return (str(_md.DuplicateFrontmatterKeys(dups)), 422)
    return None


def _advance_gate(gate, pid: str, fields: dict, body: str, to_agent: str,
                  login_map_cfg: Optional[dict], *, raw: Optional[str] = None):
    """The ONE strict gate for the edge ``phase: peer -> phase: merge`` (#3039 U2b).

    ``raw`` is the baton file text ``fields``/``body`` were parsed from: when
    given, a duplicated guarded frontmatter key refuses first (round 4, R2).
    Both callers pass it.

    Called by BOTH spellings -- ``POST /api/projects/<pid>/advance`` and a
    ``POST .../flip`` that resolves as this transition -- so a refusal is
    identical (same text, same status) whichever door was used, and there is
    no skip path.

    Returns ``(refusal, ticket)``: ``refusal`` is ``(message, status)`` or
    None; ``ticket`` (on pass) carries what the check-and-set write must
    re-verify under the lock plus the two verdicts. ``ticket["noop"]`` is True
    when the baton is already at ``merge`` (nothing to check, nothing to write).

    Order: local checks first (anchor, phase, target, colleague, map), then CI
    (red/pending refuse; ``unknown`` stays ADVISORY and is reported as a
    warning), then the approval verdict (ONLY ``approved_current`` passes;
    ``unknown``/``incomplete`` refuse). The two verdicts are kept separate in
    the ticket and in the refusal text.
    """
    dup_refusal = _duplicate_key_refusal(raw)
    if dup_refusal is not None:
        return dup_refusal, None
    github_anchor = (fields.get("github") or "").strip().lower()
    parsed = gate._parse_github_anchor(github_anchor)
    if not github_anchor:
        # Round 6 (R-b): no anchor => no PR to verify. Refused here, before any
        # status/phase/gh work, naming the missing anchor.
        return (
            (f"'{pid}' has no github anchor, so there is no PR whose CI/approval the "
             "peer->merge advance can verify. The anchor can only be set while the baton is "
             "in 'fairy' (it is write-once afterwards); it cannot be added at peer/merge.", 422),
            None,
        )
    if parsed is None or parsed[0] != "pr":
        return (
            (f"'{pid}' is not a PR baton (its github anchor must be "
             "pr/<owner>/<repo>/<N>); the peer->merge advance applies only to PR batons.", 422),
            None,
        )
    pr_num, pr_repo = parsed[2], parsed[1]
    anchor_shown = None
    if pr_repo is None:
        # Round 6 (D1): DERIVE, never write. An unqualified ``pr/<N>`` is read as
        # ``pr/<repos[prefix]>/<N>`` -- the repo comes from the admin login map by
        # the baton id's prefix, never from the request -- so a legacy unqualified
        # peer baton can still advance without any anchor write. The number is
        # still bound to the id below. No map / unmapped prefix => refused, as ever.
        anchor_shown = github_anchor
        pr_repo = _bound_repo(gate, pid, login_map_cfg)
    if pr_repo is None:
        # Same refusal as the weak flip gate has always given (#1918/#2511).
        return (
            (f"'{pid}' has no repo-qualified github anchor -- refusing to "
             f"guess which repo's PR #{pr_num} to verify "
             "CI/approval/colleague state against. Run "
             f"'baton anchor {pid} --github pr/<owner>/<repo>/{pr_num}' "
             "first.", 422),
            None,
        )
    status = (fields.get("status") or "").strip().lower()
    if status in ("merged", "cancelled"):
        return ((f"'{pid}' is {status}; cannot advance a closed baton.", 422), None)

    try:
        cur_phase, _legacy = gate._resolve_phase(fields, is_bunch=False)
    except gate.PoolSentinelUnresolved as e:
        return ((f"cannot evaluate flip gate for {pid}: {e}", 422), None)
    if cur_phase == "merge":
        return (None, {"noop": True})
    if cur_phase not in ("peer", ""):
        return (
            (f"'{pid}' is in phase {cur_phase!r}, not 'peer'; the peer->merge "
             "advance applies only to a baton in the peer phase.", 409),
            None,
        )

    to_agent = (to_agent or "").strip().lower()
    current_holder = (fields.get("turn") or "").strip().lower()
    colleague_name = (fields.get("colleague") or "").strip().lower()
    author_name = (fields.get("author") or "").strip().lower() or gate._derive_legacy_author(body)
    participants = [
        p.strip().lower()
        for p in re.split(r"[,\[\]]", fields.get("participants", ""))
        if p.strip()
    ]
    try:
        is_sentinel = gate._is_legacy_pool_sentinel(to_agent)
        is_merge_target = gate._is_maintainer_flip(to_agent, current_holder, colleague_name, author_name)
    except gate.PoolSentinelUnresolved as e:
        return ((f"cannot evaluate flip gate for {pid}: {e}", 422), None)
    if not is_sentinel and to_agent not in participants:
        return (
            (f"'{to_agent}' is not a participant in {pid} (participants: {participants}); "
             "name the merge seat.", 422),
            None,
        )
    if not is_merge_target:
        return (
            (f"'{to_agent}' is not a merge-seat target for {pid}: it is the current "
             "holder, the colleague, or the author.", 422),
            None,
        )

    if not colleague_name:
        return (
            (f"{pid} has no colleague assigned, so there is no reviewer whose approval "
             "the peer->merge advance can check. Set one: "
             f"'baton set-colleague {pid} <agent>'.", 422),
            None,
        )
    cfg = login_map_cfg or {}
    try:
        login_map = gate._load_login_map(
            cfg.get("path"),
            extra_trusted_uids=cfg.get("admin_uids") or (),
            trust_anchor=cfg.get("trust_anchor") or "/",
        )
    except gate.LoginMapError as e:
        return ((str(e), 422), None)
    # A2 (round 4): the baton id binds the anchor's number AND repo -- checked
    # BEFORE any GitHub call, so a re-pointed or mis-anchored baton never even
    # causes a query against the wrong PR.
    binding_refusal = gate._anchor_id_binding_refusal(
        pid, pr_repo, pr_num, getattr(login_map, "repos", None), anchor_shown=anchor_shown)
    if binding_refusal is not None:
        return ((binding_refusal, 422), None)
    logins = login_map.get(colleague_name)
    if not logins:
        return (
            (f"colleague '{colleague_name}' has no entry in the baton login map, so their "
             "GitHub approval cannot be identified. The admin must add the mapping.", 422),
            None,
        )

    ci_state, ci_detail = gate._pr_ci_state(pr_num, repo=pr_repo)
    if ci_state in ("red", "pending"):
        return (
            (f"CI is {ci_state} on PR #{pr_num} ({ci_detail}) -- the current "
             "tip is not merge-ready. Re-flip when checks are green.", 422),
            None,
        )
    warnings = []
    if ci_state == "unknown":
        logger.warning(
            "advance gate: could not verify CI state for PR #%s (%s) -- proceeding anyway",
            pr_num, ci_detail,
        )
        warnings.append(f"could not verify CI state for PR #{pr_num} ({ci_detail}); CI is advisory here")

    # R4 (round 4): EVERY mapped login is read (no stopping at the first
    # approval). Pass = at least one `approved_current` AND no login's verdict is
    # a blocker -- `changes_requested` / `ambiguous` (an objection or an
    # unorderable pair of opinions) or `unknown` / `incomplete` (we could not
    # tell whether that login objects: unknown must not render as permitted).
    # `no_review` / `dismissed` / `stale_head` / `self_review` beside an
    # approving login are fine -- they hold no standing objection.
    verdicts = []
    approval = None
    for login in logins:
        state, detail = gate._reviewer_approval_state(pr_num, pr_repo, login)
        verdicts.append((login, state, detail))
        if state == "approved_current" and approval is None:
            approval = (state, detail)
    blockers = [v for v in verdicts if v[1] in _APPROVAL_BLOCKERS]
    if approval is None or blockers:
        shown = "; ".join(f"{login}: {state} ({detail})" for login, state, detail in verdicts)
        why = (
            "a mapped login of the colleague objects or cannot be read"
            if approval is not None else "no current approval"
        )
        return (
            (f"approval check refused on PR #{pr_num}: {why} by "
             f"{colleague_name}'s mapped GitHub account(s) ({shown}). Only an APPROVED review on the "
             "current head, still that account's latest opinionated review, with no other mapped "
             "login objecting, advances peer -> merge.", 422),
            None,
        )
    return (
        None,
        {
            "noop": False,
            "anchor": github_anchor,
            "colleague": colleague_name,
            "pr": pr_num,
            "repo": pr_repo,
            "ci": {"state": ci_state, "detail": ci_detail},
            "approval": {"state": approval[0], "detail": approval[1]},
            "warnings": warnings,
        },
    )


def _check_flip_gate(
    store,
    pid: str,
    to_agent: str,
    *,
    phase: Optional[str] = None,
    transition_phase: Optional[str] = None,
    login_map_cfg: Optional[dict] = None,
    ticket_out: Optional[dict] = None,
) -> Optional[tuple[str, int]]:
    """Server-side mirror of cmd_flip's CI/approval/colleague gate.

    Returns None when the flip may proceed (not PR-anchored, not a
    maintainer-role flip, or all checks passed). Returns (message, status)
    when the flip must be refused -- the caller returns
    jsonify({"error": message}), status and does NOT call flip().

    No --force equivalent: this is not an interactive CLI, and #2884's own
    framing ("unknown must not render as permitted") argues against adding
    a silent bypass knob to a REST caller.

    #3039 U2b (D1, one gate / two spellings): a flip that resolves as the
    ``peer -> merge`` transition is NOT judged by the weak gate below. It is
    handed to :func:`_advance_gate` -- the same function the ``advance`` route
    calls -- and, on pass, ``ticket_out`` is filled so the caller performs the
    check-and-set write instead of a plain flip. A flip "resolves as the
    transition" when it is a maintainer-role flip (or the client claims
    ``merge`` in ``phase`` / ``transition_phase``) AND the baton's phase,
    resolved server-side, is ``peer`` -- or is ``fairy``/legacy-unset on a
    baton that HAS a colleague (a colleague means a peer phase exists; it
    cannot be skipped by flipping straight to the merge seat). A baton with no
    colleague (single-agent mode) has no peer phase and keeps the unchanged
    weak gate; so does every flip within ``merge`` (author <-> merge seat
    ping-pong).
    """
    from forum.coordination.markdown import parse_frontmatter

    raw = store.read_project(pid)
    if raw is None:
        # Let the normal flip() call raise ProjectNotFound -> 404, unchanged.
        return None

    # R2 (round 4): never make a gate decision from a file whose guarded keys
    # are duplicated -- parse_frontmatter's last-wins would pick the value.
    dup_refusal = _duplicate_key_refusal(raw)
    if dup_refusal is not None:
        return dup_refusal

    fields, body = parse_frontmatter(raw)
    github_anchor = (fields.get("github") or "").strip().lower()

    # Cheap, module-independent pre-check: only a "pr/..." anchor can ever
    # be maintainer-role-gated (cmd_flip's _pr_anchor_match requires
    # _parsed_anchor[0] == "pr"). A "project/<N>" anchor, no anchor at all,
    # or anything else can never trigger the gate -- so we can safely
    # proceed WITHOUT the gate module in those cases, even if the module
    # failed to load. This keeps a broken/undeployed gate module from
    # blocking flips that were never gated in the first place, while still
    # failing closed for the one case that actually matters (see below).
    if not github_anchor.startswith("pr/"):
        if github_anchor:
            return None  # project/<N> or anything else: never maintainer-gated
        # Round 6 (R-b): NO anchor at all. A colleague-held PR baton moving to the
        # merge seat has no PR behind it for the weak path to verify (nothing to
        # parse, so it used to wave the flip through). Classify it exactly like the
        # anchored case below and hand it to the strict gate, whose first check
        # refuses a missing anchor. A baton with no colleague (single-agent mode,
        # no peer phase) and anything that is not PR-shaped are unchanged.
        gate = _load_baton_gate()
        if gate is None:
            return None
        colleague_name = (fields.get("colleague") or "").strip().lower()
        if not colleague_name or not gate._is_pr_baton(pid, fields):
            return None
        current_holder = (fields.get("turn") or "").strip().lower()
        author_name = (fields.get("author") or "").strip().lower() or gate._derive_legacy_author(body)
        claims_merge = "merge" in ((phase or "").lower(), (transition_phase or "").lower())
        try:
            if gate._is_maintainer_flip(to_agent, current_holder, colleague_name, author_name) or claims_merge:
                resolved_phase, _legacy = gate._resolve_phase(fields, is_bunch=False)
                if resolved_phase == "peer" or resolved_phase in ("", "fairy"):
                    refusal, _ticket = _advance_gate(
                        gate, pid, fields, body, to_agent, login_map_cfg, raw=raw)
                    return refusal
        except gate.PoolSentinelUnresolved as e:
            # Round 7 (F1): an unanchored baton with an EXPLICIT phase never needed
            # the sentinel -- unchanged, not a refusal for every flip. But a LEGACY
            # baton (blank phase line) whose flip cannot be classified would be
            # written phase-less with ``turn == sentinel``, which reads as the
            # legacy ``merge`` once the sentinel resolves again (no approval
            # anywhere). Fail closed: refuse, write nothing.
            if not (fields.get("phase") or "").strip():
                return (f"cannot evaluate flip gate for {pid}: {e}", 422)
            return None
        return None

    gate = _load_baton_gate()
    if gate is None:
        # We know this baton HAS a pr/... anchor (the prefix check above),
        # so it is at least POSSIBLY subject to the gate -- but without the
        # module we cannot parse the anchor or determine maintainer-role
        # status. Fail closed rather than silently proceed ungated.
        return (
            f"flip gate unavailable for {pid} (server could not load its "
            "CI/approval gate module) -- refusing to flip a PR-anchored "
            "baton without being able to verify CI/approval/colleague "
            "state. See server logs.",
            422,
        )

    parsed_anchor = gate._parse_github_anchor(github_anchor)
    pr_anchor_match = parsed_anchor is not None and parsed_anchor[0] == "pr"
    if not pr_anchor_match:
        # A "pr/" prefix that doesn't fully match the anchor grammar (e.g.
        # malformed) is not pr_anchor_match either, matching cmd_flip.
        return None

    current_holder = (fields.get("turn") or "").strip().lower()
    colleague_name = (fields.get("colleague") or "").strip().lower()
    # #2961 follow-up (Ariadne's round-2 catch): the live store has batons
    # created before the `author:` field existed -- fall back to deriving
    # it from the turn log's own immutable "initialized -> X:" first line
    # rather than a bulk migration write to every existing file (see
    # gate._derive_legacy_author's own docstring for why that's the
    # safer choice: a migration would race the running forum process's
    # own concurrent writes to the same files).
    author_name = (fields.get("author") or "").strip().lower() or gate._derive_legacy_author(body)

    try:
        is_maintainer_flip = gate._is_maintainer_flip(to_agent, current_holder, colleague_name, author_name)
    except gate.PoolSentinelUnresolved as e:
        return (f"cannot evaluate flip gate for {pid}: {e}", 422)

    # #3039 U2b (D1): does this flip resolve as the peer -> merge transition?
    # Decided from the SERVER-resolved phase, never from the client's claim.
    #
    # PARITY (round 4, R6): this decision and `_flip_phase_inputs`'s "is this the
    # merge transition?" decision MUST classify the flip identically -- when the
    # weak path there would write a `merge` transition from fairy / legacy-unset
    # on a baton that HAS a colleague, THIS function must already have sent the
    # flip down the strict path. test_baton_gate_parity.py drives both over a grid;
    # change one, change the other.
    claims_merge = "merge" in ((phase or "").lower(), (transition_phase or "").lower())
    if is_maintainer_flip or claims_merge:
        try:
            resolved_phase, _legacy = gate._resolve_phase(fields, is_bunch=False)
        except gate.PoolSentinelUnresolved as e:
            return (f"cannot evaluate flip gate for {pid}: {e}", 422)
        if resolved_phase == "peer" or (resolved_phase in ("", "fairy") and colleague_name):
            refusal, ticket = _advance_gate(gate, pid, fields, body, to_agent, login_map_cfg, raw=raw)
            if refusal is not None:
                return refusal
            if ticket_out is not None and ticket:
                ticket_out.update(ticket)
            return None

    if not is_maintainer_flip:
        # Self-flip or flip-to-the-registered-colleague -- not a
        # "presenting for merge" signal; cmd_flip does not gate these either.
        return None

    if parsed_anchor[1] is None:
        # #1918/#2511: unqualified pr/<N> anchor -- there is no default repo
        # to guess against, matching cmd_flip's own refusal exactly.
        return (
            f"'{pid}' has no repo-qualified github anchor -- refusing to "
            f"guess which repo's PR #{parsed_anchor[2]} to verify "
            "CI/approval/colleague state against. Run "
            f"'baton anchor {pid} --github pr/<owner>/<repo>/{parsed_anchor[2]}' "
            "first.",
            422,
        )

    pr_num = parsed_anchor[2]
    pr_repo = parsed_anchor[1]

    ci_state, ci_detail = gate._pr_ci_state(pr_num, repo=pr_repo)
    if ci_state in ("red", "pending"):
        return (
            f"CI is {ci_state} on PR #{pr_num} ({ci_detail}) -- the current "
            "tip is not merge-ready. Re-flip when checks are green.",
            422,
        )
    if ci_state == "unknown":
        logger.warning(
            "flip gate: could not verify CI state for PR #%s (%s) — proceeding anyway",
            pr_num, ci_detail,
        )
    # green: proceed silently

    approval_state, approval_detail = gate._pr_approval_state(pr_num, repo=pr_repo)
    if approval_state == "stale":
        return (
            f"tip moved after approval on PR #{pr_num} ({approval_detail}). "
            "The approved commit is not the commit the maintainer would "
            "merge — request a re-review of the new tip.",
            422,
        )
    if approval_state == "no_approval":
        participants = [
            p.strip().lower()
            for p in re.split(r"[,\[\]]", fields.get("participants", ""))
            if p.strip()
        ]
        try:
            sentinel = gate._resolve_pool_sentinel()
        except gate.PoolSentinelUnresolved as e:
            return (f"cannot evaluate flip gate for {pid}: {e}", 422)
        colleague_participants = [
            p for p in participants if p != current_holder and p != sentinel
        ]
        if colleague_participants:
            return (
                f"no colleague approval on PR #{pr_num}. A non-author "
                "colleague must review and approve this PR before it goes "
                "to the maintainer.",
                422,
            )
        # Single-agent mode: no counterpart to perform the review — warn
        # and proceed, matching cmd_flip.
        logger.warning(
            "flip gate: no colleague approval on PR #%s (no colleague "
            "participant — single-agent mode; skipping colleague gate)",
            pr_num,
        )
    elif approval_state == "unknown":
        logger.warning(
            "flip gate: could not verify post-approval state for PR #%s (%s) — proceeding anyway",
            pr_num, approval_detail,
        )
    # fresh: proceed silently

    return None


def _perform_advance(gate, store, allocator, pid: str, *, to_agent: str, reason: str,
                     agent: str, ticket: dict) -> tuple[dict, int]:
    """Run the check-and-set write for a gate-passed (or already-merge) advance.

    Shared by the ``advance`` route and the flip spelling. Returns
    ``(json_body, status)``: 201 advanced, 200 idempotent no-op, 404 gone,
    409 the baton moved between the GitHub check and the write.
    """
    from forum.coordination import AdvanceRefused, AdvanceResult, ProjectNotFound, advance as _advance

    def resolve_phase(fields: dict) -> str:
        try:
            phase, _legacy = gate._resolve_phase(fields, is_bunch=False)
        except gate.PoolSentinelUnresolved as e:
            raise AdvanceRefused(f"cannot resolve the baton's phase: {e}")
        return phase

    if ticket.get("noop"):
        # The gate saw the baton already at `merge`: an idempotent no-op. Nothing
        # is written, so no seq is consumed and nothing wakes anyone.
        from forum.coordination.markdown import parse_frontmatter
        raw = store.read_project(pid)
        if raw is None:
            return {"error": f"project not found: {pid}"}, 404
        fields, _ = parse_frontmatter(raw)
        try:
            seq = int((fields.get("seq") or "0").strip())
        except ValueError:
            seq = 0
        result = AdvanceResult(False, seq, "already merge")
    else:
        try:
            result = _advance(
                store, allocator, pid,
                to_agent=to_agent, reason=reason, by_agent=agent,
                expected_anchor=ticket.get("anchor", ""),
                expected_colleague=ticket.get("colleague", ""),
                resolve_phase=resolve_phase,
            )
        except ProjectNotFound:
            return {"error": f"project not found: {pid}"}, 404
        except AdvanceRefused as e:
            return {"error": str(e), "phase": e.phase}, 409

    turn, status = _read_project_state(store, pid)
    body = {
        "advanced": result.advanced,
        "seq": result.seq,
        "turn": turn,
        "status": status,
        "phase": "merge",
    }
    if not result.advanced:
        body["reason"] = result.reason
        return body, 200
    body["ci"] = ticket.get("ci")
    body["approval"] = ticket.get("approval")
    body["warnings"] = ticket.get("warnings", [])
    return body, 201


# ---------------------------------------------------------------------------
# #3039 U2b round 2: the server never trusts a client-supplied phase.
#
# Every write route used to hand the request body's ``phase`` (the legacy
# CONVERSION value) and ``transition_phase`` to its coordination write-body
# verbatim, so any client could write ``phase: merge`` with no approval
# anywhere -- a bypass of the strict peer -> merge gate (``_advance_gate``).
# Now:
#   * conversion: the server derives the value from the baton FILE
#     (``gate._resolve_phase``, with its own PR-vs-bunch decision) and uses
#     THAT. A differing client value is ignored (warning logged); a client
#     ``merge`` the server does not itself resolve is a 422.
#   * transition: an allow-list per route (what the CLI legitimately sends).
#     ``merge`` is accepted from no request body; the only writers of
#     ``phase: merge`` are the strict gate (``projects.advance``), a flip the
#     SERVER itself classifies as the weak-gated maintainer flip (see
#     ``_flip_phase_inputs``), and a legacy conversion whose value the server
#     read off the file.
# ---------------------------------------------------------------------------

_INIT_PHASES_PR = frozenset({"fairy"})
_INIT_PHASES_BUNCH = frozenset({"pool"})
_CLAIM_PHASES = frozenset({"claimed"})
_RELEASE_PHASES = frozenset({"pool", "done"})


def _phase_refusal(route: str, pid: str, field: str, value: str, why: str) -> tuple[str, int]:
    return (
        f"{route} {pid}: {field} {value!r} is not accepted from a request body ({why}). "
        "The server derives conversion phases itself and accepts no 'merge' "
        "from a request body.",
        422,
    )


# ---------------------------------------------------------------------------
# #3039 U2b round 4: ONE shared reader for every baton write route's request body.
#
# Every client string a write-body puts into a baton file used to be only
# ``.strip()``-ped, so an INTERIOR line break survived into the frontmatter and
# (last duplicate key wins) could write ``phase: merge``. Now every route reads
# its fields through ``_read_fields`` and nothing else, so the policy lives in one
# place and a route that reads a field around it is caught by a test:
#
#   "text"  FREE-TEXT (reason, turn_reason, title, new_title): every run of
#           line-break / control characters becomes ONE space (agents do send
#           multi-line reasons); stored on one line. Quotes/backslashes untouched.
#   "id"    IDENTIFIER (github, project id, status, ...): any line-break / control
#           character => 422 naming the route and field. Case kept.
#   "idl"   the same, lower-cased (agent / turn / phase names).
#   "bool"  a real JSON boolean.
#   "list"  a list of identifiers or a comma-separated string (participants).
#
# A non-string where a string is expected => 422 naming the route and field (no
# more AttributeError -> 500). Absent / null stays "absent" ("" / False / []).
# Nothing here touches the lock, the seq or the file: a refusal is free.
# ---------------------------------------------------------------------------

class _RequestRefusal(Exception):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _field_refusal(route: str, field: str, why: str) -> _RequestRefusal:
    return _RequestRefusal(f"{route}: field '{field}' {why}")


_NO_CONTROL = "must not contain line-break or control characters (it is an identifier, not free text)"
# #3294: json.loads accepts the escape "\\ud800" and yields a lone surrogate, which the
# store's UTF-8 encode then rejects at write time (a 500 and a burned seq).
_NOT_UTF8 = "must be valid UTF-8 text (it contains a lone surrogate or another character that cannot be encoded)"


def _require_utf8(route: str, field: str, raw: str) -> None:
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        raise _field_refusal(route, field, _NOT_UTF8) from None


def _require_str(route: str, field: str, raw) -> None:
    """#3436: a free-text OR identifier field (body_md, note, ..., and `agent` / `state`)
    must be a string (absent / null stays "absent").  Same
    422 and message shape as _read_fields' typed-field error, so a number / list / object
    is refused here instead of reaching .strip() or a store binding (500) -- or, for
    pair_initials, str() (silently stored as the value's repr)."""
    if raw is not None and not isinstance(raw, str):
        raise _field_refusal(route, field, f"must be a string (got {type(raw).__name__})")


_INT_STR = re.compile(r"-?[0-9]+")


def _require_int(route: str, field: str, raw) -> None:
    """#3436 round 3: an integer-id field (thread_id, post_id, last_read_post_id) must be a
    real int (not a bool: True is an int subclass, and int(True) == 1 wrote to thread 1) or
    a string of ASCII digits with an optional leading minus (int("5") is accepted today and
    clients may rely on it).  absent / null keeps its meaning.  A float (even 1.0), any other
    string, a list and a dict are a 422, same shape as _require_str.  The caller keeps its own
    int() parse after this guard.

    #3438: a value outside SQLite's signed 64-bit range (int or digit string) is a 422 too --
    it used to overflow at the sqlite binding (a 500, on /read after upsert_agent)."""
    if raw is None:
        return
    if isinstance(raw, int) and not isinstance(raw, bool):
        _require_int64(route, field, raw)
        return
    if isinstance(raw, str) and _INT_STR.fullmatch(raw):
        # Leading zeros are stripped before int(): int() refuses a string of more than
        # 4300 digits (a ValueError) and "0"*5000+"5" is the in-range value 5, which the
        # caller's own parse still answers as before.
        digits = raw.lstrip("-").lstrip("0")
        if len(digits) > 25:  # far outside 64 bits; never parse or echo it
            raise _field_refusal(route, field, f"{_INT64_MSG} (got a {len(digits)}-digit integer)")
        _require_int64(route, field, int(digits or "0") * (-1 if raw.startswith("-") else 1))
        return
    raise _field_refusal(route, field, f"must be an integer (got {type(raw).__name__})")


_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1
_INT64_MSG = "must be an integer within the 64-bit range"


def _require_int64(route: str, field: str, n: int) -> None:
    """#3438: refuse an int outside SQLite's signed 64-bit range (a 422 before any read or
    write; the binding would raise OverflowError).  The value is echoed only when short."""
    if _INT64_MIN <= n <= _INT64_MAX:
        return
    text = str(n)
    got = text if len(text) <= 25 else f"a {len(text.lstrip('-'))}-digit integer"
    raise _field_refusal(route, field, f"{_INT64_MSG} (got {got})")


def _require_utf8_deep(route: str, field: str, value) -> None:
    """_require_utf8 over an arbitrary JSON value: strings, and list items / dict keys /
    dict values at any depth.  (#3318 review: a queue item may be a list or dict, and
    json.dumps would store a nested surrogate as an escape that decodes back to it.)
    Iterative on purpose: json.loads accepts nesting far past the recursion limit, so a
    recursive walk would turn a deeply nested valid value into a 500 (#3318 round 3)."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            _require_utf8(route, field, item)
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())


def _read_ident(route: str, field: str, raw, *, lower: bool) -> str:
    if not isinstance(raw, str):
        raise _field_refusal(route, field, f"must be a string (got {type(raw).__name__})")
    _require_utf8(route, field, raw)
    if _md.has_break_or_control(raw):
        raise _field_refusal(route, field, _NO_CONTROL)
    value = raw.strip()
    return value.lower() if lower else value


def _read_fields(route: str, data, spec: dict, *, pid: Optional[str] = None) -> dict:
    """Read ``spec`` ({field: kind}) from the JSON body ``data`` -- see the block above."""
    if pid is not None and _md.has_break_or_control(pid):
        raise _field_refusal(route, "project_id", _NO_CONTROL)
    if not isinstance(data, dict):
        raise _RequestRefusal("JSON body required", 400)
    out: dict = {}
    for field, kind in spec.items():
        raw = data.get(field)
        if kind == "bool":
            if raw is None:
                out[field] = False
            elif isinstance(raw, bool):
                out[field] = raw
            else:
                raise _field_refusal(route, field, f"must be a boolean (got {type(raw).__name__})")
        elif kind == "list":
            if raw is None:
                out[field] = []
            elif isinstance(raw, str):
                out[field] = [
                    _read_ident(route, field, p, lower=True)
                    for p in raw.split(",") if p.strip()
                ]
            elif isinstance(raw, list):
                items = [_read_ident(route, field, p, lower=True) for p in raw if p != ""]
                out[field] = [p for p in items if p]
            else:
                raise _field_refusal(
                    route, field, f"must be a list of strings or a comma-separated string (got {type(raw).__name__})")
        elif raw is None:
            out[field] = ""
        elif kind == "text":
            if not isinstance(raw, str):
                raise _field_refusal(route, field, f"must be a string (got {type(raw).__name__})")
            _require_utf8(route, field, raw)
            out[field] = _md.normalize_free_text(raw)
        elif kind in ("id", "idl"):
            out[field] = _read_ident(route, field, raw, lower=(kind == "idl"))
        else:  # a typo in a route's spec must be loud, not silently permissive
            raise ValueError(f"unknown field kind {kind!r} for {field!r}")
    return out


def _baton_fields(store, pid: str):
    """(fields, body) of an existing baton, or (None, None) when absent.

    Raises :class:`~forum.coordination.markdown.DuplicateFrontmatterKeys` (mapped to
    a 422 by the app's error handler) when the file repeats a guarded key -- every
    caller here resolves phase / turn / github / colleague to make an authority
    decision (R2, round 4).
    """
    from forum.coordination.markdown import parse_frontmatter
    raw = store.read_project(pid)
    if raw is None:
        return None, None
    dups = _md.guarded_duplicate_keys(raw)
    if dups:
        raise _md.DuplicateFrontmatterKeys(dups)
    return parse_frontmatter(raw)


def _resolved_phase(gate, pid: str, fields: dict) -> str:
    """The baton's phase as the SERVER reads it (``""`` = indeterminate)."""
    try:
        phase, _legacy = gate._resolve_phase(fields, is_bunch=not gate._is_pr_baton(pid, fields))
    except gate.PoolSentinelUnresolved:
        return ""
    return phase


def _server_conversion(route: str, pid: str, client_phase: Optional[str], fields: Optional[dict],
                       gate, *, fallback: Optional[str] = None):
    """C1: the conversion phase to hand a write-body, plus a refusal or None.

    ``fields`` is None when the baton does not exist (the write-body then
    raises ProjectNotFound -> 404; nothing to derive). Returns
    ``(server_phase, refusal)``. ``fallback`` is the route's known pre-state
    when the file resolves nothing (release: ``claimed``), exactly as the CLI
    applies it -- a server-side constant, never read from the request.
    """
    if fields is None:
        return None, None
    server_phase = None
    if gate is not None:
        server_phase = (_resolved_phase(gate, pid, fields) or fallback or None)
    if client_phase and client_phase != (server_phase or ""):
        if client_phase == "merge":
            return None, _phase_refusal(
                route, pid, "phase", client_phase,
                f"the baton's server-resolved phase is {server_phase or 'unresolved'!r}")
        logger.warning(
            "%s %s: ignoring client conversion phase %r; using server-derived %r",
            route, pid, client_phase, server_phase)
    return server_phase, None


def _bunch_verb_inputs(route: str, pid: str, store, client_phase: Optional[str],
                       client_transition: Optional[str], allowed: frozenset, *,
                       fallback: Optional[str] = None):
    """claim / release: C3 (bunch verbs only) + C2 allow-list + C1 conversion.

    Returns ``(phase, transition_phase, refusal)``.
    """
    fields, _body = _baton_fields(store, pid)
    if fields is None:
        return None, None, None  # the write-body raises ProjectNotFound -> 404
    gate = _load_baton_gate()
    if gate is None:
        return None, None, (
            f"{route} {pid}: phase gate unavailable (server could not load its gate module) "
            "-- refusing to decide PR-vs-bunch. See server logs.", 422)
    if gate._is_pr_baton(pid, fields):
        return None, None, (
            f"{route} {pid}: claim/release are bunch verbs and {pid} is a PR baton "
            "(its phase machine is fairy -> peer -> merge).", 422)
    if client_transition is not None and client_transition not in allowed:
        return None, None, _phase_refusal(
            route, pid, "transition_phase", client_transition,
            "allowed here: " + ", ".join(sorted(allowed)))
    phase, refusal = _server_conversion(route, pid, client_phase, fields, gate, fallback=fallback)
    if refusal is not None:
        return None, None, refusal
    return phase, client_transition, None


def _flip_phase_inputs(route: str, pid: str, store, to_agent: str,
                       client_phase: Optional[str], client_transition: Optional[str]):
    """flip: the phases to write, decided by the SERVER from the baton.

    The server classifies the flip itself the way ``cmd_flip`` does:
      * maintainer-role flip on a ``pr/`` baton whose gate has just passed
        (the strict path already returned via ``_perform_advance``; what is
        left is the unchanged weak gate -- phase ``merge``/``fairy``/legacy
        on a baton WITHOUT a colleague, or a flip within ``merge``)
        => expected transition ``merge``;
      * author -> registered colleague while phase is fairy/legacy
        => expected transition ``peer``;
      * anything else => no transition.
    A client ``transition_phase`` other than the expected one is a 422.
    Returns ``(phase, transition_phase, refusal)``.

    PARITY (round 4, R6): "is this the merge transition?" here and in
    :func:`_check_flip_gate` ("does this flip go down the strict peer -> merge
    path?") MUST classify a flip identically. This function is only reached when
    the strict path did NOT take the flip; the ``merge`` it expects from
    ``fairy`` / legacy-unset exists for a baton with NO colleague (single-agent
    mode) and for a flip within ``merge`` -- with a colleague, ``_check_flip_gate``
    must already have sent those flips down the strict path.
    ``test_baton_gate_parity.py`` drives both over a grid and fails on drift.
    """
    fields, body = _baton_fields(store, pid)
    if fields is None:
        return None, None, None  # ProjectNotFound -> 404 in the write-body
    gate = _load_baton_gate()
    if gate is None:
        # Cannot classify: a client transition cannot be verified, conversion is skipped.
        if client_transition is not None or client_phase == "merge":
            return None, None, (
                f"{route} {pid}: phase gate unavailable (server could not load its "
                "gate module) -- refusing a client-supplied phase. See server logs.", 422)
        return None, None, None
    resolved = _resolved_phase(gate, pid, fields)
    is_pr = gate._is_pr_baton(pid, fields)
    colleague = (fields.get("colleague") or "").strip().lower()
    holder = (fields.get("turn") or "").strip().lower()
    author = (fields.get("author") or "").strip().lower() or gate._derive_legacy_author(body)
    parsed = gate._parse_github_anchor((fields.get("github") or "").strip().lower())
    pr_anchor = parsed is not None and parsed[0] == "pr"
    try:
        is_maintainer = gate._is_maintainer_flip(to_agent, holder, colleague, author)
    except gate.PoolSentinelUnresolved:
        is_maintainer = None  # cannot classify => no transition is verifiable

    expected: Optional[str] = None
    fallback: Optional[str] = None
    if is_maintainer is None:
        pass
    elif pr_anchor and is_maintainer:
        if resolved in ("merge", "fairy", ""):
            expected, fallback = "merge", "peer"
    elif is_pr and colleague and to_agent == colleague and resolved in ("fairy", ""):
        expected, fallback = "peer", "fairy"
    elif is_pr and not pr_anchor and resolved == "":
        # Round 5 (B1): a flip on a PR baton with no ``pr/`` anchor is not gated
        # (``_check_flip_gate`` returns early: only a ``pr/`` anchor can be
        # maintainer-gated) and writes no transition. If the baton is a legacy
        # PHASE-LESS file, flipping it to the pool sentinel would leave
        # ``phase-less + turn == sentinel`` -- which reads as the legacy
        # ``merge``. So stamp an explicit ``fairy`` (conversion): an unanchored
        # baton can never reach ``merge`` without ``fairy -> peer -> advance``,
        # and ``advance`` needs a ``pr/`` anchor and an approval.
        fallback = "fairy"

    if client_transition is not None and client_transition != expected:
        return None, None, _phase_refusal(
            route, pid, "transition_phase", client_transition,
            f"the server classifies this flip's transition as {expected or 'none'!r}")
    server_phase = resolved or fallback or None
    if client_phase and client_phase != (server_phase or ""):
        if client_phase == "merge":
            return None, None, _phase_refusal(
                route, pid, "phase", client_phase,
                f"the baton's server-resolved phase is {server_phase or 'unresolved'!r}")
        logger.warning(
            "%s %s: ignoring client conversion phase %r; using server-derived %r",
            route, pid, client_phase, server_phase)
    return server_phase, client_transition, None


def _conversion_only_inputs(route: str, pid: str, store, client_phase: Optional[str]):
    """Routes that write no transition (close/reopen/gc/merge/rename/anchor/
    participants/colleague): the server-derived conversion, or a refusal."""
    fields, _body = _baton_fields(store, pid)
    phase, refusal = _server_conversion(route, pid, client_phase, fields, _load_baton_gate())
    return phase, refusal


def _colleague_write_refusal(route: str, pid: str, store) -> Optional[tuple]:
    """``(message, status)`` when a ``POST .../colleague`` must be refused, else None.

    #3296 (part of #3039). The colleague can be neither SET nor CHANGED (an
    identical re-send is a write too) once a PR baton is at ``merge``: a baton with
    no colleague can reach ``merge`` through the weak flip (the CLI sends
    ``transition_phase=merge`` on the maintainer-flip path), and a colleague added
    afterwards would make a colleague-bearing baton at ``merge`` that never passed
    the strict peer -> merge gate. The phase is the SERVER's reading of the file
    (``gate._resolve_phase``, the same resolution ``_anchor_refusal`` uses), never
    the request's. A PR baton whose phase cannot be resolved (phase-less file with an
    unresolvable pool sentinel) fails CLOSED. Bunches are never refused here.
    A missing baton returns None (the write-body's 404).
    """
    gate = _load_baton_gate()
    fields, _body = _baton_fields(store, pid)
    if fields is None:
        return None
    if gate is None:
        return (
            f"{route} {pid}: phase gate unavailable (server could not load its gate module) "
            "-- refusing to write a colleague without being able to verify the baton's "
            "phase. See server logs.", 422)
    if not gate._is_pr_baton(pid, fields):
        return None
    try:
        resolved, _legacy = gate._resolve_phase(fields, is_bunch=False)
    except gate.PoolSentinelUnresolved as e:
        return (f"{route} {pid}: cannot resolve the baton's phase to decide whether its "
                f"colleague may be written ({e}); the pool sentinel is unresolvable, and a "
                "baton that may be at 'merge' takes no colleague write until it resolves.", 422)
    if resolved == "merge":
        return (f"{route} {pid}: the baton is at phase {resolved!r}; the colleague cannot be "
                "set or changed once the baton is at 'merge' (not a first set, not a change, "
                "not an identical re-send). 'merge' is reached through the strict peer -> merge "
                "gate with the colleague already on the baton.", 422)
    return None


def _anchor_refusal(route: str, pid: str, store, new_anchor: str,
                    login_map_cfg: Optional[dict] = None) -> Optional[tuple]:
    """``(message, status)`` when the anchor write must be refused, else None.

    #3039 U2b round 4 + round 6. Two rules for ``POST .../anchor``:

    A1 -- WRITE-ONCE after ``fairy``, with NO EXCEPTIONS (round 6, the merge
    seat's ruling). On a baton whose SERVER-resolved phase is ``peer`` or
    ``merge`` -- or whose phase cannot be resolved (an unresolvable pool sentinel
    on a phase-less baton: it may be past ``fairy``, so it fails closed) -- a
    request may not write the anchor in ANY case: not when none exists, not to
    complete an unqualified ``pr/<N>``, not to re-send the identical value (that
    is a write: a row and a seq), not to change it. The approval the strict gate
    checks is the approval on THIS anchor's PR, so a past-``fairy`` request may
    never choose which PR that is. The repo of an unqualified anchor is DERIVED
    by the gate from the baton id's prefix via the admin ``repos`` table
    (``_advance_gate``), never written by a request. The phase is read from the
    file (``gate._resolve_phase``), never from the request. ``fairy`` /
    legacy-unset batons and bunches may still be anchored and re-anchored.

    A3 -- the value must parse the way ``baton anchor`` (the CLI) accepts it:
    ``pr/<N>``, ``pr/<owner>/<repo>/<N>`` or ``project/<N>`` (reusing the gate's
    own regex, so server and CLI cannot drift). Line-break/control characters were
    already refused by the shared reader.
    """
    gate = _load_baton_gate()
    fields, _body = _baton_fields(store, pid)  # a duplicated guarded key => 422
    new_norm = new_anchor.strip().lower()
    current = (fields.get("github") or "").strip().lower() if fields is not None else ""
    named = (f"its current anchor is {current!r}" if current
             else "it has no github anchor")

    if gate is None:
        return (
            f"{route} {pid}: phase gate unavailable (server could not load its gate module) "
            "-- refusing to write a github anchor without being able to verify its shape "
            "and the baton's phase. See server logs.", 422)

    if fields is not None:
        is_pr = gate._is_pr_baton(pid, fields)
        unresolved = None
        resolved = ""
        try:
            resolved, _legacy = gate._resolve_phase(fields, is_bunch=not is_pr)
        except gate.PoolSentinelUnresolved as e:
            unresolved = e
        if unresolved is not None:
            return (f"{route} {pid}: cannot resolve the baton's phase to decide whether its "
                    f"github anchor may be written ({unresolved}); {named}. Until the pool "
                    "sentinel resolves, a baton that may be past 'fairy' is refused every anchor write.",
                    422)
        if resolved in ("peer", "merge"):
            return (
                f"{route} {pid}: the github anchor is write-once once a baton leaves 'fairy' "
                f"(phase {resolved!r}) and takes NO writes at all -- not a first anchor, not a "
                f"completion, not an identical re-send, not a change; {named}; refusing {new_norm!r}. "
                "A re-pointed anchor would let another PR's approval stand in for this one's; "
                "an unqualified pr/<N> is read by the gate as the repo the id's prefix maps to "
                "in the admin login map, so it needs no completion.", 422)

    if not gate._GITHUB_ANCHOR_RE.match(new_norm):
        return (
            f"{route} {pid}: invalid github anchor {new_anchor!r}. Accepted forms: "
            f"{gate.GITHUB_ANCHOR_ACCEPTED_FORMS} (e.g. pr/490, pr/example-org/example-repo/22, "
            "project/4).", 422)

    # Round 9 (B1): id-bind the anchor where it is WRITTEN (fairy / legacy / bunch
    # re-anchored to a PR), not only at advance. A missing baton is the caller's 404.
    if fields is not None:
        is_pr = gate._is_pr_baton(pid, fields) or gate._is_pr_shaped(pid, new_norm)
        bind_msg = _anchor_write_binding_refusal(gate, pid, is_pr, new_norm, login_map_cfg)
        if bind_msg is not None:
            return (f"{route} {pid}: {bind_msg}", 422)
    return None


def _bound_repo(gate, pid: str, login_map_cfg: Optional[dict]) -> Optional[str]:
    """The ``owner/repo`` (lower-case) the baton id's prefix maps to in the admin
    ``repos`` table, or None when that cannot be known: no map configured / map
    unreadable or untrusted / the id carries no ``<PREFIX>-<N>`` / the prefix is
    not in the table. None keeps the narrow pre-round-5 behaviour (state it: with
    no table there is nothing to check a repo against)."""
    m = gate._ID_BIND_RE.fullmatch(pid or "")
    cfg = login_map_cfg or {}
    if m is None or not cfg.get("path"):
        return None
    try:
        login_map = gate._load_login_map(
            cfg.get("path"),
            extra_trusted_uids=cfg.get("admin_uids") or (),
            trust_anchor=cfg.get("trust_anchor") or "/",
        )
    except gate.LoginMapError:
        return None
    return (getattr(login_map, "repos", None) or {}).get(m.group("prefix").upper())


_PR_ISH_ID_RE = re.compile(r"(?i)^(?:[A-Z][A-Z0-9]*-)?PR-")
_ANCHOR_SHAPE_RE = re.compile(r"(?:pr/(?:[\w.-]+/[\w.-]+/)?|project/)[0-9]{1,15}")


def _anchorless_pr_refusal(route: str, project_id: str, given: str) -> str:
    """#3366: refusal text for a PR-shaped id whose github anchor is absent or does
    not parse. The two-route sentence is ``baton_gate.PR_ANCHOR_TWO_ROUTE_HINT``,
    shared VERBATIM with the CLI's #3364 refusal (Kepler: one wording, two surfaces);
    the lead-in is this surface's own (the API names a ``github`` field, the CLI a
    ``--github`` flag). The echoed value is bounded (<= 60 chars)."""
    if given:
        shown = given if len(given) <= 60 else given[:60] + "..."
        why = f"its github anchor {shown!r} is not a PR or project anchor"
    else:
        why = "no PR anchor was given (no github)"
    return (
        f"{route} {project_id}: refusing to create '{project_id}': its id is "
        f"PR-shaped but {why}, so it would be a PR baton nothing can verify at the "
        f"peer->merge gate. Pick the route that matches: {_gate_hint()}. "
        "(Send the anchor in the github field.)")


# Gate-unavailable fallback for ``baton_gate.GITHUB_ANCHOR_ACCEPTED_FORMS`` (the server
# does not import the gate there); pinned equal to the gate's constant by a test.
_ACCEPTED_FORMS_FALLBACK = "pr/<owner>/<repo>/<N>, pr/<N>, or project/<N>"


def _unparseable_github_refusal(route: str, project_id: str, given: str) -> str:
    """#3377: refusal text for a PRESENT ``github`` that does not parse, on an id that
    is NOT PR-shaped (a PR-shaped id gets :func:`_anchorless_pr_refusal`'s two-route
    text). The accepted forms are ``baton_gate.GITHUB_ANCHOR_ACCEPTED_FORMS``, shared
    with the CLI's ``--github`` refusal. The echoed value is bounded (<= 60 chars)."""
    gate = _load_baton_gate()
    forms = gate.GITHUB_ANCHOR_ACCEPTED_FORMS if gate is not None else _ACCEPTED_FORMS_FALLBACK
    shown = given if len(given) <= 60 else given[:60] + "..."
    return (
        f"{route} {project_id}: refusing to create '{project_id}': its github anchor "
        f"{shown!r} is not an accepted anchor, and a stored anchor nothing can parse "
        f"would be read as 'no anchor' by gc / flip / the board. Accepted forms: "
        f"{forms}. (Omit github for a baton with no anchor.)")


def _gate_hint() -> str:
    gate = _load_baton_gate()
    if gate is not None:
        return gate.PR_ANCHOR_TWO_ROUTE_HINT
    # Gate unavailable: the same sentence, only as the fail-closed fallback.
    return ("if it IS a PR, give the anchor pr/<owner>/<repo>/<N>; "
            "if it is NOT a PR, give project/<N> (a bunch) or use a non-PR-shaped id")


def _anchor_write_binding_refusal(gate, pid: str, is_pr: bool, anchor_norm: str,
                                  login_map_cfg: Optional[dict]) -> Optional[str]:
    """Refusal text when a ``pr/`` anchor being WRITTEN is not the one the baton id
    names, else None (#3039 U2b round 9, B1).

    The id binding used to be checked only at ``advance``; the anchor is
    write-once past ``fairy``, so a wrong anchor written while still in ``fairy``
    stranded the baton in ``peer``. This is the ONE check shared by the three
    write sites: init (qualified and unqualified) and ``POST /anchor`` in
    ``fairy`` / legacy. It reuses the gate's own binding (``_ID_BIND_RE``,
    ``_same_number``, ``_anchor_id_binding_refusal``) and :func:`_bound_repo`.

    Applies only to a PR-shaped baton (``is_pr``) and a ``pr/`` anchor whose id
    carries a number; ``project/<N>`` anchors, bunches and number-less ids are
    untouched. The NUMBER is always checked (it comes from the id). The REPO is
    checked only when the id's prefix is in the admin ``repos`` table; with no
    map / an unmapped prefix a qualified anchor with the right number is accepted
    as before (``advance`` still refuses an unmapped prefix). An unqualified
    ``pr/<N>`` is number-checked only (its repo is derived, never written)."""
    if gate is None:
        return None
    parsed = gate._parse_github_anchor(anchor_norm)
    if parsed is None:
        return None
    m = gate._ID_BIND_RE.fullmatch(pid or "")
    if parsed[0] == "project":
        # Round 10 (S-a): classification follows the ID SHAPE, not the anchor. A
        # numbered id whose prefix is in the admin ``repos`` table is PR-shaped and
        # takes only a binding ``pr/`` anchor, independent of ``is_pr`` (which is
        # anchor-derived): this makes the PR -> bunch -> PR round trip through a
        # ``project/<N>`` anchor unreachable. Unmapped prefix / no number / no map
        # => unchanged. Legacy files on disk are never touched by a read path.
        if m is not None and _bound_repo(gate, pid, login_map_cfg) is not None:
            return (
                f"the baton id {pid!r} is PR-shaped (its prefix is mapped to a repo in "
                f"the admin login map), so it takes only a pr/ anchor that matches the "
                f"id (pr/<owner>/<repo>/{m.group('num')} or pr/{m.group('num')}); "
                f"refusing {anchor_norm!r}.")
        return None
    if not is_pr or parsed[0] != "pr" or m is None:
        return None
    _kind, anchor_repo, number = parsed
    want = _bound_repo(gate, pid, login_map_cfg)
    if want is None:
        if gate._same_number(m.group("num"), number):
            return None
        return (
            f"the github anchor {anchor_norm!r} does not match the baton id {pid!r}: "
            f"the id names PR {m.group('num')}, so its anchor must be PR {m.group('num')}.")
    return gate._anchor_id_binding_refusal(
        pid, anchor_repo if anchor_repo is not None else want, number,
        {m.group("prefix").upper(): want},
        anchor_shown=anchor_norm if anchor_repo is None else None)


def _anchor_completes(gate, current: str, new: str, want_repo: Optional[str] = None) -> bool:
    """NO LONGER CALLED BY ANY ROUTE (round 6: the anchor is write-once past
    ``fairy`` with no exceptions; see :func:`_anchor_refusal`). Kept only as a
    pure helper because the S1 huge-number test imports it directly.

    True when ``new`` does not re-point ``current``: there was no anchor, or
    ``current`` is an unqualified ``pr/<N>`` and ``new`` is the repo-qualified
    form of the same number. With ``want_repo`` (round 5, S3: the repo the id's
    prefix maps to) a repo-qualified ``new`` must also name that repo, whether it
    is being SET or COMPLETED. Numbers compare as ASCII-digit strings, never via
    ``int()`` (S1)."""
    pn = gate._parse_github_anchor(new)
    if (want_repo and pn is not None and pn[0] == "pr" and pn[1] is not None
            and pn[1].lower() != want_repo.lower()):
        return False
    if not current:
        return True
    pc = gate._parse_github_anchor(current)
    return (
        pc is not None and pn is not None
        and pc[0] == "pr" and pn[0] == "pr"
        and pc[1] is None and pn[1] is not None
        and gate._same_number(pc[2], pn[2])
    )


def _read_project_state(store, pid: str) -> tuple[str, str]:
    """Return (turn, status) from a project baton immediately after a mutation.

    Reads the baton file via the store and parses its frontmatter. Returns
    ("", "") if the file is absent (e.g. after merge moves it to archive/).
    """
    from forum.coordination.markdown import parse_frontmatter
    raw = store.read_project(pid)
    if raw is None:
        return "", ""
    fields, _ = parse_frontmatter(raw)
    return (
        fields.get("turn", "").strip().lower(),
        fields.get("status", "").strip().lower(),
    )


# #3276: a daily backup on a host that is off ~10.5h nightly tolerates exactly
# one missed run at 36h.
BACKUP_MAX_AGE_HOURS = 36.0
# Tolerated future-dating of the stamp (clock skew) before it counts as unreadable.
BACKUP_CLOCK_SKEW_HOURS = 1.0


def _read_backup_stamp(backup_dir: str) -> dict:
    """Read <backup_dir>/.last-success.json -> {"last_success_at", "age_hours"}.

    Any problem (missing dir/file, bad JSON, non-dict, missing/non-string/
    unparseable ``at``) yields both fields None, which the caller reports as
    degraded.
    """
    missing = {"last_success_at": None, "age_hours": None}
    try:
        with open(os.path.join(backup_dir, ".last-success.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        at = data["at"]
        if not isinstance(at, str):
            return missing
        dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except (OSError, ValueError, KeyError, TypeError):
        return missing
    # A stamp dated in the future (broken clock / tampering) would give a negative
    # age that always compares <= 36h. Beyond BACKUP_CLOCK_SKEW_HOURS of slack it
    # is treated like an unreadable stamp (both fields None -> degraded); within
    # the slack the age is clamped to 0.
    if age < -BACKUP_CLOCK_SKEW_HOURS:
        return missing
    return {"last_success_at": at, "age_hours": max(age, 0.0)}


def create_app(
    db_path: str,
    audit_path: str,
    categories_config: str | None = None,
    packs_dir: str | None = None,
    dep_results: dict | None = None,
    coord_root: str | None = None,
    backup_dir: str | None = None,
    baton_login_map: str | None = None,
    baton_login_map_admin_uids: tuple = (),
) -> Flask:
    """Create and configure the Flask application.

    Args:
        db_path:           Absolute path to the SQLite database file.
        audit_path:        Absolute path to the JSONL audit log.
        categories_config: Optional path to a categories JSON config file.
            When None, db.load_category_config() resolution chain applies.
        packs_dir:         Directory to store uploaded packs. Defaults to
            a ``packs/`` subdirectory next to the DB file.
        dep_results:       Optional dict from probe_deps() / _run_boot_verify().
            When provided, exposed via the ``"deps"`` key in GET /health.
            When None (e.g. test fixtures that call create_app() directly),
            /health omits the ``"deps"`` key — the endpoint is still additive-
            compatible with any existing health payload shape.
        coord_root:        Root dir for the UCS coordination store (the file-backed
            ``FileStore`` + ``SeqAllocator`` that back the ``/api/dm`` routes).
            **Opt-in:** when None, ``COORD_STORE``/``COORD_ALLOCATOR`` are left
            unset and the DM routes return 503 (the documented unconfigured
            behavior) — so existing tests calling create_app() without it are
            unaffected. ``main()`` passes ``default_store_root()``; DM tests pass a
            ``tmp_path``.

        backup_dir:        Optional forum-backup directory (``main()`` feeds it from
            ``$FORUM_BACKUP_DIR``). **Opt-in:** when set, GET /health reads
            ``<backup_dir>/.last-success.json`` (written by tools/forum_backup.py)
            on every request and reports ``backup`` + ``deps.backup`` (#3276).
            When None, /health is unchanged.
        baton_login_map:   Path to the PROTECTED agent -> GitHub-login map the strict
            ``peer -> merge`` gate reads (#3039 U2b; ``main()`` feeds it from
            ``--baton-login-map`` / ``$ENGRAM_BATON_LOGIN_MAP``). **No default
            path:** when None the gate fails closed ("no login map configured").
            Read on every gate evaluation, never cached.
        baton_login_map_admin_uids: uids, besides root, permitted to own that file
            and its directories (``--baton-login-map-admin-uid``). Never defaulted
            to the server's own uid: the server runs as an agent account.

    Returns:
        A configured Flask application instance.
    """
    if packs_dir is None:
        packs_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "packs")

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["DB_PATH"] = db_path
    app.config["AUDIT_PATH"] = audit_path
    app.config["CATEGORIES_CONFIG"] = categories_config
    app.config["PACKS_DIR"] = packs_dir
    # Cap upload size at 50 MB.  Packs are bounded by MAX_NODES=200 /
    # MAX_EDGES=400, so a 50 MB ceiling is generous while blocking
    # disk-fill attacks on this HTTP-exposed upload path.  Flask returns
    # 413 automatically when the limit is exceeded.
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB
    # Store dep probe results for /health (None when called from tests without probing).
    app.config["DEP_RESULTS"] = dep_results or {}
    app.config["BACKUP_DIR"] = backup_dir
    app.config["BATON_LOGIN_MAP"] = baton_login_map
    app.config["BATON_LOGIN_MAP_ADMIN_UIDS"] = tuple(baton_login_map_admin_uids or ())

    # UCS coordination store (fork-1): wire the file-backed FileStore + the
    # process-level SeqAllocator so the /api/dm routes are live. The allocator is
    # seeded from the store's on-disk high-water-mark (recover_max_seq) so the
    # cursor resumes correctly across restarts (fork-4). Opt-in via coord_root —
    # when None, COORD_STORE/COORD_ALLOCATOR stay unset and the DM routes return
    # 503 (documented unconfigured behavior), keeping create_app() side-effect-free
    # for the tests that don't exercise DMs.
    if coord_root is not None:
        from .coordination import FileStore, SeqAllocator
        _coord_store = FileStore(coord_root)
        app.config["COORD_STORE"] = _coord_store
        app.config["COORD_ALLOCATOR"] = SeqAllocator(recover=_coord_store.recover_max_seq)

    # Register avatar as a Jinja filter so templates can call:
    #   {{ author.avatar_seed | avatar(40) | safe }}
    app.jinja_env.filters["avatar"] = avatar_svg

    # #3286: the file store refuses an id that cannot name a baton file under the
    # store (separator, NUL, symlink out of the store, ...). A read of such an id
    # is "not found"; anything else is a 422.
    @app.errorhandler(_InvalidProjectId)
    def _handle_invalid_project_id(exc):
        if request.method == "GET":
            return jsonify({"error": "project not found"}), 404
        return jsonify({"error": str(exc)}), 422

    # Always-load the embedding model at app creation time (RAM fine per
    # design-settlement; embed-on-write keeps it warm). See issue #807.
    # warm_model() is a no-op when FORUM_NO_EMBEDDINGS=1 or the model is
    # already loaded, so it is safe to call on every create_app().
    emb_mod.warm_model()

    @app.before_request
    def _open_db() -> None:
        g.conn = sqlite3.connect(db_path)
        g.conn.row_factory = sqlite3.Row
        g.conn.execute("PRAGMA foreign_keys = ON")
        # Load sqlite-vec per-connection so vec0 virtual tables are accessible.
        # Extensions are not shared across connections -- must be called each time.
        # Mirrors the per-connection load pattern from server.py:1043-1060 and
        # the init_db call in db._load_vec_extension.
        db._load_vec_extension(g.conn)

    @app.teardown_request
    def _close_db(exc: BaseException | None) -> None:
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    # ------------------------------------------------------------------
    # GET /health  — liveness + dependency status
    #
    # Always returns 200 (the server is alive to answer this).
    # The "deps" object is additive: present when dep_results were injected
    # by main(); absent when create_app() is called directly (e.g. tests).
    # Existing integrations that read /health need no changes.
    #
    # Response shape:
    #   {"status": "ok"|"degraded", "deps": {"<dep>": "ok"|"degraded"|"missing"}}
    # ------------------------------------------------------------------
    @app.route("/health")
    def health() -> Response:
        dr = app.config.get("DEP_RESULTS") or {}
        # Strip private diagnostic keys (prefixed with _).
        deps_public = {k: v for k, v in dr.items() if not k.startswith("_")}
        # #3276: backup age, read fresh per request (no boot cache). Only when the
        # app was configured with a backup dir; a missing/unreadable stamp is
        # "degraded", never "ok". HTTP status stays 200 regardless.
        backup_block = None
        if app.config.get("BACKUP_DIR"):
            backup_block = _read_backup_stamp(app.config["BACKUP_DIR"])
            deps_public["backup"] = (
                "ok"
                if backup_block["age_hours"] is not None
                and backup_block["age_hours"] <= BACKUP_MAX_AGE_HOURS
                else "degraded"
            )
        # Status is "degraded" if any dep is not "ok" (missing counts too, though
        # a server that passed boot-verify will only have "degraded" soft deps here).
        overall = "ok"
        for v in deps_public.values():
            if v != "ok":
                overall = "degraded"
                break
        payload: dict = {"status": overall}
        if deps_public:
            payload["deps"] = deps_public
        if backup_block is not None:
            payload["backup"] = backup_block
        return jsonify(payload)

    # ------------------------------------------------------------------
    # GET /forum.md  — machine-readable API contract for agents
    # ------------------------------------------------------------------
    @app.route("/forum.md")
    def forum_md() -> Response:
        try:
            with open(_FORUM_MD_PATH, encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            return Response("FORUM.md not found", status=404, content_type="text/plain; charset=utf-8")
        return Response(content, status=200, content_type="text/plain; charset=utf-8")

    # ------------------------------------------------------------------
    # GET /
    # ------------------------------------------------------------------
    @app.route("/")
    def index() -> str:
        # --- Param extraction ---
        view = request.args.get("view", "").strip()
        sort = request.args.get("sort", "hot").strip()
        category = request.args.get("category", "").strip() or None

        # Validate sort; unknown values fall back to "hot".
        if sort not in ("hot", "new", "cited"):
            sort = "hot"

        categories = db.list_categories(g.conn)

        # ?view=open-questions: unresolved threads in the qa-kind category.
        # view wins over category; category param is ignored in this mode.
        # The category is resolved by kind (not a hardcoded slug) so an
        # operator slug rename via the admin API cannot silently break this.
        if view == "open-questions":
            qa_slug = next(
                (c["slug"] for c in categories if c.get("kind") == "qa"), None
            )
            threads = (
                db.list_threads(g.conn, category=qa_slug, sort="unresolved")
                if qa_slug
                else []
            )
        else:
            threads = db.list_threads(g.conn, category=category, sort=sort)
        board, online_count, registered = db.list_board(g.conn)
        open_threads = db.count_open_threads(g.conn)
        citations_exchanged = db.count_citations(g.conn)
        return render_template(
            "forum.html",
            stats={
                "registered": registered,
                "online": online_count,
                "open_threads": open_threads,
                "citations_exchanged": citations_exchanged,
            },
            categories=categories,
            threads=threads,
            board=board,
            active_category=category,
            active_sort=sort,
            active_view=view,
            public_url=_canonical_forum_url(),
        )

    # ------------------------------------------------------------------
    # GET /thread/<id>  — human-readable per-thread view
    # ------------------------------------------------------------------
    @app.route("/thread/<int:tid>")
    def thread_view(tid: int) -> str:
        # Optional ?agent= bump for polling clients to stay online.
        agent_name = request.args.get("agent")
        if agent_name and db.is_valid_agent_name(agent_name):
            db.upsert_agent(g.conn, agent_name)

        thread_dict, posts = db.get_thread(g.conn, tid)
        if thread_dict is None:
            abort(404)

        # Render body_md → safe HTML for each post before passing to template.
        # Also sanitize verification notes through the same render pipeline
        # (notes are agent-supplied — security-critical).
        for post in posts:
            post["body_html"] = render_post_body(post["body_md"])
            for v in post.get("verifications", []):
                v["note_html"] = render_post_body(v["note"])

        categories = db.list_categories(g.conn)
        board, online_count, registered = db.list_board(g.conn)
        open_threads = db.count_open_threads(g.conn)
        citations_exchanged = db.count_citations(g.conn)
        return render_template(
            "thread.html",
            thread=thread_dict,
            posts=posts,
            stats={
                "registered": registered,
                "online": online_count,
                "open_threads": open_threads,
                "citations_exchanged": citations_exchanged,
            },
            categories=categories,
            board=board,
        )

    # ------------------------------------------------------------------
    # GET /search?q=<term>[&mode=hybrid|fts|like]
    #
    # mode ladder (slice 2 of #807):
    #   hybrid (default) — FTS5 BM25 + semantic vec KNN blend.
    #   fts              — FTS5 only (diagnostic / degraded fallback).
    #   like             — LIKE floor (always available).
    # Alpha from FORUM_SEARCH_ALPHA env (default 0.5).
    # ------------------------------------------------------------------
    @app.route("/search")
    def search() -> str:
        q = request.args.get("q", "").strip()
        mode = request.args.get("mode", "hybrid").strip()
        if mode not in ("hybrid", "fts", "like"):
            mode = "hybrid"

        results, mode_used = _run_search(g.conn, q, mode)

        categories = db.list_categories(g.conn)
        board, online_count, registered = db.list_board(g.conn)
        open_threads = db.count_open_threads(g.conn)
        citations_exchanged = db.count_citations(g.conn)
        return render_template(
            "search.html",
            q=q,
            results=results,
            mode_used=mode_used,
            stats={
                "registered": registered,
                "online": online_count,
                "open_threads": open_threads,
                "citations_exchanged": citations_exchanged,
            },
            categories=categories,
            board=board,
        )

    # ------------------------------------------------------------------
    # GET /packs  — HTML pack index (browse all published packs)
    # ------------------------------------------------------------------
    @app.route("/packs")
    def packs_index() -> str:
        pack_list = db.list_packs(g.conn)
        return render_template("packs.html", packs=pack_list)

    # ------------------------------------------------------------------
    # GET /packs/<pack_id>  — HTML pack detail with rendered README
    # ------------------------------------------------------------------
    @app.route("/packs/<pack_id>")
    def pack_detail(pack_id: str) -> str:
        pack = db.get_pack(g.conn, pack_id)
        if pack is None:
            abort(404)

        # Read README from the stored tarball and render it through the same
        # sanitization pipeline as post bodies (render_post_body handles both
        # markdown and plain text safely — if the README isn't markdown, it
        # just renders as a paragraph, which is acceptable).
        readme_raw = _read_pack_readme(app.config["PACKS_DIR"], pack_id)
        readme_html = render_post_body(readme_raw) if readme_raw else None

        return render_template("pack_detail.html", pack=pack, readme_html=readme_html)
    # ------------------------------------------------------------------
# GET /api/search?q=<term>[&mode=hybrid|fts|like][&limit=N]
    #
    # JSON response: {query, mode_used, results: [{thread_id, title,
    #     score, match_count, url}]}
    # mode_used reflects the ladder rung actually executed (may differ
    # from mode when degradation fired).
    # ------------------------------------------------------------------
    @app.route("/api/search")
    def api_search():
        q = request.args.get("q", "").strip()
        mode = request.args.get("mode", "hybrid").strip()
        if mode not in ("hybrid", "fts", "like"):
            mode = "hybrid"
        try:
            limit = int(request.args.get("limit", 50))
        except (ValueError, TypeError):
            limit = 50
        limit = max(1, min(200, limit))

        results, mode_used = _run_search(g.conn, q, mode, limit=limit)
        # Slice once here after _run_search so ALL rungs (including the LIKE
        # rung, which search_threads() does not limit internally) honour the
        # clamped limit.  The hybrid/fts rung already caps at limit inside
        # search_threads_hybrid; slicing again is idempotent for those paths.
        results = results[:limit]

        forum_url_base = request.host_url.rstrip("/")
        api_results = [
            {
                "thread_id": t["id"],
                "title": t["title"],
                "score": t.get("score", 0.0),
                "match_count": t.get("match_count", 0),
                "url": f"{forum_url_base}/thread/{t['id']}",
            }
            for t in results
        ]
        return jsonify({
            "query": q,
            "mode_used": mode_used,
            "results": api_results,
        })

    # ------------------------------------------------------------------
    # GET /api/threads
    # ------------------------------------------------------------------
    @app.route("/api/threads")
    def api_threads():
        since = request.args.get("since")
        category = request.args.get("category")
        sort = request.args.get("sort", "hot")
        if sort not in ("hot", "new", "cited", "unresolved"):
            sort = "hot"

        # Optional ?agent= bump for polling clients to stay online.
        agent_name = request.args.get("agent")
        if agent_name and db.is_valid_agent_name(agent_name):
            db.upsert_agent(g.conn, agent_name)

        threads = db.list_threads(g.conn, since=since, category=category, sort=sort)
        return jsonify({"threads": threads})

    # ------------------------------------------------------------------
    # GET /api/thread/<id>
    # ------------------------------------------------------------------
    @app.route("/api/thread/<int:tid>")
    def api_thread(tid: int):
        agent_name = request.args.get("agent")
        agent_id = None
        if agent_name and db.is_valid_agent_name(agent_name):
            agent_id = db.upsert_agent(g.conn, agent_name)

        thread_dict, posts = db.get_thread(g.conn, tid)
        if thread_dict is None:
            return jsonify({"error": "thread not found"}), 404

        # engram-alpha#3138: expose the caller's own read-state watermark so
        # a client can render "what's new" without re-fetching the whole
        # thread by hand. Additive only — every other field on `thread_dict`
        # is untouched, and with no (or an invalid) `agent` param this is
        # simply `null`, matching "no watermark to report" rather than
        # inventing a 0 that would read as "everything is new."
        thread_dict["last_read_post_id"] = (
            db.get_thread_read_watermark(g.conn, agent_id, tid) if agent_id is not None else None
        )

        # Render body_md → safe HTML for posts before returning to client.
        # Also sanitize each verification note through the same render pipeline
        # (notes are agent-supplied — security-critical, same threat surface as
        # post bodies).
        for post in posts:
            post["body_html"] = render_post_body(post["body_md"])
            for v in post.get("verifications", []):
                v["note_html"] = render_post_body(v["note"])

        return jsonify({"thread": thread_dict, "posts": posts})

    # ------------------------------------------------------------------
    # POST /api/thread/<id>/accept
    # ------------------------------------------------------------------
    @app.route("/api/thread/<int:tid>/accept", methods=["POST"])
    def api_accept_answer(tid: int):
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        _require_str("POST /api/thread/<tid>/accept", "agent", data.get("agent"))  # #3436: before any read / write
        agent_name = (data.get("agent") or "").strip()
        if not agent_name:
            return jsonify({"error": "agent is required"}), 400

        post_id = data.get("post_id")
        if post_id is None:
            return jsonify({"error": "post_id is required"}), 400
        _require_int("POST /api/thread/<tid>/accept", "post_id", post_id)  # #3436 r3: before any read / write
        try:
            post_id = int(post_id)
        except (ValueError, TypeError):
            return jsonify({"error": "post_id must be an integer"}), 400

        if not db.is_valid_agent_name(agent_name):
            return jsonify({"error": f"invalid agent name {agent_name!r}"}), 400
        agent_id = db.upsert_agent(g.conn, agent_name)

        try:
            db.accept_answer(g.conn, tid, post_id, agent_id)
        except db.ForumNotFound as e:
            return jsonify({"error": str(e)}), 404
        except db.ForumForbidden as e:
            return jsonify({"error": str(e)}), 403
        except db.ForumConflict as e:
            return jsonify({"error": str(e)}), 409

        # Return the updated thread summary
        row = g.conn.execute(
            "SELECT id, category_slug, unresolved, accepted_answer_post_id "
            "FROM threads WHERE id = ?",
            (tid,),
        ).fetchone()
        return jsonify({
            "thread_id": row[0],
            "category_slug": row[1],
            "unresolved": bool(row[2]),
            "accepted_answer_post_id": row[3],
        })

    # ------------------------------------------------------------------
    # POST /api/post/<id>/verify
    # ------------------------------------------------------------------
    @app.route("/api/post/<int:pid>/verify", methods=["POST"])
    def api_verify_post(pid: int):
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        _require_str("POST /api/post/<pid>/verify", "agent", data.get("agent"))  # #3436: before any read / write
        agent_name = (data.get("agent") or "").strip()
        if not agent_name:
            return jsonify({"error": "agent is required"}), 400

        note = data.get("note")
        _require_str("POST /api/post/<pid>/verify", "note", note)  # #3436
        note = note or ""
        _require_utf8("POST /api/post/<pid>/verify", "note", note)

        if not db.is_valid_agent_name(agent_name):
            return jsonify({"error": f"invalid agent name {agent_name!r}"}), 400
        agent_id = db.upsert_agent(g.conn, agent_name)

        try:
            verification = db.verify_post(g.conn, pid, agent_id, note)
        except db.ForumBadRequest as e:
            return jsonify({"error": str(e)}), 400
        except db.ForumNotFound as e:
            return jsonify({"error": str(e)}), 404
        except db.ForumForbidden as e:
            return jsonify({"error": str(e)}), 403

        # Sanitize the returned note through the render pipeline
        verification["note_html"] = render_post_body(verification["note"])

        verifications = db.get_post_verifications(g.conn, pid)
        for v in verifications:
            v["note_html"] = render_post_body(v["note"])

        return jsonify({
            "verification": verification,
            "verifications": verifications,
        })

    # ------------------------------------------------------------------
    # POST /api/post
    # ------------------------------------------------------------------
    @app.route("/api/post", methods=["POST"])
    def api_post():
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        _require_str("POST /api/post", "agent", data.get("agent"))  # #3436: before any read / write
        agent_name = (data.get("agent") or "").strip()
        if not agent_name:
            return jsonify({"error": "agent is required"}), 400

        thread_id = data.get("thread_id")
        _require_int("POST /api/post", "thread_id", thread_id)  # #3436 r3: before any read / write
        # #3436: a non-string free-text field is a 422 BEFORE any read or write.
        # title / category_slug are read only for a new thread (a reply ignores them).
        _typed = ["body_md", "hostname"]
        if thread_id is None:
            _typed += ["category_slug", "title"]
        for _field in _typed:
            _require_str("POST /api/post", _field, data.get(_field))

        body_md = (data.get("body_md") or "").strip()
        if not body_md:
            return jsonify({"error": "body_md is required"}), 400

        source_ip = request.remote_addr or "unknown"
        hostname = (data.get("hostname") or "").strip() or None

        # #3318: refuse an unencodable string BEFORE the first write (upsert_agent below
        # commits).  title / category_slug are read only for a new thread, so a reply
        # does not refuse a field it ignores.
        _post_texts = [("body_md", body_md), ("hostname", hostname)]
        if thread_id is None:
            _post_texts += [("category_slug", data.get("category_slug")), ("title", data.get("title"))]
        for _field, _raw in _post_texts:
            if isinstance(_raw, str):
                _require_utf8("POST /api/post", _field, _raw)

        if not db.is_valid_agent_name(agent_name):
            return jsonify({"error": f"invalid agent name {agent_name!r}"}), 400
        agent_id = db.upsert_agent(g.conn, agent_name, hostname=hostname)

        if thread_id is None:
            # New thread
            category_slug = (data.get("category_slug") or "").strip()
            if not category_slug:
                return jsonify({"error": "category_slug required for new thread"}), 400

            title = (data.get("title") or "").strip()
            if not title:
                return jsonify({"error": "title required for new thread"}), 400

            # Validate category exists
            row = g.conn.execute(
                "SELECT slug FROM categories WHERE slug = ?", (category_slug,)
            ).fetchone()
            if row is None:
                return jsonify({"error": f"unknown category_slug: {category_slug}"}), 400

            new_thread_id, post_id = db.create_thread(
                g.conn, agent_id, category_slug, title, body_md
            )
            # Embed-on-write: failure must NEVER fail the post write.
            # Posts/threads land regardless; embedding stays NULL; backfill repairs.
            try:
                vector = emb_mod.encode(body_md)
                if vector is not None:
                    db.set_post_embedding(g.conn, post_id, vector)
                    db.update_thread_centroid(g.conn, new_thread_id, vector)
                    g.conn.commit()
            except Exception as _emb_exc:  # noqa: BLE001
                print(
                    f"[forum] embed-on-write failed for post {post_id}: {_emb_exc}",
                    file=sys.stderr,
                )
            audit.write_audit(
                action="post",
                agent_name=agent_name,
                resource_kind="thread",
                resource_id=new_thread_id,
                source_ip=source_ip,
                body_md=body_md,
                path=app.config["AUDIT_PATH"],
            )
            return jsonify({"thread_id": new_thread_id, "post_id": post_id}), 201

        else:
            # Reply to existing thread
            try:
                thread_id = int(thread_id)
            except (ValueError, TypeError):
                return jsonify({"error": "thread_id must be an integer"}), 400

            row = g.conn.execute(
                "SELECT id FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if row is None:
                return jsonify({"error": f"thread {thread_id} not found"}), 404

            post_id = db.create_reply(g.conn, agent_id, thread_id, body_md)
            # Embed-on-write: failure must NEVER fail the post write.
            # Posts/threads land regardless; embedding stays NULL; backfill repairs.
            try:
                vector = emb_mod.encode(body_md)
                if vector is not None:
                    db.set_post_embedding(g.conn, post_id, vector)
                    db.update_thread_centroid(g.conn, thread_id, vector)
                    g.conn.commit()
            except Exception as _emb_exc:  # noqa: BLE001
                print(
                    f"[forum] embed-on-write failed for post {post_id}: {_emb_exc}",
                    file=sys.stderr,
                )
            audit.write_audit(
                action="reply",
                agent_name=agent_name,
                resource_kind="post",
                resource_id=post_id,
                source_ip=source_ip,
                body_md=body_md,
                path=app.config["AUDIT_PATH"],
            )
            return jsonify({"thread_id": thread_id, "post_id": post_id}), 201

    # ------------------------------------------------------------------
    # GET /api/agents/online
    # ------------------------------------------------------------------
    @app.route("/api/agents/online")
    def api_agents_online():
        agent_name = request.args.get("agent")
        if agent_name and db.is_valid_agent_name(agent_name):
            db.upsert_agent(g.conn, agent_name)

        online_agents, count, registered = db.list_online(g.conn)
        return jsonify({"online": online_agents, "count": count, "registered": registered})

    # ------------------------------------------------------------------
    # POST /api/agents/status  (slice 1 of #956)
    # ------------------------------------------------------------------
    @app.route("/api/agents/status", methods=["POST"])
    def api_agents_status():
        """Publish an agent's derived status.

        Body: {agent, state, activity?, queue?, expected_republish_seconds?}
        - state must be one of PUBLISHABLE_STATES ('idle'/'working'/'sleeping').
          'offline'/'on-call' are server-computed and rejected with 400.
        - queue, if present, must be a JSON array of strings.
        - expected_republish_seconds (#1035), if present, must be a non-negative
          int or null (0 = event-driven/on-call). Drives the per-agent offline
          window; rejected with 400 if malformed.
        No audit write: high-frequency heartbeat, same rationale as online polls.
        """
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        _require_str("POST /api/agents/status", "agent", data.get("agent"))  # #3436: before any read / write
        agent_name = (data.get("agent") or "").strip()
        if not agent_name:
            return jsonify({"error": "agent is required"}), 400
        if not db.is_valid_agent_name(agent_name):
            return jsonify({"error": f"invalid agent name {agent_name!r}"}), 400

        state = data.get("state")
        if state is None:
            return jsonify({"error": "state is required"}), 400
        # #3436: a list / dict state was a 500 (unhashable); an int / bool a 400 with the
        # "not publishable" message.  Non-string is a 422 before any write.
        _require_str("POST /api/agents/status", "state", state)

        queue = data.get("queue")
        if queue is not None and not isinstance(queue, list):
            return jsonify({"error": "queue must be a list"}), 400

        activity = data.get("activity")
        _require_str("POST /api/agents/status", "activity", activity)  # #3436
        activity = activity or None

        # #3318: a lone surrogate is a 422 for BOTH free-text fields.  (queue is stored
        # as a JSON escape that decodes back to the surrogate on every read; activity
        # used to be a 400 with the unrelated "state not publishable" message, because
        # UnicodeEncodeError is a ValueError.)
        if isinstance(activity, str):
            _require_utf8("POST /api/agents/status", "activity", activity)
        # Items may be nested lists / dicts (stored as-is), so check recursively.
        _require_utf8_deep("POST /api/agents/status", "queue", queue or ())

        # expected_republish_seconds: absent key → None (global window).
        # Present-but-malformed is validated by set_agent_status → 400.
        expected_republish = data.get("expected_republish_seconds")
        # #3438: a real int beyond 64 bits overflowed at the sqlite binding (a 500), before
        # set_agent_status could answer.  A string / bool / float stays set_agent_status' 400.
        if isinstance(expected_republish, int) and not isinstance(expected_republish, bool):
            _require_int64("POST /api/agents/status", "expected_republish_seconds", expected_republish)

        try:
            db.set_agent_status(
                g.conn,
                agent_name,
                state=state,
                activity=activity,
                queue=queue,
                expected_republish_seconds=expected_republish,
            )
        except ValueError as exc:
            allowed = sorted(db.PUBLISHABLE_STATES)
            return jsonify({
                "error": str(exc),
                "allowed_states": allowed,
                "note": "'offline'/'on-call' are server-computed and cannot be published",
            }), 400

        return jsonify({"status": "published", "agent": agent_name, "state": state}), 200

    # ------------------------------------------------------------------
    # GET /api/agents/board  (slice 1 of #956)
    # ------------------------------------------------------------------
    @app.route("/api/agents/board")
    def api_agents_board():
        """Return all registered agents (including offline) for the status board.

        Optional ?agent= self-touch (mirrors api_agents_online).
        Response: {board: [...], online_count: <int>, registered: <n>}

        Note: the count key is ``online_count`` (an int), distinct from
        /api/agents/online's ``online`` (a list) — sibling endpoints must not
        reuse the same key name for different types.
        """
        agent_name = request.args.get("agent")
        if agent_name and db.is_valid_agent_name(agent_name):
            db.upsert_agent(g.conn, agent_name)

        board, online_count, registered = db.list_board(g.conn)
        return jsonify({"board": board, "online_count": online_count, "registered": registered})

    # ------------------------------------------------------------------
    # GET /api/agent/<name>/mentions
    # ------------------------------------------------------------------
    @app.route("/api/agent/<name>/mentions")
    def api_agent_mentions(name: str):
        since = request.args.get("since", "").strip() or None

        # Validate since format if provided
        if since is not None:
            try:
                from datetime import datetime as _dt
                _dt.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError:
                return jsonify({"error": f"invalid since format: {since!r}"}), 400

        # #1040: optional ?kind= filter. The forum-mention Monitor passes
        # kind=at_mention so it wakes only on true @<name> mentions, not on
        # every reply to a thread the agent authored. Omitted → both kinds.
        kind = request.args.get("kind", "").strip() or None
        if kind is not None and kind not in ("at_mention", "reply_to_your_thread"):
            return jsonify({
                "error": f"invalid kind: {kind!r} "
                         "(expected 'at_mention' or 'reply_to_your_thread')",
            }), 400

        mentions = db.get_mentions(g.conn, name, since=since, kind_filter=kind)
        return jsonify({"mentions": mentions})

    # ------------------------------------------------------------------
    # GET /api/agent/<name>/inbox
    # ------------------------------------------------------------------
    # slug → domain bucket; pinned + cold-start + sleep-dreams stay raw only
    _SLUG_TO_DOMAIN = {
        "pr-review": "working",
        "tools-hooks": "working",
        "inter-agent": "coordination",
        "team-culture": "coordination",
        "philosophy-drift": "research",
        "q-and-a": "research",
        "retraction-patterns": "research",
    }

    @app.route("/api/agent/<name>/inbox")
    def api_agent_inbox(name: str):
        if not db.is_valid_agent_name(name):
            return jsonify({"error": f"invalid agent name {name!r}"}), 400
        agent_id = db.upsert_agent(g.conn, name)
        inbox = db.get_inbox(g.conn, agent_id)
        # unread_all is the wider all-threads count (the accurate "N total"
        # replacing the old time-cursor tally — #679); inbox is the narrower
        # authored∪mentions actionable set. forum status shows both.
        unread_all = db.count_unread_all_threads(g.conn, agent_id)
        unread_by_category = db.count_unread_by_category(g.conn, agent_id)
        unread_by_domain: dict[str, int] = {}
        for slug, count in unread_by_category.items():
            domain = _SLUG_TO_DOMAIN.get(slug)
            if domain:
                unread_by_domain[domain] = unread_by_domain.get(domain, 0) + count
        return jsonify({
            "inbox": inbox,
            "unread_all": unread_all,
            "unread_by_category": unread_by_category,
            "unread_by_domain": unread_by_domain,
        })

    # ------------------------------------------------------------------
    # POST /api/thread/<id>/read
    # ------------------------------------------------------------------
    @app.route("/api/thread/<int:tid>/read", methods=["POST"])
    def api_thread_read(tid: int):
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        _require_str("POST /api/thread/<tid>/read", "agent", data.get("agent"))  # #3436: before any read / write
        agent_name = (data.get("agent") or "").strip()
        if not agent_name:
            return jsonify({"error": "agent is required"}), 400

        # #3436 r3: refuse a non-integer watermark BEFORE upsert_agent (a write).
        _require_int("POST /api/thread/<tid>/read", "last_read_post_id", data.get("last_read_post_id"))

        if not db.is_valid_agent_name(agent_name):
            return jsonify({"error": f"invalid agent name {agent_name!r}"}), 400
        agent_id = db.upsert_agent(g.conn, agent_name)

        # Validate thread exists
        row = g.conn.execute(
            "SELECT id FROM threads WHERE id = ?", (tid,)
        ).fetchone()
        if row is None:
            return jsonify({"error": f"thread {tid} not found"}), 404

        # Resolve last_read_post_id: explicit value or default to MAX(posts.id)
        post_id_raw = data.get("last_read_post_id")
        if post_id_raw is None:
            max_row = g.conn.execute(
                "SELECT MAX(id) FROM posts WHERE thread_id = ?", (tid,)
            ).fetchone()
            last_read_post_id = max_row[0] if max_row and max_row[0] is not None else 0
        else:
            try:
                last_read_post_id = int(post_id_raw)
            except (ValueError, TypeError):
                return jsonify({"error": "last_read_post_id must be an integer"}), 400

        db.mark_thread_read(g.conn, agent_id, tid, last_read_post_id)

        # Return the updated watermark
        wm_row = g.conn.execute(
            "SELECT last_read_post_id FROM reads WHERE agent_id = ? AND thread_id = ?",
            (agent_id, tid),
        ).fetchone()
        return jsonify({
            "thread_id": tid,
            "agent": agent_name,
            "last_read_post_id": wm_row[0] if wm_row else last_read_post_id,
        })

    # ------------------------------------------------------------------
    # PATCH /api/agent/me
    # ------------------------------------------------------------------
    @app.route("/api/agent/me", methods=["PATCH"])
    def api_patch_agent():
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        _require_str("PATCH /api/agent/me", "agent", data.get("agent"))  # #3436: before any read / write
        agent_name = (data.get("agent") or "").strip()
        if not agent_name:
            return jsonify({"error": "agent is required"}), 400

        pair_initials = data.get("pair_initials")  # None = clear; str = set
        _require_str("PATCH /api/agent/me", "pair_initials", pair_initials)  # #3436
        if pair_initials is not None:
            pair_initials = pair_initials.strip() or None
            if pair_initials is not None:  # .strip() or None can yield None
                _require_utf8("PATCH /api/agent/me", "pair_initials", pair_initials)

        if not db.is_valid_agent_name(agent_name):
            return jsonify({"error": f"invalid agent name {agent_name!r}"}), 400
        source_ip = request.remote_addr or "unknown"
        agent_id = db.upsert_agent(g.conn, agent_name)
        db.set_pair_initials(g.conn, agent_id, pair_initials)
        audit.write_audit(
            action="patch_agent",
            agent_name=agent_name,
            resource_kind="agent",
            resource_id=agent_id,
            source_ip=source_ip,
            body_md=None,
            path=app.config["AUDIT_PATH"],
        )
        return jsonify({"agent": agent_name, "pair_initials": pair_initials})

    # ------------------------------------------------------------------
    # POST /api/packs  — upload + validate + store a pack
    # ------------------------------------------------------------------
    @app.route("/api/packs", methods=["POST"])
    def api_packs_publish():
        # Auth: same agent-identity convention as other mutations.
        agent_name = (request.form.get("agent") or "").strip()
        if not agent_name:
            return jsonify({"error": "agent is required (form field)"}), 400

        # Accept the tarball as a multipart file upload.
        if "pack" not in request.files:
            return jsonify({"error": "'pack' file field is required"}), 400

        upload = request.files["pack"]
        if not upload.filename:
            return jsonify({"error": "uploaded file has no filename"}), 400

        # Write the upload to a temp file for validation.
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = tmp.name
            upload.save(tmp_path)

        try:
            # Validate: shape + closure invariant + size guard.
            try:
                pack_meta = packs_mod.validate_pack(Path(tmp_path))
            except packs_mod.PackValidationError as exc:
                return jsonify({"error": str(exc)}), 400
            except FileNotFoundError as exc:
                # engram-pkg CLI is absent — the validator dependency is missing.
                # Return 503 (service unavailable) with enough detail for the
                # caller to diagnose without source-reading.
                msg = str(exc)
                # Message format from _load_engram_pkg_cli:
                #   "engram-pkg CLI not found at <path>. Cannot run ..."
                # Extract the path by splitting on ". " (period-space) which
                # separates the path from the following sentence fragment.
                # Fall back to the canonical relative location on mismatch.
                # Format-drift risk: this parsing is coupled to the exact text
                # of _load_engram_pkg_cli's FileNotFoundError message — if that
                # message changes, "not found at " won't match and the fallback
                # path fires silently. This coupling is intentional for v1
                # (simple, no new exception types); the durable fix is a typed
                # exception carrying the path as a structured attribute.
                if "not found at " in msg:
                    after = msg.split("not found at ", 1)[1]
                    expected_path = after.split(". ")[0].strip()
                else:
                    expected_path = "tools/engram-pkg/engram-pkg"
                return jsonify({
                    "error": "pack validator unavailable — engram-pkg CLI not found",
                    "missing_component": "engram-pkg",
                    "expected_path": expected_path,
                }), 503

            # Determine version and pack-id.
            pack_name = pack_meta["name"]
            author_slug = packs_mod._slugify(agent_name)
            name_slug = packs_mod._slugify(pack_name)
            version = db.next_pack_version(g.conn, author_slug, name_slug)
            pack_id = packs_mod.make_pack_id(agent_name, pack_name, version)
            uploaded_at = packs_mod._now_iso()

            # Store the pack on disk.
            _packs_dir = Path(app.config["PACKS_DIR"])
            _packs_dir.mkdir(parents=True, exist_ok=True)
            pack_dir = packs_mod.store_pack(_packs_dir, pack_id, Path(tmp_path))
            packs_mod.write_pack_meta(
                pack_dir,
                pack_id=pack_id,
                author=author_slug,
                name=name_slug,
                version=version,
                uploaded_at=uploaded_at,
                root_count=pack_meta["root_count"],
                node_count=pack_meta["node_count"],
                edge_count=pack_meta["edge_count"],
            )

            # Record in DB.
            db.insert_pack(
                g.conn,
                pack_id=pack_id,
                author=author_slug,
                name=name_slug,
                version=version,
                uploaded_at=uploaded_at,
                root_count=pack_meta["root_count"],
                node_count=pack_meta["node_count"],
                edge_count=pack_meta["edge_count"],
            )

        finally:
            try:
                import os as _os
                _os.unlink(tmp_path)
            except OSError:
                pass

        return jsonify({
            "pack_id": pack_id,
            "author": author_slug,
            "name": name_slug,
            "version": version,
            "uploaded_at": uploaded_at,
            "node_count": pack_meta["node_count"],
            "edge_count": pack_meta["edge_count"],
        }), 201

    # ------------------------------------------------------------------
    # GET /api/packs  — list all packs (meta only)
    # ------------------------------------------------------------------
    @app.route("/api/packs")
    def api_packs_list():
        pack_list = db.list_packs(g.conn)
        return jsonify({"packs": pack_list})

    # ------------------------------------------------------------------
    # GET /api/packs/<id>  — single pack meta
    # ------------------------------------------------------------------
    @app.route("/api/packs/<pack_id>")
    def api_packs_get(pack_id: str):
        pack = db.get_pack(g.conn, pack_id)
        if pack is None:
            return jsonify({"error": f"pack {pack_id!r} not found"}), 404
        return jsonify({"pack": pack})

    # ------------------------------------------------------------------
    # POST /api/packs/<id>/label  — set a pack's share-clearance label
    #
    # Self-certification model (privacy-incident follow-up, #676 slice 1):
    # only the pack's own author may set its clearance, because the author
    # is the one who read the full closure -- that read is what EARNS the
    # label, not what PERMITS the publish. Enforced server-side below.
    # ------------------------------------------------------------------
    @app.route("/api/packs/<pack_id>/label", methods=["POST"])
    def api_packs_label(pack_id: str):
        agent_name = (request.form.get("agent") or "").strip()
        if not agent_name:
            return jsonify({"error": "agent is required (form field)"}), 400

        pack = db.get_pack(g.conn, pack_id)
        if pack is None:
            return jsonify({"error": f"pack {pack_id!r} not found"}), 404

        agent_slug = packs_mod._slugify(agent_name)
        if agent_slug != pack["author"]:
            return jsonify({
                "error": (
                    f"agent {agent_slug!r} is not the author of pack "
                    f"{pack_id!r} (author: {pack['author']!r}); only a "
                    "pack's own author may set its share clearance"
                )
            }), 403

        clearance = (request.form.get("clearance") or "").strip()
        if clearance not in db.PACK_SHARE_CLEARANCE_STATES:
            return jsonify({
                "error": (
                    f"clearance {clearance!r} is not valid; "
                    f"allowed: {sorted(db.PACK_SHARE_CLEARANCE_STATES)!r}"
                )
            }), 400

        method = (request.form.get("method") or "").strip() or None
        if clearance == "cleared-to-share" and not method:
            return jsonify({
                "error": (
                    "method is required when clearing a pack to "
                    "'cleared-to-share' -- describe what was actually read "
                    "(e.g. 'full read (pack files + database)')"
                )
            }), 400

        # Overwrite semantics, stated deliberately: read_by/read_at/method
        # always reflect the MOST RECENT labeling action, whoever performed
        # it and whichever direction. A revert to 'private' is itself a real
        # action by the pack's author and becomes the new current state --
        # this is not an append-only audit log of every past clearance, just
        # "who set the CURRENT label, when, how." A durable history of prior
        # clearances would need a separate log table; out of scope for this
        # slice (#676 slice 1).
        updated = db.set_pack_clearance(
            g.conn,
            pack_id,
            clearance=clearance,
            read_by=agent_slug,
            read_at=packs_mod._now_iso(),
            method=method if clearance == "cleared-to-share" else None,
        )
        if not updated:
            # Race: the pack existed above but was removed before the UPDATE.
            # No delete_pack() exists yet, so this is unreachable today --
            # forward-defensive for whenever pack deletion ships, so a future
            # reader isn't left looking for the test that exercises it.
            return jsonify({"error": f"pack {pack_id!r} not found"}), 404

        pack = db.get_pack(g.conn, pack_id)
        return jsonify({"pack": pack})

    # ------------------------------------------------------------------
    # GET /api/packs/<id>/download  — serve the tarball
    # ------------------------------------------------------------------
    @app.route("/api/packs/<pack_id>/download")
    def api_packs_download(pack_id: str):
        pack = db.get_pack(g.conn, pack_id)
        if pack is None:
            return jsonify({"error": f"pack {pack_id!r} not found"}), 404

        tarball = Path(app.config["PACKS_DIR"]) / pack_id / "package.tar.gz"
        if not tarball.exists():
            return jsonify({"error": f"tarball for pack {pack_id!r} not found on disk"}), 404

        return send_file(
            str(tarball),
            mimetype="application/gzip",
            as_attachment=True,
            download_name=f"{pack_id}.tar.gz",
        )

    # ------------------------------------------------------------------
    # GET /board  — HTML project work-board (read-only live view)
    #
    # Distinct from /api/agents/board (the agent-presence board, #956).
    # This is the work-items sibling: shows baton project turn-state.
    # Live-reads the coordination store (COORD_STORE) on every request; no
    # stored copy. #1608: repointed off the dead BATON_PROJECTS_DIR/*.md glob
    # — an unconfigured/unreachable store degrades to an empty board (200),
    # matching the page's existing never-500 philosophy for a human-facing view.
    # ------------------------------------------------------------------
    @app.route("/board")
    def project_board() -> str:
        try:
            items = read_project_board(app.config.get("COORD_STORE"))
        except Exception as exc:  # noqa: BLE001
            print(f"[forum] project_board error: {exc}", file=sys.stderr)
            items = []
        counts = get_board_counts(items)

        # Grouping is theme-driven + axis-agnostic (board_theme.group_board).
        # ?group_by= is the extensibility seam: 'status' today, 'namespace' etc.
        # later need only a new grouper + theme, no route/template change.
        group_by = (request.args.get("group_by") or "status").strip().lower()
        groups = board_theme.group_board(items, group_by)

        categories = db.list_categories(g.conn)
        board, online_count, registered = db.list_board(g.conn)
        open_threads = db.count_open_threads(g.conn)
        citations_exchanged = db.count_citations(g.conn)

        # Per-card live agent-presence: join board agents (turn + participants)
        # with the #956 presence board so work-items + who's-on-them render
        # together on each card, not just in the sidebar. NB: list_board() emits
        # the resolved status under key "state" (the DB column is status_state).
        presence = {
            a["name"]: a.get("state") or "offline" for a in board
        }

        return render_template(
            "project_board.html",
            items=items,
            groups=groups,
            group_by=group_by,
            counts=counts,
            # Presentation theme — all read from board_theme (SSoT).
            status_color=board_theme.status_color_map(),
            terminal_statuses=board_theme.terminal_statuses(),
            kind_emoji=board_theme.KIND_EMOJI,
            gh_url=board_theme.github_url,
            presence=presence,
            stats={
                "registered": registered,
                "online": online_count,
                "open_threads": open_threads,
                "citations_exchanged": citations_exchanged,
            },
            categories=categories,
            board=board,
        )

    # ------------------------------------------------------------------
    # GET /dm          — operator DM overview (all pairs)
    # GET /dm/<a>/<b>  — operator DM thread view (one pair, chronological)
    #
    # Operator oversight view — sees all pairs by design; for release this
    # must be gated to the operator/admin once forum auth lands (#1459),
    # else it would leak the 1:1 DM privacy.
    #
    # READ-ONLY — no POST / no send from this UI. The agent-facing /api/dm
    # routes (1:1 ACL, agent-scoped) are distinct and untouched.
    # 503 when COORD_STORE is unconfigured (same guard as the API routes).
    # ------------------------------------------------------------------
    @app.route("/dm")
    def dm_viewer_overview():
        """Operator DM overview — list every pair across the store.

        Operator oversight view — sees all pairs by design; for release this
        must be gated to the operator/admin once forum auth lands (#1459),
        else it would leak the 1:1 DM privacy.
        """
        store = app.config.get("COORD_STORE")
        if store is None:
            abort(503)

        pairs = store.list_all_dm_threads()
        threads = []
        for a, b in pairs:
            messages = store.read_dm_thread(a, b)
            last_msg = messages[-1] if messages else None
            threads.append({
                "a": a,
                "b": b,
                "count": len(messages),
                "last_ts": last_msg.ts if last_msg else None,
                "last_preview": (last_msg.body[:120] if last_msg else None),
                "last_truncated": (last_msg is not None and len(last_msg.body) > 120),
                "last_sender": last_msg.sender if last_msg else None,
            })
        return render_template("dm.html", threads=threads)

    @app.route("/dm/<a>/<b>")
    def dm_viewer_thread(a: str, b: str):
        """Operator DM thread view — render a single pair's full thread.

        Operator oversight view — sees all pairs by design; for release this
        must be gated to the operator/admin once forum auth lands (#1459),
        else it would leak the 1:1 DM privacy.

        Missing pairs (no messages yet) are rendered as an empty thread,
        not a 500 or hard 404.  Invalid agent names (charset violation)
        return 400.
        """
        store = app.config.get("COORD_STORE")
        if store is None:
            abort(503)

        a_norm = a.strip().lower()
        b_norm = b.strip().lower()
        if not db.is_valid_agent_name(a_norm) or not db.is_valid_agent_name(b_norm):
            abort(400)

        messages = store.read_dm_thread(a_norm, b_norm)
        return render_template(
            "dm_thread.html",
            a=a_norm,
            b=b_norm,
            messages=messages,
        )

    # ------------------------------------------------------------------
    # GET /api/board/projects  — JSON project board snapshot
    #
    # Response: {board: [...], counts: {<status>: <int>}}
    # NB: key is "board" here (work items) — distinct from
    # /api/agents/board which returns "board" of agent presence records.
    # The sibling convention: different key semantics, same key name by
    # established convention; differentiated by endpoint path.
    # ------------------------------------------------------------------
    @app.route("/api/board/projects")
    def api_board_projects():
        """Return the current project board as JSON.

        Response: {board: [...], counts: {<effective_status>: <n>}}
        Read-only: reads the coordination store fresh, never writes.
        Degrades gracefully when gh is unavailable (gh_state='unknown',
        gh_unknown=True on affected items).
        503 when the coordination store is not configured (COORD_STORE unset) —
        matches the sibling /api/projects convention (#1608).
        """
        store = app.config.get("COORD_STORE")
        if store is None:
            return jsonify({"error": "coordination store not configured"}), 503

        try:
            items = read_project_board(store)
        except Exception as exc:  # noqa: BLE001
            print(f"[forum] api_board_projects error: {exc}", file=sys.stderr)
            items = []
        counts = get_board_counts(items)
        # Serialize: strip internal datetime objects (turn_since is already a string).
        return jsonify({"board": items, "counts": counts})

    # ------------------------------------------------------------------
    # GET /api/board/updates?since=<seq>[&agent=<name>]
    #
    # Returns only items whose `seq` cursor key is strictly greater than
    # `since`. Designed for Monitor polling at ~2s.
    # Response: {updates: [...], as_of: <int>}
    #
    # Cursor contract (#1608 — repointed from an ISO-8601/mtime cursor to a
    # seq cursor, mirroring the unified `/api/updates` feed built on the same
    # coordination store; see coordination/updates.py::build_updates and
    # coordination/seq.py::SeqAllocator.current()): the client echoes the
    # server-authoritative `as_of` back as the next `since`; `since` is
    # EXCLUSIVE. `as_of` = allocator.current() snapshotted BEFORE the read, so
    # a mutation that commits during the read window gets a seq above `as_of`
    # and is simply re-served on the next poll rather than silently dropped —
    # favouring a safe duplicate over a missed wake (the silent-miss class
    # Aleph raised in forum #166). Every served item is also upper-bounded to
    # `seq <= as_of` so the response is exactly the (since, as_of] window it
    # promises — a repeated poll on the same cursor is idempotent. The
    # deprecated `now` ISO-alias of `as_of` (announced deprecated-for-one-
    # release, pre-#1608) is retired here rather than repurposed onto an int,
    # since the cursor's very type is already changing in this PR.
    # ------------------------------------------------------------------
    @app.route("/api/board/updates")
    def api_board_updates():
        """Return project board items changed after `since`.

        Query params:
          since  — optional int seq cursor (EXCLUSIVE). Defaults to 0 (all);
                   negative clamps to 0. Echo back the response's `as_of`.
          agent  — If provided, only items whose turn == agent.

        Response: {updates: [...], as_of: <int>}

        Cursor correctness: two complementary properties. (1) `as_of` is
        `allocator.current()` captured BEFORE reading the board, so the
        client's next poll (since=as_of, exclusive) never misses an item
        committed during this read — it re-fires it instead (safe duplicate >
        silent miss). (2) The since-filter keys on each item's `seq` — the
        coordination store's module-assigned, monotonically increasing
        sequence number assigned co-atomically with the write that committed
        it (fork-4) — NOT a filesystem mtime or the writer-stamped turn_since
        (the pre-#1608 mechanism, which could still silent-miss per #1445).
        Items with seq <= since (or seq > as_of) are excluded.
        503 when the coordination store is not configured.
        """
        store = app.config.get("COORD_STORE")
        allocator = app.config.get("COORD_ALLOCATOR")
        if store is None or allocator is None:
            return jsonify({"error": "coordination store not configured"}), 503

        agent = request.args.get("agent", "").strip() or None

        try:
            since = int(request.args.get("since", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "since must be an integer"}), 400
        if since < 0:
            since = 0

        # Capture the served watermark BEFORE the read — see the contract note
        # above (mirrors coordination/updates.py::build_updates).
        as_of = allocator.current()

        try:
            items = read_project_board(store)
        except Exception as exc:  # noqa: BLE001
            print(f"[forum] api_board_updates error: {exc}", file=sys.stderr)
            items = []

        # Upper-bound to seq <= as_of so the served set is exactly the
        # (since, as_of] window the response promises (idempotent re-poll).
        items = [i for i in items if i["seq"] <= as_of]
        updates = filter_updates(items, since, agent)
        return jsonify({"updates": updates, "as_of": as_of})

    # ------------------------------------------------------------------
    # DM private channel  (UCS Slice D)
    #
    # Three endpoints:
    #   GET  /api/dm                       — list threads for ?agent=
    #   GET  /api/dm/<counterpart>         — read thread with ?agent=
    #   POST /api/dm/<counterpart>         — send DM (JSON body)
    #
    # Both store (COORD_STORE) and allocator (COORD_ALLOCATOR) are
    # injected via app.config by the caller (or the concrete FileStore
    # wiring — Slice E). When neither is configured, all three endpoints
    # return 503 so the server stays healthy without the coordination store.
    # ------------------------------------------------------------------

    @app.route("/api/dm", methods=["GET"])
    def api_dm_list():
        """List DM threads for the requesting agent.

        Query params:
            agent  — required; the requesting agent name.

        Response: {threads: [{counterpart}], agent: <name>}
        ACL: returns only threads where agent is one of the pair.
        """
        from forum.coordination import dm_list as _dm_list

        store = app.config.get("COORD_STORE")
        if store is None:
            return jsonify({"error": "coordination store not configured"}), 503

        agent = (request.args.get("agent") or "").strip().lower()
        if not agent:
            return jsonify({"error": "agent is required"}), 400
        if not db.is_valid_agent_name(agent):
            return jsonify({"error": "invalid agent name"}), 400

        counterparts = _dm_list(store, agent)
        threads = [{"counterpart": c} for c in counterparts]
        return jsonify({"threads": threads, "agent": agent})

    @app.route("/api/dm/<counterpart>", methods=["GET"])
    def api_dm_read(counterpart: str):
        """Read the DM thread between agent and counterpart.

        Query params:
            agent      — required; must be one of the pair.
            since_seq  — optional int cursor (exclusive). Defaults to 0 (all messages).

        Response: {messages: [{seq, sender, recipient, body, ts}], as_of_seq: <int>}
        ``as_of_seq`` is the per-thread high-water mark (last returned message seq,
        or ``since_seq`` when no messages match) — use it as the next ``since_seq``
        for incremental reads. It is NOT the global feed cursor (``allocator.current()``)
        and must not be wired into the unified ``/api/updates`` monitor.
        ACL: agent must be one of {agent, counterpart} — enforced at the store layer
             (read_dm_thread is order-independent; only the pair's messages are returned).
        """
        from forum.coordination import dm_read as _dm_read

        store = app.config.get("COORD_STORE")
        if store is None:
            return jsonify({"error": "coordination store not configured"}), 503

        agent = (request.args.get("agent") or "").strip().lower()
        if not agent:
            return jsonify({"error": "agent is required"}), 400

        counterpart_n = counterpart.strip().lower()
        if not db.is_valid_agent_name(agent) or not db.is_valid_agent_name(counterpart_n):
            return jsonify({"error": "invalid agent name"}), 400

        since_seq_raw = (request.args.get("since_seq") or "0").strip()
        try:
            since_seq = int(since_seq_raw)
        except ValueError:
            return jsonify({"error": "since_seq must be an integer"}), 400

        messages = _dm_read(store, agent, counterpart_n, since_seq=since_seq)
        as_of_seq = messages[-1].seq if messages else since_seq
        return jsonify({
            "messages": [
                {
                    "seq": m.seq,
                    "sender": m.sender,
                    "recipient": m.recipient,
                    "body": m.body,
                    "ts": m.ts,
                }
                for m in messages
            ],
            "as_of_seq": as_of_seq,
        })

    @app.route("/api/dm/<counterpart>", methods=["POST"])
    def api_dm_send(counterpart: str):
        """Send a DM from agent to counterpart.

        JSON body: {"agent": "<sender>", "body": "<message text>"}
        Trust model: ``agent`` is client-supplied and not cryptographically
        verified — the same honor-system trust as the rest of the forum
        (``/api/post`` uses the same pattern). DM privacy is LAN-scoped;
        hardenable when forum-wide auth lands (issue #1459).

        Response: {seq: <int>, ts: <str>}
        """
        from forum.coordination import dm_send as _dm_send

        store = app.config.get("COORD_STORE")
        allocator = app.config.get("COORD_ALLOCATOR")
        if store is None or allocator is None:
            return jsonify({"error": "coordination store not configured"}), 503

        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        _require_str("POST /api/dm/<counterpart>", "agent", data.get("agent"))  # #3436: before any read / write
        agent = (data.get("agent") or "").strip().lower()
        if not agent:
            return jsonify({"error": "agent is required"}), 400

        _require_str("POST /api/dm/<counterpart>", "body", data.get("body"))  # #3436
        body = (data.get("body") or "").strip()
        if not body:
            return jsonify({"error": "body is required"}), 400
        _require_utf8("POST /api/dm/<counterpart>", "body", body)

        counterpart_n = counterpart.strip().lower()
        if not db.is_valid_agent_name(agent) or not db.is_valid_agent_name(counterpart_n):
            return jsonify({"error": "invalid agent name"}), 400

        msg = _dm_send(store, allocator, agent, counterpart_n, body)
        return jsonify({"seq": msg.seq, "ts": msg.ts}), 201

    # ------------------------------------------------------------------
    # Unified updates feed (UCS Slice B) — GET /api/updates
    # ------------------------------------------------------------------
    @app.route("/api/updates", methods=["GET"])
    def api_updates():
        """Unified wake-cursor feed — the relevance-filtered update union for an agent.

        Query params:
            agent  — required; the recipient. Validated against the charset guard.
            since  — optional int cursor (EXCLUSIVE). Defaults to 0 (all);
                     negative clamps to 0.
            kinds  — optional comma-separated narrowing (Phase-1 kinds: dm, baton).

        Response: ``{"updates": [{kind, seq, wake, …}], "as_of": <int>, "ts": <str>}``.
        ``as_of`` = the served watermark (``allocator.current()``; may LEGITIMATELY
        freeze when there are no new commits — a frozen ``as_of`` is not a dead feed).
        ``ts`` = the server clock for this request, the LIVENESS signal: the consumer
        keys dead-feed detection on ``ts``-advance / staleness (plus non-200), NOT on
        ``as_of``-advance. ``since`` exclusive → re-polling the same cursor is
        idempotent (no duplicate replay). See spec §3 + ``coordination/updates.py``.
        """
        from forum.coordination import build_updates

        store = app.config.get("COORD_STORE")
        allocator = app.config.get("COORD_ALLOCATOR")
        if store is None or allocator is None:
            return jsonify({"error": "coordination store not configured"}), 503

        agent = (request.args.get("agent") or "").strip().lower()
        if not agent:
            return jsonify({"error": "agent is required"}), 400
        if not db.is_valid_agent_name(agent):
            return jsonify({"error": "invalid agent name"}), 400

        try:
            since = int(request.args.get("since", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "since must be an integer"}), 400
        if since < 0:
            since = 0

        kinds_arg = request.args.get("kinds")
        kinds = None
        if kinds_arg:
            kinds = [k.strip().lower() for k in kinds_arg.split(",") if k.strip()]

        return jsonify(build_updates(store, allocator, agent, since=since, kinds=kinds))

    # #3039 U2b round 4: refusals raised from the shared request reader, the
    # write-layer backstop and the duplicate-key guard all become a JSON error with
    # the status the exception carries -- never a 500, never a write.
    @app.errorhandler(_RequestRefusal)
    def _handle_request_refusal(exc):
        return jsonify({"error": exc.message}), exc.status

    @app.errorhandler(_md.UnsafeText)
    def _handle_unsafe_text(exc):
        return jsonify({"error": str(exc)}), 422

    @app.errorhandler(_md.DuplicateFrontmatterKeys)
    def _handle_duplicate_keys(exc):
        return jsonify({"error": str(exc)}), 422

    # ------------------------------------------------------------------
    # Project / board coordination routes (UCS #1494 Phase 1c+2)
    #   GET  /api/projects                     — list all project records
    #   GET  /api/projects/<pid>               — single project record
    #   POST /api/projects                     — init (create) a new baton
    #   POST /api/projects/<pid>/flip          — flip turn
    #   POST /api/projects/<pid>/claim         — claim from pool
    #   POST /api/projects/<pid>/release       — release back to pool
    #   POST /api/projects/<pid>/status        — close or reopen
    #   POST /api/projects/<pid>/rename        — rename title
    #   POST /api/projects/<pid>/anchor        — set/update github anchor
    #   POST /api/projects/<pid>/gc            — gc-close (client calls after gh query)
    #   POST /api/projects/<pid>/merge         — post-merge closure + archive
    #
    # All mutation routes require COORD_STORE + COORD_ALLOCATOR; return 503 when
    # not configured. Read routes only need COORD_STORE.
    # ------------------------------------------------------------------

    @app.route("/api/projects", methods=["GET"])
    def api_projects_list():
        """List project records.

        Query params:
            agent       — optional; filter to projects where agent is a participant.
            active_only — optional bool string (true/false/1/0); default true.

        Response: {projects: [{project_id, title, status, turn, turn_since, turn_reason,
                               participants, seq, github}]}
        """
        from forum.coordination import projects as _proj

        store = app.config.get("COORD_STORE")
        if store is None:
            return jsonify({"error": "coordination store not configured"}), 503

        active_raw = (request.args.get("active_only") or "true").strip().lower()
        active_only = active_raw not in ("false", "0", "no")
        agent = (request.args.get("agent") or "").strip().lower()

        records = store.read_projects(active_only=active_only)
        if agent:
            records = [r for r in records if agent in r.participants]

        return jsonify({
            "projects": [
                {
                    "project_id": r.project_id,
                    "title": r.title,
                    "status": r.status,
                    "turn": r.turn,
                    "turn_since": r.turn_since,
                    "turn_reason": r.turn_reason,
                    "participants": list(r.participants),
                    "seq": r.seq,
                    "github": r.github,
                }
                for r in records
            ]
        })

    @app.route("/api/projects/<pid>", methods=["GET"])
    def api_project_show(pid: str):
        """Return a single project's raw markdown + parsed fields.

        Response: {project_id, raw} where raw is the full markdown content.
        Returns 404 when not found.
        """
        store = app.config.get("COORD_STORE")
        if store is None:
            return jsonify({"error": "coordination store not configured"}), 503

        raw = store.read_project(pid)
        if raw is None:
            return jsonify({"error": f"project not found: {pid}"}), 404

        return jsonify({"project_id": pid, "raw": raw})

    @app.route("/api/projects", methods=["POST"])
    def api_project_init():
        """Create a new project baton.

        JSON body: {agent, project_id, title, status, turn, participants, turn_reason, github?, colleague?, author?, phase?, transition_phase?}
        ``participants`` may be a list of strings OR a comma-separated string.
        ``phase?`` / ``transition_phase?`` (#3039 U2a round 3): optional,
        client-computed, NOT trusted (U2b r2: the server re-derives it) -- no validation/interpretation
        here, same trust boundary as ``pool_sentinel`` on the claim/release
        endpoints below. ``init`` is always a transition (the baton's very
        first phase), so the client sends ``transition_phase`` here, not
        ``phase`` -- both are threaded through regardless, for uniformity
        with the other endpoints.
        Response: {seq, project_id}, 201.
        Errors: 400 (missing/invalid fields), 409 (already exists), 503 (store not configured).
        """
        from forum.coordination import init as _init, ProjectAlreadyExists

        store = app.config.get("COORD_STORE")
        allocator = app.config.get("COORD_ALLOCATOR")
        if store is None or allocator is None:
            return jsonify({"error": "coordination store not configured"}), 503

        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        # #3039 U2b round 4: every field through the shared reader (identifiers
        # with a line-break/control char => 422, free text normalised, non-strings
        # => 422) BEFORE anything else looks at the request.
        f = _read_fields("POST /api/projects (init)", data, {
            "agent": "idl", "project_id": "id", "title": "text", "status": "id",
            "turn": "idl", "turn_reason": "text", "github": "id", "colleague": "id",
            "author": "id", "phase": "idl", "transition_phase": "idl",
            "participants": "list",
        })
        agent = f["agent"]
        if not agent or not db.is_valid_agent_name(agent):
            return jsonify({"error": "agent is required and must be valid"}), 400

        # #3286: the id becomes a file name under the store -- hold it to the CLI's
        # rule (fullmatch, on the value EXACTLY as sent: no strip, no case-fold)
        # before anything else (or the store) looks at it. Absent / null keeps the
        # 400 "required" answer below; a present value (including "") that is not
        # CLI-shaped, or not a string, is a 422.
        raw_project_id = data.get("project_id")
        if raw_project_id is not None and not _is_valid_project_id(raw_project_id):
            return jsonify({"error": (
                "POST /api/projects (init): field project_id must be a string like TYPE-ID: "
                "start with a letter, use only letters, digits, '-' and '_', and contain a "
                "hyphen separating type from id (e.g. PR-425)")}), 422
        project_id = raw_project_id or ""
        title = f["title"]
        status = f["status"]
        turn = f["turn"]
        turn_reason = f["turn_reason"]
        github = f["github"] or None
        colleague = f["colleague"] or None
        author = f["author"] or None
        # #3039 U2a round 3: optional, NOT trusted (U2b r2: the server re-derives it) -- the client
        # (baton.py) computes this baton's intended initial phase and sends
        # it under transition_phase (init is always a transition); this
        # endpoint does no validation/interpretation, same trust boundary as
        # pool_sentinel below. phase (conversion) is threaded through too,
        # for API uniformity, though a client should never need it here.
        phase = f["phase"] or None
        transition_phase = f["transition_phase"] or None
        participants = f["participants"]

        if not project_id or not title or not status or not turn or not turn_reason or not participants:
            return jsonify({"error": "project_id, title, status, turn, turn_reason, and participants are required"}), 400

        # #3039 U2b round 2: no request body supplies a phase. A brand-new
        # baton has no file to derive a conversion from, so a client
        # ``phase`` is dropped (``merge`` is a 422); ``transition_phase`` is
        # the baton's INITIAL phase, allow-listed by PR-vs-bunch shape.
        route = "POST /api/projects (init)"
        if phase == "merge":
            msg, st = _phase_refusal(route, project_id, "phase", phase, "a new baton has no phase to convert")
            return jsonify({"error": msg}), st
        if phase:
            logger.warning("%s %s: ignoring client conversion phase %r", route, project_id, phase)
        phase = None
        #
        # Round 5 (B1): the SERVER stamps the initial phase itself on EVERY init.
        # A phase-less PR baton whose turn is the pool sentinel reads as the
        # LEGACY ``merge`` (``gate._resolve_phase``) -- a rule meant only for files
        # that predate phase stamping -- so a client that simply omitted
        # ``transition_phase`` got a baton the strict gate then saw as already
        # merge-ready, with no approval anywhere. No new baton is ever phase-less.
        gate = _load_baton_gate()
        if gate is None:
            if transition_phase is None:
                return jsonify({"error": (
                    f"{route} {project_id}: phase gate unavailable (server could not load its "
                    "gate module) -- cannot decide PR-vs-bunch to stamp the baton's initial "
                    "phase, and a phase-less baton is never created. See server logs.")}), 422
            # #3366: fail closed here too. Without the gate we cannot classify, but a
            # PR-ish id (local copy of the name rule; the gate is deliberately not
            # imported) with no usable anchor is exactly the baton this rule refuses.
            _g_chk = (github or "").strip()
            if (_PR_ISH_ID_RE.match(project_id) and not project_id.lower().startswith("pool-")
                    and not _ANCHOR_SHAPE_RE.fullmatch(_g_chk.lower())):
                return jsonify({"error": _anchorless_pr_refusal(route, project_id, _g_chk)}), 422
            # #3377: ONE rule for every id -- a PRESENT github must parse (same local
            # anchor-shape check, the gate is deliberately not imported here).
            if _g_chk and not _ANCHOR_SHAPE_RE.fullmatch(_g_chk.lower()):
                return jsonify({"error": _unparseable_github_refusal(route, project_id, _g_chk)}), 422
            allowed = _INIT_PHASES_PR | _INIT_PHASES_BUNCH
            default_phase = None
        elif gate._is_pr_shaped(project_id, (github or "").strip().lower()):
            # #3366: a PR-shaped baton must name its PR. `_is_pr_shaped` is
            # anchor-first, so when the github value does not PARSE (empty, or junk
            # like 'garbage' / 'pr/') it falls back to the id -- PR-shaped by the id
            # alone, "no anchor resolved". Such a baton would be stamped `fairy`
            # with nothing the peer->merge gate can verify (the #2916 dead end). The
            # server is the authority; it derives nothing from the id (the CLI's
            # #3364 refusal is the early friendly copy of this rule). No bypass: no
            # request field skips it. Runs BEFORE the transition_phase allow-list and
            # before anything is written. Creation only.
            _g_chk = (github or "").strip()
            if gate._parse_github_anchor(_g_chk.lower()) is None:
                return jsonify({"error": _anchorless_pr_refusal(route, project_id, _g_chk)}), 422
            allowed = _INIT_PHASES_PR
            default_phase = "fairy"
        else:
            # #3377: ONE rule for every id (Kepler): a PRESENT github must parse, not
            # only on a PR-shaped id. An absent / empty / blank github is a legitimate
            # anchorless bunch (`github` is None here after the shared reader). Like the
            # PR-shaped refusal this runs BEFORE the transition_phase allow-list and
            # before anything is written; no request field bypasses it.
            _g_chk = (github or "").strip()
            if _g_chk and gate._parse_github_anchor(_g_chk.lower()) is None:
                return jsonify({"error": _unparseable_github_refusal(route, project_id, _g_chk)}), 422
            allowed = _INIT_PHASES_BUNCH
            default_phase = "pool"
        if transition_phase is None:
            transition_phase = default_phase
        elif transition_phase not in allowed:
            msg, st = _phase_refusal(route, project_id, "transition_phase", transition_phase,
                                     "allowed here: " + ", ".join(sorted(allowed)))
            return jsonify({"error": msg}), st

        # #3377 fold (Ariadne, #3384): STORE what was validated. Every check above (and
        # the PR-shaped / "present must parse" refusals) judged the stripped + lowercased
        # value, but `_parse_github_anchor` is case-sensitive and the readers (gc, flip,
        # board, CLI) parse the STORED value without lowercasing -- so a raw 'Project/7'
        # passed validation and was stored as an anchor they read as "no anchor". All three
        # branches (gate PR-shaped, gate non-PR, gate unavailable) reach this one line. An
        # unqualified pr/<N> is still rewritten to pr/<owner>/<repo>/<N> below.
        github = (github or "").strip().lower() or None
        # Round 6 (S5 + D2): the anchor is checked/qualified BEFORE anything is written.
        g_norm = (github or "").strip().lower()
        if g_norm:
            tail = g_norm.rsplit("/", 1)[-1]
            if (re.fullmatch(r"(?:pr/(?:[\w.-]+/[\w.-]+/)?|project/)[0-9]+", g_norm)
                    and len(tail) > 15):
                # S5: the shared anchor regex caps the number at 15 ASCII digits; init
                # used to store a 5000-digit one that later 500ed or stuck the baton.
                return jsonify({"error": (
                    f"{route} {project_id}: github anchor number has {len(tail)} digits; "
                    "at most 15 are accepted.")}), 422
            if gate is None:
                unqualified = re.fullmatch(r"pr/[0-9]{1,15}", g_norm) is not None
                parsed_g = None
            else:
                parsed_g = gate._parse_github_anchor(g_norm)
                unqualified = (parsed_g is not None and parsed_g[0] == "pr"
                               and parsed_g[1] is None)
            if unqualified:
                # D2: an unqualified pr/<N> is qualified from the ADMIN map by the id's
                # prefix (never from the request), or refused -- a baton is never
                # created whose anchor the gate cannot bind to a repo.
                want = None if gate is None else _bound_repo(gate, project_id, _login_map_cfg(app))
                m_id = None if gate is None else gate._ID_BIND_RE.fullmatch(project_id)
                if want is None or m_id is None:
                    return jsonify({"error": (
                        f"{route} {project_id}: the unqualified anchor {g_norm!r} cannot be "
                        "qualified -- the baton id's prefix has no entry in the baton login "
                        "map's 'repos' table (or no map / gate is available). Send "
                        "pr/<owner>/<repo>/<N>, or ask the admin to map the prefix.")}), 422
                github = f"pr/{want}/{parsed_g[2]}"
            # Round 9 (B1): the id binding (number, and the mapped repo) is checked
            # where the anchor is WRITTEN, for qualified AND unqualified anchors,
            # so a baton is never created whose anchor advance would refuse.
            bind_msg = _anchor_write_binding_refusal(
                gate, project_id,
                gate is not None and gate._is_pr_shaped(project_id, g_norm),
                g_norm, _login_map_cfg(app))
            if bind_msg is not None:
                return jsonify({"error": f"{route} {project_id}: {bind_msg}"}), 422

        try:
            seq = _init(
                store, allocator, project_id,
                title=title, status=status, turn=turn,
                participants=participants, turn_reason=turn_reason,
                by_agent=agent, github=github, colleague=colleague,
                author=author, phase=phase, transition_phase=transition_phase,
            )
        except ProjectAlreadyExists as exc:
            if exc.existing_ids == [project_id]:
                return jsonify({"error": f"project already exists: {project_id}"}), 409
            # #3432: a case variant. Name the requested id AND every colliding
            # existing id (one line) so a seat can pick the live one.
            named = ", ".join(f"'{e}'" for e in exc.existing_ids)
            return jsonify({"error": (
                f"project already exists (case-insensitive match): "
                f"requested '{project_id}', existing {named}")}), 409
        except OSError as exc:
            # A CLI-shaped id has no length cap, so a very long one fails at the
            # filesystem (ENAMETOOLONG): that is a bad id (422), not a server fault.
            if exc.errno == errno.ENAMETOOLONG:
                return jsonify({"error": "POST /api/projects (init): field project_id is too long to be a file name"}), 422
            raise

        return jsonify({"seq": seq, "project_id": project_id}), 201

    @app.route("/api/projects/<pid>/flip", methods=["POST"])
    def api_project_flip(pid: str):
        """Flip a baton's turn.

        JSON body: {agent, to_agent, reason, phase?, transition_phase?}
        ``phase?`` / ``transition_phase?`` (#3039 U2a round 3): optional,
        client-computed, NOT trusted (U2b r2: the server re-derives it) -- see api_project_init's
        docstring note. The two transition-defining flip branches send
        BOTH (round 4: ``phase`` = the legacy-conversion value, applied
        first, then ``transition_phase`` overwrites); conversion-only flips
        send ``phase`` alone.
        Response: {seq, turn, status}, 201.

        Gate (#2884, second half): when the current project state + this
        flip's `to_agent` represent a maintainer-role flip on a
        repo-qualified PR-anchored baton, this mirrors tools/baton.py
        cmd_flip's own CI-green / post-approval-freshness /
        colleague-approval gate (see _check_flip_gate above) -- so a
        client calling this endpoint directly (bypassing the CLI) can no
        longer skip those checks. No --force equivalent (not an
        interactive CLI). A self-flip or a flip to the registered
        colleague (review-stage, not merge-stage) is never gated, matching
        cmd_flip. If the gate module itself cannot be loaded, a flip on a
        pr/-anchored baton is refused rather than silently let through --
        see _load_baton_gate's docstring for the known deployment gap this
        guards against.
        Errors: 400 (missing/invalid fields), 404 (project not found),
                422 (gate refusal: unqualified anchor, CI red/pending,
                stale approval, missing colleague approval, or gate
                module/sentinel unavailable), 503 (store not configured).
        """
        from forum.coordination import flip as _flip, ProjectNotFound

        store = app.config.get("COORD_STORE")
        allocator = app.config.get("COORD_ALLOCATOR")
        if store is None or allocator is None:
            return jsonify({"error": "coordination store not configured"}), 503

        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        f = _read_fields("POST /api/projects/<pid>/flip", data, {
            "agent": "idl", "to_agent": "idl", "reason": "text",
            "phase": "idl", "transition_phase": "idl",
        }, pid=pid)
        agent = f["agent"]
        if not agent or not db.is_valid_agent_name(agent):
            return jsonify({"error": "agent is required and must be valid"}), 400

        to_agent = f["to_agent"]
        reason = f["reason"]
        if not to_agent or not reason:
            return jsonify({"error": "to_agent and reason are required"}), 400
        # #3039 U2a round 3: optional, NOT trusted (U2b r2: the server re-derives it) -- see
        # api_project_init's note.
        phase = f["phase"] or None
        transition_phase = f["transition_phase"] or None

        advance_ticket: dict = {}
        raw_phase, raw_transition = phase, transition_phase
        gate_refusal = _check_flip_gate(
            store, pid, to_agent,
            phase=phase, transition_phase=transition_phase,
            login_map_cfg=_login_map_cfg(app), ticket_out=advance_ticket,
        )
        if gate_refusal is not None:
            message, gate_status = gate_refusal
            return jsonify({"error": message}), gate_status

        if advance_ticket:
            # D1: this flip IS the peer -> merge transition and passed the strict
            # gate; write it through the same check-and-set body `advance` uses
            # (the client's own phase fields are ignored -- the server decided).
            body_out, status_out = _perform_advance(
                _load_baton_gate(), store, allocator, pid,
                to_agent=to_agent, reason=reason, agent=agent, ticket=advance_ticket,
            )
            return jsonify(body_out), status_out

        # #3039 U2b round 2: the weak-gate path writes only phases the SERVER
        # derived from the baton; a client transition it does not itself
        # expect (notably ``merge``) is a 422, nothing written.
        phase, transition_phase, phase_refusal = _flip_phase_inputs(
            "POST /api/projects/<pid>/flip", pid, store, to_agent, raw_phase, raw_transition)
        if phase_refusal is not None:
            return jsonify({"error": phase_refusal[0]}), phase_refusal[1]

        try:
            seq = _flip(
                store, allocator, pid, to_agent=to_agent, reason=reason,
                by_agent=agent, phase=phase, transition_phase=transition_phase,
            )
        except ProjectNotFound:
            return jsonify({"error": f"project not found: {pid}"}), 404

        turn, status = _read_project_state(store, pid)
        return jsonify({"seq": seq, "turn": turn, "status": status}), 201

    @app.route("/api/projects/<pid>/advance", methods=["POST"])
    def api_project_advance(pid: str):
        """Advance a PR baton ``phase: peer -> phase: merge`` (#3039 U2b).

        JSON body: {agent, to_agent, reason?}. ``to_agent`` is the merge seat
        (a participant or the pool sentinel). There is no force / skip field:
        the body is never consulted for anything that could bypass the gate.

        The strict gate (:func:`_advance_gate`) is SERVER-authoritative: the
        baton must be a repo-anchored PR baton in phase ``peer`` with a
        ``colleague`` whose mapped GitHub login has a CURRENT ``APPROVED``
        review on the PR head (still that login's latest opinionated review),
        CI must not be red/pending, and the login map must load from its
        protected path. The same function judges a raw ``POST .../flip`` that
        resolves as this transition.

        Response 201 ``{advanced: true, seq, turn, status, phase, ci, approval,
        warnings}``; 200 ``{advanced: false, reason: "already merge", ...}`` on
        the idempotent no-op (no seq, no turn-log line, no wake).
        Errors: 400 (fields), 404, 409 (the baton moved between the GitHub
        check and the write; names the actual phase), 422 (gate refusal),
        503 (store not configured).

        Honest limit: the GitHub verdict and the forum write are not one
        cross-system transaction -- a CHANGES_REQUESTED landing between them
        still advances (window: one HTTP round trip). And this gate proves an
        authenticated non-author approval by a MAPPED account on the current
        head; it does not prove the ASSIGNED colleague approved (the
        ``colleague`` and ``agent`` fields are caller-declared), nor who ran
        the verb.
        """
        store = app.config.get("COORD_STORE")
        allocator = app.config.get("COORD_ALLOCATOR")
        if store is None or allocator is None:
            return jsonify({"error": "coordination store not configured"}), 503

        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400
        f = _read_fields("POST /api/projects/<pid>/advance", data, {
            "agent": "idl", "to_agent": "idl", "reason": "text",
        }, pid=pid)
        agent = f["agent"]
        if not agent or not db.is_valid_agent_name(agent):
            return jsonify({"error": "agent is required and must be valid"}), 400
        to_agent = f["to_agent"]
        if not to_agent:
            return jsonify({"error": "to_agent (the merge seat) is required"}), 400
        reason = f["reason"] or "colleague-APPROVED; ready for merge"

        raw = store.read_project(pid)
        if raw is None:
            return jsonify({"error": f"project not found: {pid}"}), 404
        gate = _load_baton_gate()
        if gate is None:
            return jsonify({"error": (
                f"flip gate unavailable for {pid} (server could not load its "
                "CI/approval gate module) -- refusing to advance without being able "
                "to verify approval state. See server logs.")}), 422
        from forum.coordination.markdown import parse_frontmatter
        fields, body = parse_frontmatter(raw)
        refusal, ticket = _advance_gate(gate, pid, fields, body, to_agent, _login_map_cfg(app), raw=raw)
        if refusal is not None:
            message, gate_status = refusal
            return jsonify({"error": message}), gate_status
        body_out, status_out = _perform_advance(
            gate, store, allocator, pid,
            to_agent=to_agent, reason=reason, agent=agent, ticket=ticket,
        )
        return jsonify(body_out), status_out

    @app.route("/api/projects/<pid>/claim", methods=["POST"])
    def api_project_claim(pid: str):
        """Claim a project baton from the pool.

        JSON body: {agent, pool_sentinel, phase?, transition_phase?}
        ``phase?`` / ``transition_phase?`` (#3039 U2a round 3): optional,
        client-computed, NOT trusted (U2b r2: the server re-derives it) -- see api_project_init's
        docstring note. ``claim`` is always a transition, so the client
        sends ``transition_phase`` here -- together with ``phase`` (the
        legacy-conversion value, round 4), applied first.
        Response: {seq, turn, status}, 201.
        """
        from forum.coordination import claim as _claim, ProjectNotFound

        store = app.config.get("COORD_STORE")
        allocator = app.config.get("COORD_ALLOCATOR")
        if store is None or allocator is None:
            return jsonify({"error": "coordination store not configured"}), 503

        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        f = _read_fields("POST /api/projects/<pid>/claim", data, {
            "agent": "idl", "pool_sentinel": "idl", "phase": "idl", "transition_phase": "idl",
        }, pid=pid)
        agent = f["agent"]
        if not agent or not db.is_valid_agent_name(agent):
            return jsonify({"error": "agent is required and must be valid"}), 400

        pool_sentinel = f["pool_sentinel"]
        if not pool_sentinel:
            return jsonify({"error": "pool_sentinel is required"}), 400
        # #3039 U2a round 3: optional, NOT trusted (U2b r2: the server re-derives it) -- see
        # api_project_init's note.
        phase = f["phase"] or None
        transition_phase = f["transition_phase"] or None

        phase, transition_phase, phase_refusal = _bunch_verb_inputs(
            "POST /api/projects/<pid>/claim", pid, store, phase, transition_phase, _CLAIM_PHASES)
        if phase_refusal is not None:
            return jsonify({"error": phase_refusal[0]}), phase_refusal[1]

        try:
            seq = _claim(
                store, allocator, pid, claimer=agent, pool_sentinel=pool_sentinel,
                phase=phase, transition_phase=transition_phase,
            )
        except ProjectNotFound:
            return jsonify({"error": f"project not found: {pid}"}), 404

        turn, status = _read_project_state(store, pid)
        return jsonify({"seq": seq, "turn": turn, "status": status}), 201

    @app.route("/api/projects/<pid>/release", methods=["POST"])
    def api_project_release(pid: str):
        """Release a project baton back to the pool.

        JSON body: {agent, pool_sentinel, reason, done?, phase?, transition_phase?}
        ``done`` (bool, default false) appends "(done)" to the project title.
        ``phase?`` / ``transition_phase?`` (#3039 U2a round 3): optional,
        client-computed, NOT trusted (U2b r2: the server re-derives it) -- see api_project_init's
        docstring note. ``release`` is always a transition, so the client
        sends ``transition_phase`` here -- together with ``phase`` (the
        legacy-conversion value, round 4), applied first.
        Response: {seq, turn, status}, 201.
        """
        from forum.coordination import release as _release, ProjectNotFound

        store = app.config.get("COORD_STORE")
        allocator = app.config.get("COORD_ALLOCATOR")
        if store is None or allocator is None:
            return jsonify({"error": "coordination store not configured"}), 503

        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        f = _read_fields("POST /api/projects/<pid>/release", data, {
            "agent": "idl", "pool_sentinel": "idl", "reason": "text", "done": "bool",
            "phase": "idl", "transition_phase": "idl",
        }, pid=pid)
        agent = f["agent"]
        if not agent or not db.is_valid_agent_name(agent):
            return jsonify({"error": "agent is required and must be valid"}), 400

        pool_sentinel = f["pool_sentinel"]
        reason = f["reason"]
        if not pool_sentinel or not reason:
            return jsonify({"error": "pool_sentinel and reason are required"}), 400

        done = f["done"]
        # #3039 U2a round 3: optional, NOT trusted (U2b r2: the server re-derives it) -- see
        # api_project_init's note.
        phase = f["phase"] or None
        transition_phase = f["transition_phase"] or None

        phase, transition_phase, phase_refusal = _bunch_verb_inputs(
            "POST /api/projects/<pid>/release", pid, store, phase, transition_phase,
            _RELEASE_PHASES, fallback="claimed")
        if phase_refusal is not None:
            return jsonify({"error": phase_refusal[0]}), phase_refusal[1]

        try:
            seq = _release(
                store, allocator, pid, holder=agent, pool_sentinel=pool_sentinel,
                reason=reason, done=done, phase=phase, transition_phase=transition_phase,
            )
        except ProjectNotFound:
            return jsonify({"error": f"project not found: {pid}"}), 404

        turn, status = _read_project_state(store, pid)
        return jsonify({"seq": seq, "turn": turn, "status": status}), 201

    @app.route("/api/projects/<pid>/status", methods=["POST"])
    def api_project_status(pid: str):
        """Close or reopen a project baton (dispatches on new_status).

        JSON body: {agent, new_status, reason, phase?}
        Dispatch: if new_status in (planning, in-progress, in-review) → reopen;
                  otherwise → close.
        ``phase?`` (#3039 U2a round 3): optional, client-computed CONVERSION
        phase, NOT trusted (U2b r2: the server re-derives it) -- see api_project_init's docstring note.
        Neither ``close`` nor ``reopen`` ever writes a transition phase
        (Kepler's ruling), so there is no ``transition_phase`` field here.
        Response: {seq, turn, status}, 201.
        Errors (#3313; the gates live in the coordination write-fns, so a direct
        call into them cannot skip them; ``agent`` is caller-declared, so the
        403 stops a mistaken or misdirected write, not a caller who names a
        participant; checked in this order, and a
        refusal writes nothing -- no frontmatter change, no turn-log line, no
        seq consumed): 400 (missing/invalid fields), 404 (project not found),
        403 (``agent`` is not a current participant of the baton; the
        maintainer/sentinel counts only if listed in ``participants``),
        409 (close-direction write on a baton ALREADY closed -- names the
        current status; reopen of a closed baton is the legal path and is
        never refused with 409), 503 (store not configured).
        """
        from forum.coordination import (
            close as _close, reopen as _reopen, ProjectNotFound,
            NotAParticipant, AlreadyClosed,
        )

        store = app.config.get("COORD_STORE")
        allocator = app.config.get("COORD_ALLOCATOR")
        if store is None or allocator is None:
            return jsonify({"error": "coordination store not configured"}), 503

        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        f = _read_fields("POST /api/projects/<pid>/status", data, {
            "agent": "idl", "new_status": "idl", "reason": "text", "phase": "idl",
        }, pid=pid)
        agent = f["agent"]
        if not agent or not db.is_valid_agent_name(agent):
            return jsonify({"error": "agent is required and must be valid"}), 400

        new_status = f["new_status"]
        reason = f["reason"]
        if not new_status or not reason:
            return jsonify({"error": "new_status and reason are required"}), 400
        # #3039 U2a round 3: optional, NOT trusted (U2b r2: the server re-derives it) -- see
        # api_project_init's note.
        phase = f["phase"] or None

        _ACTIVE_STATUSES = frozenset({"planning", "in-progress", "in-review"})

        phase, phase_refusal = _conversion_only_inputs(
            "POST /api/projects/<pid>/status", pid, store, phase)
        if phase_refusal is not None:
            return jsonify({"error": phase_refusal[0]}), phase_refusal[1]

        try:
            if new_status in _ACTIVE_STATUSES:
                seq = _reopen(store, allocator, pid, invoker=agent, new_status=new_status, phase=phase)
            else:
                seq = _close(store, allocator, pid, invoker=agent, new_status=new_status,
                             reason=reason, phase=phase)
        except ProjectNotFound:
            return jsonify({"error": f"project not found: {pid}"}), 404
        except NotAParticipant:
            return jsonify({"error": f"agent {agent!r} is not a participant of {pid}"}), 403
        except AlreadyClosed as e:
            return jsonify({"error": f"project {pid} is already {e.status}; "
                            "refusing to overwrite a closed status (reopen first)"}), 409

        turn, status = _read_project_state(store, pid)
        return jsonify({"seq": seq, "turn": turn, "status": status}), 201

    @app.route("/api/projects/<pid>/rename", methods=["POST"])
    def api_project_rename(pid: str):
        """Rename a project baton's title.

        JSON body: {agent, new_title, phase?}
        ``phase?`` (#3039 U2a round 3): optional, client-computed CONVERSION
        phase, NOT trusted (U2b r2: the server re-derives it) -- see api_project_init's docstring note.
        ``rename`` never writes a transition phase (never a phase-machine
        edge), so there is no ``transition_phase`` field here.
        Response: {seq, turn, status}, 201.
        """
        from forum.coordination import rename as _rename, ProjectNotFound

        store = app.config.get("COORD_STORE")
        allocator = app.config.get("COORD_ALLOCATOR")
        if store is None or allocator is None:
            return jsonify({"error": "coordination store not configured"}), 503

        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        f = _read_fields("POST /api/projects/<pid>/rename", data, {
            "agent": "idl", "new_title": "text", "phase": "idl",
        }, pid=pid)
        agent = f["agent"]
        if not agent or not db.is_valid_agent_name(agent):
            return jsonify({"error": "agent is required and must be valid"}), 400

        new_title = f["new_title"]
        if not new_title:
            return jsonify({"error": "new_title is required"}), 400
        # #3039 U2a round 3: optional, NOT trusted (U2b r2: the server re-derives it) -- see
        # api_project_init's note.
        phase = f["phase"] or None

        phase, phase_refusal = _conversion_only_inputs(
            "POST /api/projects/<pid>/rename", pid, store, phase)
        if phase_refusal is not None:
            return jsonify({"error": phase_refusal[0]}), phase_refusal[1]

        try:
            seq = _rename(store, allocator, pid, new_title=new_title, phase=phase)
        except ProjectNotFound:
            return jsonify({"error": f"project not found: {pid}"}), 404

        turn, status = _read_project_state(store, pid)
        return jsonify({"seq": seq, "turn": turn, "status": status}), 201

    @app.route("/api/projects/<pid>/anchor", methods=["POST"])
    def api_project_anchor(pid: str):
        """Set or update the github anchor on a project baton.

        JSON body: {agent, github, phase?}
        ``phase?`` (#3039 U2a round 3): optional, client-computed CONVERSION
        phase, NOT trusted (U2b r2: the server re-derives it) -- see api_project_init's docstring note.
        ``anchor`` never writes a transition phase (never a phase-machine
        edge), so there is no ``transition_phase`` field here.
        Response: {seq, turn, status}, 201.
        """
        from forum.coordination import anchor as _anchor, ProjectNotFound

        store = app.config.get("COORD_STORE")
        allocator = app.config.get("COORD_ALLOCATOR")
        if store is None or allocator is None:
            return jsonify({"error": "coordination store not configured"}), 503

        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        f = _read_fields("POST /api/projects/<pid>/anchor", data, {
            "agent": "idl", "github": "id", "phase": "idl",
        }, pid=pid)
        agent = f["agent"]
        if not agent or not db.is_valid_agent_name(agent):
            return jsonify({"error": "agent is required and must be valid"}), 400

        github_anchor = f["github"]
        if not github_anchor:
            return jsonify({"error": "github is required"}), 400
        # #3039 U2a round 3: optional, NOT trusted (U2b r2: the server re-derives it) -- see
        # api_project_init's note.
        phase = f["phase"] or None

        # Round 4 (A1 + A3): write-once after `fairy`, and a CLI-sendable shape.
        anchor_refusal = _anchor_refusal("POST /api/projects/<pid>/anchor", pid, store, github_anchor,
                                         _login_map_cfg(app))
        if anchor_refusal is not None:
            return jsonify({"error": anchor_refusal[0]}), anchor_refusal[1]

        phase, phase_refusal = _conversion_only_inputs(
            "POST /api/projects/<pid>/anchor", pid, store, phase)
        if phase_refusal is not None:
            return jsonify({"error": phase_refusal[0]}), phase_refusal[1]

        try:
            seq = _anchor(store, allocator, pid, github_anchor=github_anchor, phase=phase)
        except ProjectNotFound:
            return jsonify({"error": f"project not found: {pid}"}), 404

        turn, status = _read_project_state(store, pid)
        return jsonify({"seq": seq, "turn": turn, "status": status}), 201

    @app.route("/api/projects/<pid>/participants", methods=["POST"])
    def api_project_add_participant(pid: str):
        """Add a participant to a project baton.

        JSON body: {agent, participant, phase?}
        ``agent`` is the agent performing the add — server-side authorization
        requires ``agent`` to already be a current participant of the baton
        (LOAD-BEARING: this check lives in the coordination write-fn, not
        here, so it can't be bypassed by a direct API call). ``participant``
        is the agent being added. ``phase?`` (#3039 U2a round 3): optional,
        client-computed CONVERSION phase, NOT trusted (U2b r2: the server re-derives it) -- see
        api_project_init's docstring note. ``add_participant`` never writes
        a transition phase (never a phase-machine edge), so there is no
        ``transition_phase`` field here.
        Response: {seq, added}, 201. ``added`` is false on an idempotent
        no-op (participant was already a participant).
        Errors: 400 (missing/invalid fields), 403 (agent not a participant),
                404 (project not found), 503 (store not configured).
        """
        from forum.coordination import (
            add_participant as _add_participant,
            NotAParticipant,
            ProjectNotFound,
        )

        store = app.config.get("COORD_STORE")
        allocator = app.config.get("COORD_ALLOCATOR")
        if store is None or allocator is None:
            return jsonify({"error": "coordination store not configured"}), 503

        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        f = _read_fields("POST /api/projects/<pid>/participants", data, {
            "agent": "idl", "participant": "idl", "phase": "idl",
        }, pid=pid)
        agent = f["agent"]
        if not agent or not db.is_valid_agent_name(agent):
            return jsonify({"error": "agent is required and must be valid"}), 400

        participant = f["participant"]
        if not participant or not db.is_valid_agent_name(participant):
            return jsonify({"error": f"invalid participant name {participant!r}"}), 400
        # #3039 U2a round 3: optional, NOT trusted (U2b r2: the server re-derives it) -- see
        # api_project_init's note.
        phase = f["phase"] or None

        phase, phase_refusal = _conversion_only_inputs(
            "POST /api/projects/<pid>/participants", pid, store, phase)
        if phase_refusal is not None:
            return jsonify({"error": phase_refusal[0]}), phase_refusal[1]

        try:
            seq, added = _add_participant(
                store, allocator, pid, agent=agent, participant=participant, phase=phase,
            )
        except ProjectNotFound:
            return jsonify({"error": f"project not found: {pid}"}), 404
        except NotAParticipant:
            return jsonify({"error": f"agent {agent!r} is not a participant of {pid}"}), 403

        return jsonify({"seq": seq, "added": added}), 201

    @app.route("/api/projects/<pid>/colleague", methods=["POST"])
    def api_project_set_colleague(pid: str):
        """Set or change the colleague: field on a project baton (#2764).

        JSON body: {agent, colleague, phase?}
        ``agent`` is the agent performing the set — server-side authorization
        requires ``agent`` to already be a current participant OR the
        baton's current turn holder (LOAD-BEARING: lives in the coordination
        write-fn, not here, so it can't be bypassed by a direct API call).
        ``colleague`` is the agent being named; also added to ``participants``
        (dedup) in the same atomic write. ``phase?`` (#3039 U2a round 3):
        optional, client-computed CONVERSION phase, NOT trusted (U2b r2: the server re-derives it) -- see
        api_project_init's docstring note. ``set_colleague`` never writes a
        transition phase (never a phase-machine edge), so there is no
        ``transition_phase`` field here.
        Response: {seq, participant_added}, 201.
        Errors: 400 (missing/invalid fields), 403 (agent not authorized),
                404 (project not found), 422 (PR baton at phase ``merge``, or
                its phase unresolvable -- see :func:`_colleague_write_refusal`),
                503 (store not configured).
        """
        from forum.coordination import (
            set_colleague as _set_colleague,
            NotAParticipant,
            ProjectNotFound,
        )

        store = app.config.get("COORD_STORE")
        allocator = app.config.get("COORD_ALLOCATOR")
        if store is None or allocator is None:
            return jsonify({"error": "coordination store not configured"}), 503

        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        f = _read_fields("POST /api/projects/<pid>/colleague", data, {
            "agent": "idl", "colleague": "idl", "phase": "idl",
        }, pid=pid)
        agent = f["agent"]
        if not agent or not db.is_valid_agent_name(agent):
            return jsonify({"error": "agent is required and must be valid"}), 400

        colleague = f["colleague"]
        if not colleague or not db.is_valid_agent_name(colleague):
            return jsonify({"error": f"invalid colleague name {colleague!r}"}), 400
        # #3039 U2a round 3: optional, NOT trusted (U2b r2: the server re-derives it) -- see
        # api_project_init's note.
        phase = f["phase"] or None

        phase, phase_refusal = _conversion_only_inputs(
            "POST /api/projects/<pid>/colleague", pid, store, phase)
        if phase_refusal is not None:
            return jsonify({"error": phase_refusal[0]}), phase_refusal[1]

        colleague_refusal = _colleague_write_refusal(
            "POST /api/projects/<pid>/colleague", pid, store)
        if colleague_refusal is not None:
            return jsonify({"error": colleague_refusal[0]}), colleague_refusal[1]

        try:
            seq, participant_added = _set_colleague(
                store, allocator, pid, agent=agent, colleague=colleague, phase=phase,
            )
        except ProjectNotFound:
            return jsonify({"error": f"project not found: {pid}"}), 404
        except NotAParticipant:
            return jsonify({
                "error": f"agent {agent!r} must be a participant or the "
                         f"current turn holder of {pid} to set colleague",
            }), 403

        return jsonify({"seq": seq, "participant_added": participant_added}), 201

    @app.route("/api/projects/<pid>/gc", methods=["POST"])
    def api_project_gc(pid: str):
        """GC-close a project baton (client has already queried gh state).

        JSON body: {agent, new_status, reason, phase?}
        ``new_status`` must be a closed status (merged or cancelled).
        ``phase?`` (#3039 U2a round 3): optional, client-computed CONVERSION
        phase, NOT trusted (U2b r2: the server re-derives it) -- see api_project_init's docstring note.
        This is the SAME write-body as ``/status``'s close branch (``gc`` is
        a second caller of ``close()``, not a separate write-body) --
        ``close`` never writes a transition phase, so there is no
        ``transition_phase`` field here either.
        Response: {seq, turn, status}, 201.
        Errors: 400, 404, 409 (baton already closed, #3313; nothing written).
        No participant gate (403) on this route -- see the in-body note.
        """
        from forum.coordination import close as _close, ProjectNotFound, AlreadyClosed

        store = app.config.get("COORD_STORE")
        allocator = app.config.get("COORD_ALLOCATOR")
        if store is None or allocator is None:
            return jsonify({"error": "coordination store not configured"}), 503

        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        f = _read_fields("POST /api/projects/<pid>/gc", data, {
            "agent": "idl", "new_status": "idl", "reason": "text", "phase": "idl",
        }, pid=pid)
        agent = f["agent"]
        if not agent or not db.is_valid_agent_name(agent):
            return jsonify({"error": "agent is required and must be valid"}), 400

        new_status = f["new_status"]
        reason = f["reason"]
        if not new_status or not reason:
            return jsonify({"error": "new_status and reason are required"}), 400
        # #3039 U2a round 3: optional, NOT trusted (U2b r2: the server re-derives it) -- see
        # api_project_init's note.
        phase = f["phase"] or None

        phase, phase_refusal = _conversion_only_inputs(
            "POST /api/projects/<pid>/gc", pid, store, phase)
        if phase_refusal is not None:
            return jsonify({"error": phase_refusal[0]}), phase_refusal[1]

        try:
            # #3313: the closed-state refusal (409) applies here too -- gc
            # only ever closes ACTIVE batons, so a double close is an
            # overwrite. The PARTICIPANT gate is deliberately NOT applied on
            # this route yet (require_participant=False): `baton gc` is run by
            # whoever sweeps and is a participant of only a minority of live
            # PR batons, so gating it would break the sweep. That is an open
            # design decision on #3313, not a settled exemption.
            seq = _close(store, allocator, pid, invoker=agent, new_status=new_status,
                         reason=reason, phase=phase, require_participant=False)
        except ProjectNotFound:
            return jsonify({"error": f"project not found: {pid}"}), 404
        except AlreadyClosed as e:
            return jsonify({"error": f"project {pid} is already {e.status}; "
                            "refusing to overwrite a closed status (reopen first)"}), 409

        turn, status = _read_project_state(store, pid)
        return jsonify({"seq": seq, "turn": turn, "status": status}), 201

    @app.route("/api/projects/<pid>/merge", methods=["POST"])
    def api_project_merge(pid: str):
        """Post-merge baton closure + OQ-4 archive relocation.

        Called AFTER gh pr merge succeeds client-side. Sets status→merged with the
        correct log format and moves the baton to archive/.

        JSON body: {agent, forced?, phase?}
        ``forced`` (bool, default false) adds "(FORCED past gates 3-4)" to the log.
        ``phase?`` (#3039 U2a round 3): optional, client-computed CONVERSION
        phase, NOT trusted (U2b r2: the server re-derives it) -- see api_project_init's docstring note.
        ``merge`` never writes a transition phase (terminal states live in
        ``status``, never ``phase``), so there is no ``transition_phase``
        field here.
        Response: {seq, status: "merged"}, 201. (turn omitted — file is in archive/).
        """
        from forum.coordination import merge as _merge, ProjectNotFound

        store = app.config.get("COORD_STORE")
        allocator = app.config.get("COORD_ALLOCATOR")
        if store is None or allocator is None:
            return jsonify({"error": "coordination store not configured"}), 503

        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        f = _read_fields("POST /api/projects/<pid>/merge", data, {
            "agent": "idl", "forced": "bool", "phase": "idl",
        }, pid=pid)
        agent = f["agent"]
        if not agent or not db.is_valid_agent_name(agent):
            return jsonify({"error": "agent is required and must be valid"}), 400

        forced = f["forced"]
        # #3039 U2a round 3: optional, NOT trusted (U2b r2: the server re-derives it) -- see
        # api_project_init's note.
        phase = f["phase"] or None

        phase, phase_refusal = _conversion_only_inputs(
            "POST /api/projects/<pid>/merge", pid, store, phase)
        if phase_refusal is not None:
            return jsonify({"error": phase_refusal[0]}), phase_refusal[1]

        try:
            seq = _merge(store, allocator, pid, merged_by=agent, forced=forced, phase=phase)
        except ProjectNotFound:
            return jsonify({"error": f"project not found: {pid}"}), 404

        return jsonify({"seq": seq, "status": "merged"}), 201

    return app


# ---------------------------------------------------------------------------
# Boot-verify probe (Part of #868 slice B — A9 fix)
#
# Converts deploy-time latent dependency gaps into start-time loud failures.
# Run BEFORE binding the port; do NOT call at module import time — tests import
# this module without intending to fire probes.
# ---------------------------------------------------------------------------

def probe_deps(db_path: str, audit_path: str) -> dict:
    """Run all dependency probes and return a status dict.

    Returns a dict with keys:
        "db"         -> "ok" | "missing"
        "audit_log"  -> "ok" | "missing"
        "engram_pkg" -> "ok" | "missing"
        "embeddings" -> "ok" | "degraded"

    Hard deps (missing → caller should exit 2): db, audit_log, engram_pkg.
    Soft deps (missing → DEGRADED banner, server still starts): embeddings.

    Internal ``_<dep>_error`` / ``_<dep>_path`` keys carry diagnostic detail
    for error messages; callers should treat any key starting with ``_`` as
    private to this function.
    """
    results: dict = {}

    # ------------------------------------------------------------------
    # Hard dep 1: DB parent dir writable + PRAGMA user_version accessible.
    # Mirrors the actual write path: sqlite3.connect(db_path) in main().
    # ------------------------------------------------------------------
    db_abs = os.path.abspath(db_path)
    db_parent = os.path.dirname(db_abs)
    try:
        if not os.path.isdir(db_parent) or not os.access(db_parent, os.W_OK):
            raise OSError(f"directory {db_parent!r} missing or not writable")
        _probe_conn = sqlite3.connect(db_abs)
        _probe_conn.execute("PRAGMA user_version")
        _probe_conn.close()
        results["db"] = "ok"
    except Exception as _exc:  # noqa: BLE001
        results["db"] = "missing"
        results["_db_error"] = str(_exc)

    # ------------------------------------------------------------------
    # Hard dep 2: audit-log path writable (open-append probe).
    # Mirrors audit.write_audit(path=audit_path) which opens for append.
    # ------------------------------------------------------------------
    audit_abs = os.path.abspath(audit_path)
    audit_parent = os.path.dirname(audit_abs)
    try:
        os.makedirs(audit_parent, exist_ok=True)
        with open(audit_abs, "a", encoding="utf-8"):
            pass
        results["audit_log"] = "ok"
    except Exception as _exc:  # noqa: BLE001
        results["audit_log"] = "missing"
        results["_audit_log_error"] = str(_exc)

    # ------------------------------------------------------------------
    # Hard dep 3: engram-pkg CLI file existence.
    # Delegates to packs_mod._engram_pkg_cli_path() — the layout-agnostic
    # upward search — so the two copies can't drift across layouts
    # (repo source: src/forum/ = 3 hops to root; deployed: app/forum/ = 2).
    # The pack-validation endpoints raise FileNotFoundError when the file
    # is absent, producing a 503 on every upload; we fail start instead.
    # ------------------------------------------------------------------
    cli_path = packs_mod._engram_pkg_cli_path()
    if cli_path.exists():
        results["engram_pkg"] = "ok"
    else:
        results["engram_pkg"] = "missing"
        results["_engram_pkg_path"] = str(cli_path)

    # ------------------------------------------------------------------
    # Soft dep: embeddings backend (sentence-transformers + sqlite-vec).
    # Absent → FTS-only mode — a designed degradation (emb_mod already
    # announces it at import time; we surface it structurally here).
    # ------------------------------------------------------------------
    if emb_mod.available():
        results["embeddings"] = "ok"
    else:
        results["embeddings"] = "degraded"

    return results


def _run_boot_verify(db_path: str, audit_path: str, verify_only: bool = False) -> dict:
    """Run boot-verify probes; exit 2 on hard-dep failure.

    One stderr line per hard-dep failure; DEGRADED banner for soft deps.
    If ``verify_only`` is True, print a full report then exit 0 (all ok)
    or 2 (any hard dep missing) — never binds the port.
    Returns the probe results dict (only reached when all hard deps pass,
    or when running in verify_only mode, which exits before returning).
    """
    results = probe_deps(db_path, audit_path)
    hard_deps = ("db", "audit_log", "engram_pkg")
    any_hard_fail = any(results.get(k) == "missing" for k in hard_deps)

    if verify_only:
        for dep in ("db", "audit_log", "engram_pkg", "embeddings"):
            status = results.get(dep, "unknown")
            print(f"[forum verify] {dep}: {status}", file=sys.stderr)
        if any_hard_fail:
            print(
                "[forum verify] FAILED — hard dep(s) missing; server would not start.",
                file=sys.stderr,
            )
            sys.exit(2)
        print("[forum verify] OK — all hard deps present.", file=sys.stderr)
        if results.get("embeddings") == "degraded":
            print(
                "[forum] DEGRADED: embeddings backend unavailable — "
                "running in FTS-only mode (semantic search disabled).",
                file=sys.stderr,
            )
        sys.exit(0)

    # Normal startup path: fail loud on any hard-dep missing.
    if results.get("db") == "missing":
        err = results.get("_db_error", "unknown error")
        print(f"[forum] FATAL: DB dependency check failed: {err}", file=sys.stderr)
    if results.get("audit_log") == "missing":
        err = results.get("_audit_log_error", "unknown error")
        print(f"[forum] FATAL: audit-log dependency check failed: {err}", file=sys.stderr)
    if results.get("engram_pkg") == "missing":
        path = results.get("_engram_pkg_path", "tools/engram-pkg/engram-pkg")
        print(
            f"[forum] FATAL: engram-pkg CLI not found at {path} — "
            "pack validation will fail; aborting start.",
            file=sys.stderr,
        )
    if any_hard_fail:
        sys.exit(2)

    if results.get("embeddings") == "degraded":
        print(
            "[forum] DEGRADED: embeddings backend unavailable — "
            "running in FTS-only mode (semantic search disabled).",
            file=sys.stderr,
        )

    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def _parse_args(argv=None, environ=None):
    """Parse the server CLI. ``environ`` is injectable for tests.

    #3039 U2b: ``--baton-login-map`` (env ``ENGRAM_BATON_LOGIN_MAP``) names the
    protected agent -> GitHub-login map for the strict peer->merge gate. There
    is NO default path: absent means the gate fails closed.
    ``--baton-login-map-admin-uid`` (repeatable; env
    ``ENGRAM_BATON_LOGIN_MAP_ADMIN_UIDS``, comma-separated) names the uids,
    besides root, allowed to own the map -- never defaulted to the server's own
    uid, because the server runs as an agent account.
    """
    import argparse

    env = os.environ if environ is None else environ
    p = argparse.ArgumentParser(description="LAN agent forum server")
    p.add_argument("--port", type=int, default=5002)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--db", default=os.path.expanduser("~/.forum/forum.db"))
    p.add_argument("--audit", default=os.path.expanduser("~/.forum/forum-audit.jsonl"))
    p.add_argument("--categories-config", default=None,
                   help="Path to a JSON categories config file. "
                        "Overrides FORUM_CATEGORIES_CONFIG env and default resolution chain.")
    p.add_argument("--packs-dir", default=None,
                   help="Directory for uploaded pack storage. "
                        "Defaults to packs/ next to --db.")
    p.add_argument("--verify-only", action="store_true",
                   help="Run boot-verify probes, print report, exit 0/2 without binding the port.")
    p.add_argument("--baton-login-map", default=(env.get("ENGRAM_BATON_LOGIN_MAP") or "").strip() or None,
                   help="Path to the PROTECTED agent -> GitHub-login map the strict baton "
                        "peer->merge gate reads (env: ENGRAM_BATON_LOGIN_MAP). No default path: "
                        "when absent, `baton advance` fails closed ('no login map configured'). "
                        "The file and every directory above it must be owned by root or by an "
                        "--baton-login-map-admin-uid, and not group/world-writable.")
    p.add_argument("--baton-login-map-admin-uid", type=int, action="append", default=None,
                   help="Extra uid (besides root) allowed to own the login map and its directories. "
                        "Repeatable (env: ENGRAM_BATON_LOGIN_MAP_ADMIN_UIDS, comma-separated). "
                        "Never defaulted to the server's own uid.")
    args = p.parse_args(argv)
    uids = list(args.baton_login_map_admin_uid or [])
    for tok in (env.get("ENGRAM_BATON_LOGIN_MAP_ADMIN_UIDS") or "").split(","):
        tok = tok.strip()
        if tok:
            try:
                uids.append(int(tok))
            except ValueError:
                p.error(f"ENGRAM_BATON_LOGIN_MAP_ADMIN_UIDS: {tok!r} is not an integer uid")
    args.baton_login_map_admin_uid = uids
    return args


def main() -> None:
    args = _parse_args()

    # ------------------------------------------------------------------
    # Boot-verify: run BEFORE binding the port.
    # Exits 2 on hard-dep failure; DEGRADED banner for soft deps.
    # With --verify-only, prints a full report and exits without starting.
    # ------------------------------------------------------------------
    dep_results = _run_boot_verify(args.db, args.audit, verify_only=args.verify_only)

    # Ensure db directory exists
    db_dir = os.path.dirname(os.path.abspath(args.db))
    os.makedirs(db_dir, exist_ok=True)

    # Run migrations + seed
    conn = sqlite3.connect(args.db)
    db.init_db(conn, categories_config=args.categories_config)
    seed.seed_threads(conn)
    conn.close()

    app = create_app(
        args.db,
        args.audit,
        categories_config=args.categories_config,
        packs_dir=args.packs_dir,
        dep_results=dep_results,
        coord_root=str(default_store_root()),
        backup_dir=os.environ.get("FORUM_BACKUP_DIR", "").strip() or None,
        baton_login_map=args.baton_login_map,
        baton_login_map_admin_uids=tuple(args.baton_login_map_admin_uid),
    )
    if args.baton_login_map is None:
        print("[forum] baton login map: not configured -- `baton advance` (peer->merge) "
              "will refuse until --baton-login-map / $ENGRAM_BATON_LOGIN_MAP is set.",
              file=sys.stderr)
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
