#!/usr/bin/env python3
"""baton-migrate-to-coord-store — one-shot migration of existing baton files.

Scans an existing BATON_PROJECTS_DIR for *.md baton files, parses their
frontmatter, and POSTs each to the forum API's POST /api/projects (init route)
so the coord store takes ownership of the files going forward.

Run AFTER Phase 2 routes are live and BEFORE baton.py Phase 3 goes into
production.  After a successful migration, the baton files live inside the
coord store at $FORUM_HOME/projects/ and are seq-stamped; baton.py's
thin-client writes route through the forum API and appear on the /api/updates
feed.

Usage:
    python3 baton-migrate-to-coord-store.py [--projects-dir PATH]
                                             [--forum-url URL]
                                             [--agent NAME]
                                             [--dry-run]

Options:
    --projects-dir PATH   Source directory of *.md baton files.
                          Default: BATON_PROJECTS_DIR env var or
                          $FORUM_HOME/projects/ (post-Phase-3 default).
    --forum-url URL       Forum API base URL.
                          Default: config.json forum.url or $FORUM_URL or
                          http://localhost:5002.
    --agent NAME          Agent name to stamp as the importer.
                          Default: resolved from config.json / $USER.
    --dry-run             Print what would be POSTed without POSTing.

Note:
    Turn-log history is not preserved — the server writes a fresh body on init.
    Closed batons (status merged or cancelled) are not imported: the server's init
    route refuses a closed status with a 422, because a baton is born open (#3901),
    so posting them would only FAIL. Each is reported as a SKIP line, counted in the
    summary's "closed" tally (separate from skipped and failed), and does not make
    the run fail. Dry-run reports the same lines.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Bootstrap: ensure forum_api is importable from tools/
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
from forum_api import ForumClient, ForumHttpError, ForumNetworkError, engram_home, forum_url_from_config

# Mirrors forum.coordination.store_file.CLOSED_STATUSES, the set the server's init
# route refuses (#3901). Local copy: this tool must run without the forum package.
CLOSED_STATUSES = frozenset({"merged", "cancelled"})

# ---------------------------------------------------------------------------
# Frontmatter parsing (mirrors baton.py / coordination/markdown.py)
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FRONTMATTER_FIELD_RE = re.compile(r"^(\w[\w-]*):[ \t]*(.*)$", re.MULTILINE)
_PARTICIPANTS_LIST_RE = re.compile(r"^\[?(.*?)\]?$")


def _parse_frontmatter(text: str) -> tuple:
    """Return (fields_dict, body_text) from a baton markdown file."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fields: dict = {}
    for field_m in _FRONTMATTER_FIELD_RE.finditer(m.group(1)):
        fields[field_m.group(1).strip().lower()] = field_m.group(2).strip()
    return fields, text[m.end():]


def _parse_participants(participants_str: str) -> list:
    """Parse '[borges, ariadne]' or 'borges,ariadne' → list of names."""
    m = _PARTICIPANTS_LIST_RE.match(participants_str.strip())
    inner = m.group(1) if m else participants_str
    return [p.strip().lower() for p in inner.split(",") if p.strip()]


# ---------------------------------------------------------------------------
# Config helpers (same as baton.py)
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    # forum_api.engram_home() is the ONE resolution (#3863): ValueError on a relative ENGRAM_HOME (main refuses).
    config_path = engram_home() / "config.json"
    if config_path.exists():
        try:
            parsed = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
        else:
            # #4229: a non-dict top level (hand-edited) is treated as no config.
            if isinstance(parsed, dict):
                return parsed
    return {}


def _get_agent_name(config: dict) -> str:
    _v = config.get("agent_name")  # #4137: null / non-string -> ""
    name = _v.strip() if isinstance(_v, str) else ""
    if name:
        return name
    for envvar in ("USER", "LOGNAME"):
        username = os.environ.get(envvar, "").strip()
        if username:
            return username.removeprefix("agent-")
    try:
        return pwd.getpwuid(os.getuid()).pw_name.removeprefix("agent-")
    except KeyError:
        return "baton-migrator"


# ---------------------------------------------------------------------------
# Main migration logic
# ---------------------------------------------------------------------------

def migrate(
    projects_dir: Path,
    client: ForumClient,
    agent: str,
    dry_run: bool,
    tally: Optional[dict] = None,
) -> tuple[int, int, int]:
    """Scan projects_dir and POST each baton to the coord store.

    Returns (imported, skipped, failed). Closed legacy batons (merged / cancelled)
    are not POSTed (init refuses them, #3901); they are counted separately and, when
    ``tally`` is a dict, reported as ``tally["closed"]``.
    """
    if not projects_dir.is_dir():
        print(f"baton-migrate: projects directory not found: {projects_dir}", file=sys.stderr)
        return 0, 0, 0

    imported = skipped = failed = closed = 0
    if tally is not None:
        tally["closed"] = 0

    for md_file in sorted(projects_dir.glob("*.md")):
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"  SKIP  {md_file.name}: cannot read — {e}", file=sys.stderr)
            skipped += 1
            continue

        fields, _body = _parse_frontmatter(text)
        if not fields:
            print(f"  SKIP  {md_file.name}: no parseable frontmatter")
            skipped += 1
            continue

        project_id = fields.get("project", "").strip() or md_file.stem
        title = fields.get("title", "").strip() or project_id
        status = fields.get("status", "planning").strip().lower()
        turn = fields.get("turn", "").strip().lower()
        turn_reason = fields.get("turn_reason", "migrated from legacy projects dir").strip()
        # Strip surrounding quotes from turn_reason if present
        if turn_reason.startswith('"') and turn_reason.endswith('"'):
            turn_reason = turn_reason[1:-1]
        if not turn_reason:
            turn_reason = "migrated from legacy projects dir"
        turn_since = fields.get("turn_since", "").strip()
        participants_str = fields.get("participants", "").strip()
        participants = _parse_participants(participants_str) if participants_str else []
        github = fields.get("github", "").strip() or None

        # #3901: a baton is born open; init refuses a closed status, so never POST one.
        if status in CLOSED_STATUSES:
            print(f"  SKIP  {project_id}: closed ({status}); closed batons are not imported "
                  f"\u2014 a baton is born open (#3901)")
            closed += 1
            if tally is not None:
                tally["closed"] = closed
            continue

        # Minimal validation before attempting POST
        if not turn:
            print(f"  SKIP  {project_id}: missing turn field")
            skipped += 1
            continue
        if not participants:
            print(f"  SKIP  {project_id}: missing participants field")
            skipped += 1
            continue

        payload = {
            "agent": agent,
            "project_id": project_id,
            "title": title,
            "status": status,
            "turn": turn,
            "turn_reason": turn_reason,
            "github": github,
            "participants": participants,
        }

        if dry_run:
            print(f"  DRY   {project_id}: would POST {json.dumps(payload, separators=(',', ':'))}")
            imported += 1
            continue

        try:
            result = client.post("/api/projects", payload)
            seq = result.get("seq", "?")
            print(f"  OK    {project_id}: seq={seq} turn={turn} status={status}")
            imported += 1
        except ForumHttpError as e:
            if e.status == 409:
                print(f"  SKIP  {project_id}: already exists in coord store (409)")
                skipped += 1
            else:
                print(f"  FAIL  {project_id}: HTTP {e.status} — {e.body}", file=sys.stderr)
                failed += 1
        except ForumNetworkError as e:
            print(f"  FAIL  {project_id}: network error — {e}", file=sys.stderr)
            failed += 1

    return imported, skipped, failed


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="baton-migrate-to-coord-store",
        description=(
            "One-shot migration: import existing baton *.md files into the "
            "forum coord store via POST /api/projects."
        ),
    )
    parser.add_argument(
        "--projects-dir", metavar="PATH",
        help=(
            "Source directory of *.md baton files. "
            "Default: BATON_PROJECTS_DIR env var, or $FORUM_HOME/projects/"
        ),
    )
    parser.add_argument(
        "--forum-url", metavar="URL",
        help=(
            "Forum API base URL. "
            "Default: config.json forum.url → $FORUM_URL → http://localhost:5002"
        ),
    )
    parser.add_argument(
        "--agent", metavar="NAME",
        help="Agent name to stamp as importer. Default: resolved from config.json / $USER.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Print what would be POSTed without POSTing.",
    )
    args = parser.parse_args()

    try:
        config = _load_config()
    except ValueError as exc:  # a relative ENGRAM_HOME: refused before anything is read or POSTed (#3863)
        print(f"baton-migrate: {exc}", file=sys.stderr)
        sys.exit(2)

    # Resolve projects-dir
    if args.projects_dir:
        projects_dir = Path(args.projects_dir)
    else:
        env_dir = os.environ.get("BATON_PROJECTS_DIR")
        if env_dir:
            projects_dir = Path(env_dir)
        else:
            forum_home = os.environ.get("FORUM_HOME", "/home/agents-shared/forum")
            projects_dir = Path(forum_home) / "projects"

    # Resolve forum URL
    # #4203: forum_api.forum_url_from_config is the same config -> $FORUM_URL -> default
    # chain, with the type guards (a hand-edited non-dict forum / non-string url is absent).
    forum_url = args.forum_url or forum_url_from_config(config)

    # Resolve agent name
    agent = (args.agent or _get_agent_name(config) or "baton-migrator").strip().lower()

    client = ForumClient(forum_url)

    mode = "DRY-RUN" if args.dry_run else "LIVE"
    print(f"baton-migrate-to-coord-store [{mode}]")
    print(f"  projects-dir: {projects_dir}")
    print(f"  forum-url:    {forum_url}")
    print(f"  agent:        {agent}")
    print()

    tally: dict = {}
    imported, skipped, failed = migrate(projects_dir, client, agent, args.dry_run, tally=tally)

    print()
    action = "would import" if args.dry_run else "imported"
    print(f"Summary: {action} {imported}, skipped {skipped}, "
          f"closed {tally.get('closed', 0)}, failed {failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
