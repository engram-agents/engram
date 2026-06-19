#!/usr/bin/env python3
"""baton — Multi-Agent Turn-State CLI

A thin CLI wrapping the baton file protocol at
/home/agents-shared/projects/. Provides explicit turn declarations for
shared projects (PRs, designs, etc.) so agents always know whose move it
is next.

Twelve subcommands:
  init        Create a new project baton file
  flip        Pass the baton to another participant (PR layer)
  claim       Take a Project-layer baton from the pool (pool → self)
  release     Return a Project-layer baton to the pool (self → pool sentinel)
  status      List active projects (not merged or cancelled)
  mine        Shorthand for status --mine
  show        Display a project file
  close       Mark a project merged or cancelled
  reopen      Flip a closed baton back to an active status (inverse of close)
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
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Environment + paths
# ---------------------------------------------------------------------------

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)
BATON_PROJECTS_DIR = os.environ.get("BATON_PROJECTS_DIR", "/home/agents-shared/projects")

# Exit codes
EXIT_OK = 0
EXIT_VALIDATION = 1
EXIT_IO = 2
EXIT_STATE = 3

# Valid project statuses
VALID_STATUSES = {"planning", "in-progress", "in-review", "merged", "cancelled"}
CLOSED_STATUSES = {"merged", "cancelled"}

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


def _is_multi_agent_mode(projects_dir: Optional[Path] = None) -> bool:
    """True if baton projects directory exists (multi-agent gate).

    Baton's multi-agent guard is the projects directory itself — if it
    doesn't exist, we're in single-agent mode (or the operator hasn't
    deployed baton). This is simpler than ia.py's config-based gate because
    baton's deployment model is: multi-agent operators mkdir the directory.
    """
    if projects_dir is None:
        projects_dir = Path(BATON_PROJECTS_DIR)
    return projects_dir.is_dir()


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


def _now_iso() -> str:
    """Return current UTC time as ISO-8601 with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

# GitHub anchor value validation: pr/<N> or project/<N>
_GITHUB_ANCHOR_RE = re.compile(r"^(pr|project)/\d+$")


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


def _project_path(project_id: str, projects_dir: Optional[Path] = None) -> Path:
    """Resolve the filesystem path for a project file."""
    if projects_dir is None:
        projects_dir = Path(BATON_PROJECTS_DIR)
    return projects_dir / f"{project_id}.md"


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------

def _atomic_write(dest: Path, content: str) -> None:
    """Write content to dest atomically via tmp + os.rename.

    Writes to <dest>.tmp first, then renames to dest. On POSIX systems
    os.rename is atomic within the same filesystem — a concurrent reader
    cannot see a partial file.
    """
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.rename(str(tmp), str(dest))
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        print(f"baton: write failed: {e}", file=sys.stderr)
        sys.exit(EXIT_IO)


# ---------------------------------------------------------------------------
# Frontmatter replacement helper
# ---------------------------------------------------------------------------

def _update_frontmatter(text: str, updates: dict) -> str:
    """Apply updates dict to frontmatter fields; return full updated text.

    Replaces individual frontmatter values in-place (preserving order of
    other fields and the body). If a key from updates doesn't exist in the
    frontmatter, it is appended before the closing ---.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        # No frontmatter — can't update; caller must handle
        return text

    fm_block = m.group(1)
    body = text[m.end():]
    fm_lines = fm_block.split("\n")
    found_keys = set()

    new_lines = []
    for line in fm_lines:
        fm = _FRONTMATTER_FIELD_RE.match(line)
        if fm:
            key = fm.group(1).strip().lower()
            if key in updates:
                new_lines.append(f"{key}: {updates[key]}")
                found_keys.add(key)
                continue
        new_lines.append(line)

    # Append any keys that weren't already present
    for key, val in updates.items():
        if key not in found_keys:
            new_lines.append(f"{key}: {val}")

    new_fm_block = "\n".join(new_lines)
    return f"---\n{new_fm_block}\n---\n{body}"


# ---------------------------------------------------------------------------
# CI-state helper for the flip guard (#685)
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


def _pr_ci_state(pr_number: str) -> tuple:
    """Query GitHub CI state for a pull request via `gh`.

    Returns (state, detail) where state is one of:
      "green"   — all checks passed (or no checks registered)
      "red"     — at least one check failed
      "pending" — no failure, but at least one check still running
      "unknown" — could not obtain a verdict (degraded; advisory only)

    The gh call uses cwd-based repo inference; a failure here is expected
    when invoked outside the repo tree and must NOT hard-block the caller.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_number,
             "--json", "statusCheckRollup,mergeStateStatus"],
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

    checks = data.get("statusCheckRollup")
    if not isinstance(checks, list):
        return ("unknown", "statusCheckRollup missing or not a list")

    if not checks:
        # No checks registered — treat as green
        return ("green", "no checks registered")

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
# Approval-staleness helper for the flip guard (#1002)
# ---------------------------------------------------------------------------

def _pr_approval_state(pr_number: str) -> tuple:
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

    Two gh calls:
      1. gh repo view --json nameWithOwner  (cwd-inferred repo identity)
      2. gh api graphql querying headRefOid and reviews(last:50){state,commit{oid}}

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

    # Step 1: resolve repo nameWithOwner (same effective scoping as today's
    # `gh pr view` — cwd-inferred from the working tree).
    try:
        repo_result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner"],
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

    if repo_result.returncode != 0:
        detail = (repo_result.stderr or "").strip() or f"gh exit {repo_result.returncode}"
        return ("unknown", detail)

    raw_repo = (repo_result.stdout or "").strip()
    if not raw_repo:
        return ("unknown", "gh repo view returned empty output")

    try:
        repo_data = json.loads(raw_repo)
        name_with_owner = repo_data["nameWithOwner"]
        owner, repo_name = name_with_owner.split("/", 1)
    except (json.JSONDecodeError, ValueError, KeyError):
        return ("unknown", "gh repo view returned unparseable JSON or missing nameWithOwner")

    # Step 2: GraphQL — fetch headRefOid and last 50 reviews with commit oid.
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


# ---------------------------------------------------------------------------
# Turn log helpers
# ---------------------------------------------------------------------------

def _append_turn_log(body: str, entry: str) -> str:
    """Append a new turn log entry to the body.

    Finds the '## Turn log' section and appends. Creates the section if
    absent.
    """
    section_header = "## Turn log"
    if section_header in body:
        # Append at end of the turn log section
        return body.rstrip() + "\n" + entry + "\n"
    else:
        # Create the section
        separator = "\n\n" if body.strip() else ""
        return body.rstrip() + separator + f"\n{section_header}\n\n" + entry + "\n"


# ---------------------------------------------------------------------------
# Project scanning
# ---------------------------------------------------------------------------

def _scan_projects(
    projects_dir: Path,
    agent_name: str = "",
    active_only: bool = True,
) -> list:
    """Scan projects_dir for baton project files.

    Returns list of dicts with keys:
      project_id, title, status, turn, turn_since, turn_reason, participants, path
    Files that don't parse cleanly are silently skipped.
    Sorts by turn_since ascending (oldest baton first).
    """
    if not projects_dir.is_dir():
        return []

    results = []
    for md_file in projects_dir.glob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        fields, _body = _parse_frontmatter(text)
        if not fields:
            continue

        project_id = fields.get("project", "").strip()
        if not project_id:
            # Try inferring from filename
            project_id = md_file.stem

        status = fields.get("status", "").strip().lower()
        turn = fields.get("turn", "").strip().lower()
        turn_since_str = fields.get("turn_since", "").strip()
        turn_reason = fields.get("turn_reason", "").strip()
        # Strip surrounding quotes from turn_reason if present
        if turn_reason.startswith('"') and turn_reason.endswith('"'):
            turn_reason = turn_reason[1:-1]
        title = fields.get("title", "").strip()
        participants_str = fields.get("participants", "").strip()
        participants = _parse_participants(participants_str) if participants_str else []
        turn_since = _parse_iso(turn_since_str)

        if active_only and status in CLOSED_STATUSES:
            continue

        results.append({
            "project_id": project_id,
            "title": title,
            "status": status,
            "turn": turn,
            "turn_since": turn_since,
            "turn_since_str": turn_since_str,
            "turn_reason": turn_reason,
            "participants": participants,
            "path": str(md_file),
        })

    results.sort(key=lambda p: p["turn_since"] or datetime.min.replace(tzinfo=timezone.utc))
    return results


# ---------------------------------------------------------------------------
# Subcommand: init
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    projects_dir = Path(BATON_PROJECTS_DIR)

    if not projects_dir.is_dir():
        print(
            f"baton: projects directory not found: {projects_dir}. "
            "baton is a multi-agent tool. Create the directory first: "
            f"mkdir -p {projects_dir}",
            file=sys.stderr,
        )
        sys.exit(EXIT_IO)

    project_id = args.project_id
    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    dest = _project_path(project_id, projects_dir)
    if dest.exists():
        print(
            f"baton init: project file already exists: {dest}. "
            "Use 'baton flip' to pass the baton, or 'baton show' to view.",
            file=sys.stderr,
        )
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

    # Default turn: the invoking agent (if in participants) else first participant
    if args.turn:
        initial_turn = args.turn.strip().lower()
    elif agent_name and agent_name.lower() in participants:
        initial_turn = agent_name.lower()
    else:
        initial_turn = participants[0]

    # Validate initial turn
    if initial_turn != _pool_sentinel() and initial_turn not in participants:
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
                "Expected pr/<N> or project/<N> (e.g. pr/490, project/4).",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)

    now = _now_iso()
    participants_str = _format_participants(participants)
    title = args.title or project_id

    # Build the initial reason
    init_reason = f"project initialized by {agent_name or 'unknown'}"

    content_lines = [
        "---",
        f"project: {project_id}",
        f"title: {title}",
        f"status: {status}",
        f"turn: {initial_turn}",
        f"turn_since: {now}",
        f'turn_reason: "{init_reason}"',
        f"participants: {participants_str}",
    ]
    if github_anchor:
        content_lines.append(f"github: {github_anchor}")
    content_lines += [
        "---",
        "",
        f"# {project_id} — {title}",
        "",
        "",
        "## Turn log",
        "",
        f"- {now} initialized → {initial_turn}: {init_reason}",
        "",
    ]
    content = "\n".join(content_lines)

    _atomic_write(dest, content)
    print(f"baton init: created {dest}")
    print(f"  project:      {project_id}")
    print(f"  title:        {title}")
    print(f"  participants: {', '.join(participants)}")
    print(f"  turn:         {initial_turn}")
    print(f"  status:       {status}")
    if github_anchor:
        print(f"  github:       {github_anchor}")


# ---------------------------------------------------------------------------
# Subcommand: flip
# ---------------------------------------------------------------------------

def cmd_flip(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    projects_dir = Path(BATON_PROJECTS_DIR)
    project_id = args.project_id
    to_agent = args.to.strip().lower()
    reason = args.reason

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    dest = _project_path(project_id, projects_dir)
    if not dest.exists():
        print(
            f"baton flip: project not found: '{project_id}' "
            f"(looked for {dest}). "
            "Use 'baton status' to see active projects.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    try:
        text = dest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"baton flip: cannot read '{dest}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)

    fields, body = _parse_frontmatter(text)
    if not fields:
        print(
            f"baton flip: cannot parse frontmatter from '{dest}'. "
            "The file may be malformed.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Validate: to_agent must be in participants or the pool sentinel
    participants_str = fields.get("participants", "").strip()
    participants = _parse_participants(participants_str) if participants_str else []

    if to_agent != _pool_sentinel() and to_agent not in participants:
        print(
            f"baton flip: '{to_agent}' is not a participant in {project_id}. "
            f"Participants: {participants}. "
            "Only participants (or the pool sentinel) can receive the baton.",
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
    github_anchor = fields.get("github", "").strip().lower()
    _flip_force = args.force
    _pr_anchor_match = re.match(r"^pr/(\d+)$", github_anchor)
    if _pr_anchor_match and to_agent == _pool_sentinel():
        _pr_num = _pr_anchor_match.group(1)
        _ci_state, _ci_detail = _pr_ci_state(_pr_num)
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
        # --force). no_approval passes through: approval-existence is the
        # colleague-layer's gate, not this oid compare's.
        _ap_state, _ap_detail = _pr_approval_state(_pr_num)
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
        elif _ap_state == "unknown":
            print(
                f"warning: could not verify post-approval state for PR "
                f"#{_pr_num} ({_ap_detail}) — proceeding anyway",
                file=sys.stderr,
            )
        # fresh / no_approval: proceed silently

    now = _now_iso()
    from_agent = fields.get("turn", agent_name or "unknown").strip().lower()

    # Update frontmatter
    updates = {
        "turn": to_agent,
        "turn_since": now,
        "turn_reason": f'"{reason}"',
    }
    updated_text = _update_frontmatter(text, updates)

    # Re-parse to get body after frontmatter update
    _updated_fields, updated_body = _parse_frontmatter(updated_text)

    # Append turn log entry
    log_entry = f"- {now} {from_agent} → {to_agent}: {reason}"
    final_body = _append_turn_log(updated_body, log_entry)

    # Reconstruct final content: frontmatter + updated body
    fm_m = _FRONTMATTER_RE.match(updated_text)
    if fm_m:
        fm_section = updated_text[:fm_m.end()]
        final_content = fm_section.rstrip("\n") + "\n" + final_body
    else:
        final_content = updated_text

    _atomic_write(dest, final_content)
    print(f"baton flip: {project_id} → {to_agent}")
    print(f"  from:   {from_agent}")
    print(f"  reason: {reason}")
    print(f"  at:     {now}")


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


def cmd_claim(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Claim a Project-layer baton from the pool.

    Equivalent to 'baton flip <project-id> <self> "claimed"' but makes the
    Project-layer grab/ungrab vocabulary first-class. Only succeeds when
    turn == pool sentinel (the install's primary_user); refuses to steal from
    another agent.
    """
    projects_dir = Path(BATON_PROJECTS_DIR)
    project_id = args.project_id

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    dest = _project_path(project_id, projects_dir)
    if not dest.exists():
        print(
            f"baton claim: project not found: '{project_id}' "
            f"(looked for {dest}). "
            "Use 'baton status' to see active projects.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    try:
        text = dest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"baton claim: cannot read '{dest}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)

    fields, body = _parse_frontmatter(text)
    if not fields:
        print(
            f"baton claim: cannot parse frontmatter from '{dest}'. "
            "The file may be malformed.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Validate: current turn must be the pool sentinel
    current_turn = fields.get("turn", "").strip().lower()
    if current_turn != _pool_sentinel():
        print(
            f"baton claim: refusing to steal claim from {current_turn}; "
            "use 'baton flip <project-id> <self> <reason>' if you have an "
            "explicit handoff agreement.",
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
            f"Participants: {participants}.",
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

    now = _now_iso()
    self_name = agent_name.lower()

    # Atomic write: turn → self, turn_since → now, turn_reason → "claimed"
    updates = {
        "turn": self_name,
        "turn_since": now,
        "turn_reason": '"claimed"',
    }
    updated_text = _update_frontmatter(text, updates)

    # Re-parse to get body after frontmatter update
    _updated_fields, updated_body = _parse_frontmatter(updated_text)

    # Append turn log entry
    log_entry = f"- {now} {_pool_sentinel()} → {self_name}: claimed"
    final_body = _append_turn_log(updated_body, log_entry)

    # Reconstruct final content: frontmatter + updated body
    fm_m = _FRONTMATTER_RE.match(updated_text)
    if fm_m:
        fm_section = updated_text[:fm_m.end()]
        final_content = fm_section.rstrip("\n") + "\n" + final_body
    else:
        final_content = updated_text

    _atomic_write(dest, final_content)
    print(f"baton claim: {project_id} → {self_name}")


# ---------------------------------------------------------------------------
# Subcommand: release  (Project layer — self → pool sentinel)
# ---------------------------------------------------------------------------

def cmd_release(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Release a Project-layer baton back to the pool.

    Equivalent to 'baton flip <project-id> <pool-sentinel> <reason>' but
    restricted to the current holder (only you can release what you hold).
    With --done, also appends '(done)' to the project title (idempotent).
    """
    projects_dir = Path(BATON_PROJECTS_DIR)
    project_id = args.project_id
    mark_done = args.done
    reason = args.reason

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    dest = _project_path(project_id, projects_dir)
    if not dest.exists():
        print(
            f"baton release: project not found: '{project_id}' "
            f"(looked for {dest}). "
            "Use 'baton status' to see active projects.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    try:
        text = dest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"baton release: cannot read '{dest}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)

    fields, body = _parse_frontmatter(text)
    if not fields:
        print(
            f"baton release: cannot parse frontmatter from '{dest}'. "
            "The file may be malformed.",
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

    now = _now_iso()

    # Build frontmatter updates: turn → pool sentinel, turn_since → now, turn_reason → reason
    updates: dict = {
        "turn": _pool_sentinel(),
        "turn_since": now,
        "turn_reason": f'"{reason}"',
    }

    # --done: append "(done)" to title (idempotent)
    title_renamed = False
    if mark_done:
        current_title = fields.get("title", "").strip()
        if not current_title.endswith("(done)"):
            new_title = current_title + " (done)"
            updates["title"] = new_title
            title_renamed = True

    updated_text = _update_frontmatter(text, updates)

    # Re-parse to get body after frontmatter update
    _updated_fields, updated_body = _parse_frontmatter(updated_text)

    # Append turn log entry (and a second line if title was renamed)
    log_entry = f"- {now} {self_name} → {_pool_sentinel()}: {reason}"
    if title_renamed:
        log_entry += f"\n- {now} title marked (done)"
    final_body = _append_turn_log(updated_body, log_entry)

    # Reconstruct final content: frontmatter + updated body
    fm_m = _FRONTMATTER_RE.match(updated_text)
    if fm_m:
        fm_section = updated_text[:fm_m.end()]
        final_content = fm_section.rstrip("\n") + "\n" + final_body
    else:
        final_content = updated_text

    _atomic_write(dest, final_content)
    print(f"baton release: {project_id} → {_pool_sentinel()}")
    if mark_done:
        print("  marked (done)")


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    projects_dir = Path(BATON_PROJECTS_DIR)

    if not projects_dir.is_dir():
        # Single-agent or not deployed — silent exit per spec
        sys.exit(EXIT_OK)

    projects = _scan_projects(projects_dir, agent_name, active_only=True)

    mine_only = getattr(args, "mine", False)
    if mine_only and agent_name:
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
        reason_part = f" — {p['turn_reason']}" if p['turn_reason'] else ""
        print(f"  {p['project_id']:<28} {turn_info}{reason_part}")
        if p["title"] and p["title"] != p["project_id"]:
            print(f"    {p['title']}")


# ---------------------------------------------------------------------------
# Subcommand: mine
# ---------------------------------------------------------------------------

def cmd_mine(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Shorthand for status --mine."""
    # Inject mine=True and delegate to cmd_status
    args.mine = True
    cmd_status(args, config, agent_name)


# ---------------------------------------------------------------------------
# Subcommand: show
# ---------------------------------------------------------------------------

def cmd_show(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    projects_dir = Path(BATON_PROJECTS_DIR)
    project_id = args.project_id

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    dest = _project_path(project_id, projects_dir)
    if not dest.exists():
        print(
            f"baton show: project not found: '{project_id}' "
            f"(looked for {dest}). "
            "Use 'baton status' to see active projects.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    try:
        text = dest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"baton show: cannot read '{dest}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)

    print(text, end="" if text.endswith("\n") else "\n")


# ---------------------------------------------------------------------------
# Subcommand: close
# ---------------------------------------------------------------------------

def cmd_close(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    projects_dir = Path(BATON_PROJECTS_DIR)
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

    dest = _project_path(project_id, projects_dir)
    if not dest.exists():
        print(
            f"baton close: project not found: '{project_id}' "
            f"(looked for {dest}).",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    try:
        text = dest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"baton close: cannot read '{dest}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)

    fields, _body = _parse_frontmatter(text)
    if not fields:
        print(
            f"baton close: cannot parse frontmatter from '{dest}'.",
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

    updated_text = _update_frontmatter(text, {"status": new_status})
    _atomic_write(dest, updated_text)
    print(f"baton close: {project_id} → {new_status}")


# ---------------------------------------------------------------------------
# Subcommand: reopen
# ---------------------------------------------------------------------------

# Active statuses that reopen can target (complement of CLOSED_STATUSES).
ACTIVE_STATUSES = {"planning", "in-progress", "in-review"}


def cmd_reopen(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Flip a closed baton back to an active status.

    Inverse of cmd_close. Only operates on batons with status: merged or
    cancelled. Refuses if the baton is already in a non-closed status.
    """
    projects_dir = Path(BATON_PROJECTS_DIR)
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

    dest = _project_path(project_id, projects_dir)
    if not dest.exists():
        print(
            f"baton reopen: project not found: '{project_id}' "
            f"(looked for {dest}).",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    try:
        text = dest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"baton reopen: cannot read '{dest}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)

    fields, _body = _parse_frontmatter(text)
    if not fields:
        print(
            f"baton reopen: cannot parse frontmatter from '{dest}'.",
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

    now = _now_iso()
    invoker = agent_name or "unknown"

    # Update status in frontmatter
    updated_text = _update_frontmatter(text, {"status": new_status})

    # Re-parse to get body after frontmatter update
    _updated_fields, updated_body = _parse_frontmatter(updated_text)

    # Append audit-log entry
    log_entry = f"- {now} reopened → {invoker}: status was {current_status} → {new_status}"
    final_body = _append_turn_log(updated_body, log_entry)

    # Reconstruct final content: frontmatter + updated body
    fm_m = _FRONTMATTER_RE.match(updated_text)
    if fm_m:
        fm_section = updated_text[:fm_m.end()]
        final_content = fm_section.rstrip("\n") + "\n" + final_body
    else:
        final_content = updated_text

    _atomic_write(dest, final_content)
    print(f"baton reopen: {project_id} → {new_status}")


# ---------------------------------------------------------------------------
# Subcommand: rename
# ---------------------------------------------------------------------------

def cmd_rename(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    projects_dir = Path(BATON_PROJECTS_DIR)
    project_id = args.project_id
    new_title = args.title

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    # Validate title before touching the filesystem
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

    dest = _project_path(project_id, projects_dir)
    if not dest.exists():
        print(
            f"baton rename: project not found: '{project_id}' "
            f"(looked for {dest}). "
            "Use 'baton status' to see active projects.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    try:
        text = dest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"baton rename: cannot read '{dest}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)

    fields, body = _parse_frontmatter(text)
    if not fields:
        print(
            f"baton rename: cannot parse frontmatter from '{dest}'. "
            "The file may be malformed.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    if "title" not in fields:
        print(
            f"baton rename: 'title' field absent in '{dest}' — "
            "refusing to write to a possibly corrupted file.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    old_title = fields["title"]

    # Update frontmatter (preserves field order via _update_frontmatter)
    updated_text = _update_frontmatter(text, {"title": stripped_title})

    # Re-parse to get body after frontmatter update
    _updated_fields, updated_body = _parse_frontmatter(updated_text)

    # Append audit line to turn log
    now = _now_iso()
    log_entry = (
        f'- {now} renamed: title from "{old_title}" to "{stripped_title}"'
    )
    final_body = _append_turn_log(updated_body, log_entry)

    # Reconstruct final content: frontmatter + updated body
    fm_m = _FRONTMATTER_RE.match(updated_text)
    if fm_m:
        fm_section = updated_text[:fm_m.end()]
        final_content = fm_section.rstrip("\n") + "\n" + final_body
    else:
        final_content = updated_text

    _atomic_write(dest, final_content)
    print(f'baton rename: {project_id} title → "{stripped_title}"')


# ---------------------------------------------------------------------------
# Subcommand: anchor
# ---------------------------------------------------------------------------

def cmd_anchor(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Set or update the github: anchor on an existing baton.

    Mirrors cmd_rename's atomic frontmatter-update pattern:
    read → validate → _update_frontmatter → atomic write → audit line.
    """
    projects_dir = Path(BATON_PROJECTS_DIR)
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
            "Expected pr/<N> or project/<N> (e.g. pr/490, project/4).",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    dest = _project_path(project_id, projects_dir)
    if not dest.exists():
        print(
            f"baton anchor: project not found: '{project_id}' "
            f"(looked for {dest}). "
            "Use 'baton status' to see active projects.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    try:
        text = dest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"baton anchor: cannot read '{dest}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)

    fields, body = _parse_frontmatter(text)
    if not fields:
        print(
            f"baton anchor: cannot parse frontmatter from '{dest}'. "
            "The file may be malformed.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    old_anchor = fields.get("github", "").strip()

    # Update (or add) the github: field in frontmatter
    updated_text = _update_frontmatter(text, {"github": github_anchor})

    # Re-parse to get body after frontmatter update
    _updated_fields, updated_body = _parse_frontmatter(updated_text)

    # Append audit line to turn log
    now = _now_iso()
    if old_anchor:
        log_entry = (
            f'- {now} anchor updated: github from "{old_anchor}" to "{github_anchor}"'
        )
    else:
        log_entry = f'- {now} anchor set: github → {github_anchor}'
    final_body = _append_turn_log(updated_body, log_entry)

    # Reconstruct final content: frontmatter + updated body
    fm_m = _FRONTMATTER_RE.match(updated_text)
    if fm_m:
        fm_section = updated_text[:fm_m.end()]
        final_content = fm_section.rstrip("\n") + "\n" + final_body
    else:
        final_content = updated_text

    _atomic_write(dest, final_content)
    print(f"baton anchor: {project_id} github → {github_anchor}")


# ---------------------------------------------------------------------------
# Subcommand: merge  (gate-checked merge verb — #999 + #1000)
# ---------------------------------------------------------------------------

def cmd_merge(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Squash-merge a PR through the baton gate ladder.

    Gate ladder (all must pass in order):
      1. Baton exists at projects dir for PR-N.           (never forceable)
      2. turn == pool sentinel.                            (never forceable)
      3. CI green via _pr_ci_state.                       (--force skips)
      4. Approval fresh via _pr_approval_state (oid).     (--force skips)
      5. gh pr merge <N> --squash.
      6. On success: append turn-log + set baton status → merged.

    --force skips gates 3-4 only; never skips 1-2.
    --dry-run prints the ladder verdict without merging.
    """
    projects_dir = Path(BATON_PROJECTS_DIR)

    # Resolve project_id and pr_number from the argument (PR-N or bare N)
    raw = args.pr.strip()
    m = re.match(r"^(?:PR-|pr-)(\d+)$", raw)
    if m:
        pr_number = m.group(1)
        project_id = f"PR-{pr_number}"
    else:
        pr_number = raw.lstrip("#")
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
    dest = _project_path(project_id, projects_dir)
    if not dest.exists():
        print(
            f"baton merge: no baton — a PR without a baton is not merge-ready "
            "(init + run the review pipeline). "
            f"Looked for {dest}.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    try:
        text = dest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"baton merge: cannot read '{dest}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)

    fields, body = _parse_frontmatter(text)
    if not fields:
        print(
            f"baton merge: cannot parse frontmatter from '{dest}'. "
            "The file may be malformed.",
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

    # ---- Gate 2: turn == pool sentinel --------------------------------
    sentinel = _pool_sentinel()
    current_turn = fields.get("turn", "").strip().lower()
    if current_turn != sentinel:
        holder = current_turn or "(unknown)"
        print(
            f"baton merge: turn is with {holder} — not presented for merge. "
            f"The baton must be flipped to the pool sentinel ('{sentinel}') "
            "before merging.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # ---- Gate 3: CI green (skippable with --force) --------------------
    _ci_verdict = "green"
    if force:
        # Audit-trail parity with cmd_flip --force: still query the actual
        # state so the forced merge's warning (and turn-log) record what was
        # bypassed, not just that something was.
        _ci_state, _ci_detail = _pr_ci_state(pr_number)
        print(
            f"warning: --force — skipping CI-green check for PR #{pr_number} "
            f"(actual: {_ci_state} — {_ci_detail})",
            file=sys.stderr,
        )
        _ci_verdict = f"skipped (--force; actual: {_ci_state})"
    else:
        _ci_state, _ci_detail = _pr_ci_state(pr_number)
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
        _ap_state, _ap_detail = _pr_approval_state(pr_number)
        print(
            f"warning: --force — skipping approval-fresh check for PR "
            f"#{pr_number} (actual: {_ap_state} — {_ap_detail})",
            file=sys.stderr,
        )
        _ap_verdict = f"skipped (--force; actual: {_ap_state})"
    else:
        _ap_state, _ap_detail = _pr_approval_state(pr_number)
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
        print(f"  gate 2 (turn=sentinel): PASS — turn={current_turn}")
        print(f"  gate 3 (CI):            {_ci_verdict}")
        print(f"  gate 4 (approval):      {_ap_verdict}")
        print(f"  merge: DRY-RUN — no merge performed")
        return

    # ---- Gate 5: gh pr merge --squash --------------------------------
    try:
        merge_result = subprocess.run(
            ["gh", "pr", "merge", pr_number, "--squash"],
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

    # ---- Gate 6: post-merge baton closure ----------------------------
    now = _now_iso()
    merge_by = agent_name or "unknown"
    if force:
        log_entry = (
            f"- {now} merged via baton merge by {merge_by} "
            "(FORCED past gates 3-4)"
        )
    else:
        log_entry = f"- {now} merged via baton merge by {merge_by}"

    # Update frontmatter: set status → merged
    updated_text = _update_frontmatter(text, {"status": "merged"})

    # Re-parse to get the body after frontmatter update, then append log entry
    _uf, updated_body = _parse_frontmatter(updated_text)
    final_body = _append_turn_log(updated_body, log_entry)

    # Reconstruct: frontmatter section + updated body
    fm_m = _FRONTMATTER_RE.match(updated_text)
    if fm_m:
        fm_section = updated_text[:fm_m.end()]
        final_content = fm_section.rstrip("\n") + "\n" + final_body
    else:
        final_content = updated_text

    # The GitHub merge above is irreversible; say so BEFORE the local write so
    # an _atomic_write failure is diagnosable as merged-on-GitHub/baton-stale.
    print(f"baton merge: PR #{pr_number} merged on GitHub; writing baton closure...")
    _atomic_write(dest, final_content)

    print(f"baton merge: PR #{pr_number} merged (squash)")
    print(f"  baton: {project_id} → merged")
    print(f"  at:    {now}")


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
            "Projects dir:  /home/agents-shared/projects/ (manual deploy)\n"
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
            "Format: pr/<N> or project/<N> (e.g. pr/490, project/4). "
            "Without this, only batons named PR-<N> resolve automatically."
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
            "Refuses to steal from another agent — use 'baton flip' with an "
            "explicit handoff agreement if needed.\n\n"
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
            "GitHub anchor value: pr/<N> or project/<N> (e.g. pr/490, project/4)"
        ),
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

    # Dispatch
    if args.subcommand == "init":
        cmd_init(args, config, agent_name)
    elif args.subcommand == "flip":
        cmd_flip(args, config, agent_name)
    elif args.subcommand == "claim":
        cmd_claim(args, config, agent_name)
    elif args.subcommand == "release":
        cmd_release(args, config, agent_name)
    elif args.subcommand == "status":
        cmd_status(args, config, agent_name)
    elif args.subcommand == "mine":
        cmd_mine(args, config, agent_name)
    elif args.subcommand == "show":
        cmd_show(args, config, agent_name)
    elif args.subcommand == "close":
        cmd_close(args, config, agent_name)
    elif args.subcommand == "reopen":
        cmd_reopen(args, config, agent_name)
    elif args.subcommand == "rename":
        cmd_rename(args, config, agent_name)
    elif args.subcommand == "anchor":
        cmd_anchor(args, config, agent_name)
    elif args.subcommand == "merge":
        cmd_merge(args, config, agent_name)
    else:
        parser.print_help()
        sys.exit(EXIT_VALIDATION)


if __name__ == "__main__":
    main()
