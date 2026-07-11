#!/usr/bin/env python3
"""tripwire — Falsifiability-grade checker for GitHub PR approvals

Usage:
  tripwire check <owner/repo> <PR> [--approver <login>] [--json]

Checks whether an approval on a GitHub PR is falsifiable-grade (the approver
left ≥1 verifiable trace) or say-so-grade (no traces, bare LGTM).

Two signals count as falsifiable:
  1. A commit authored by the approver on the PR branch
  2. A line-anchored review comment by the approver (line or original_line
     non-null) from gh api .../pulls/{PR}/comments

Review bodies from gh api .../reviews do NOT count as traces.

What the grade means (scope — do not over-read):
  - Falsifiable-grade certifies CONTACT with the locus (the approver
    demonstrably engaged a specific line / section / premise), NEVER that
    their conclusion is correct. A commit or line-anchored comment proves the
    approver touched the artifact; it says nothing about whether their take is
    right. Same cut as "attestation-grade != render-fidelity": a grade attaches
    to the event/contact, never to the meaning/correctness layered on it.
  - The property is DELIVERABLE-AGNOSTIC. A falsifiable trace is an attestation
    anchored to a checkable *locus* in the artifact, whatever the artifact is:
    a line (code), a named section (spec / design doc), or a premise (an ENGRAM
    derivation that derives_from it with a real logical_chain). A bare "looks
    sound" attached to no locus is say-so-grade regardless of deliverable type.
    This v0.1 tool checks the CODE instance only (commits + PR line-comments,
    where the gh endpoints live); a locus-aware v2 would generalize to design /
    graph deliverables. The docstring states the property; the tool implements
    one instance of it.

Config inference (when --approver is omitted):
  self_lineage contains "opus"   → approver = agent_name  (self-check)
  self_lineage contains "sonnet" → approver = peer field
  Otherwise or peer missing      → error, exit 2

Config file: ~/.engram/config.json  (override: ENGRAM_CONFIG_PATH env var)

Exit codes:
  0 = falsifiable-grade  (traces found)
  1 = say-so-grade       (no traces; FLAG message printed to stdout)
  2 = error              (gh failure, config error, missing field, etc.)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_SAY_SO = 1
EXIT_ERROR = 2


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get_config_path() -> Path:
    env = os.environ.get("ENGRAM_CONFIG_PATH")
    return Path(env) if env else (Path.home() / ".engram" / "config.json")


def _load_config() -> dict:
    """Load ~/.engram/config.json (or ENGRAM_CONFIG_PATH env var)."""
    p = _get_config_path()
    try:
        return json.loads(p.read_text())
    except FileNotFoundError:
        print(f"tripwire: config not found at {p}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    except json.JSONDecodeError as e:
        print(f"tripwire: config parse error: {e}", file=sys.stderr)
        sys.exit(EXIT_ERROR)


def _infer_approver(config: dict) -> str:
    """Infer the approver login from config based on self_lineage.

    opus   → agent_name (self-check: Opus verifies their own review had traces)
    sonnet → peer field (Sonnet verifies the peer's review had traces)
    """
    lineage = config.get("self_lineage", "")
    if "opus" in lineage:
        name = config.get("agent_name")
        if not name:
            print(
                "tripwire: config missing 'agent_name' (needed for opus self-check); "
                "supply --approver explicitly",
                file=sys.stderr,
            )
            sys.exit(EXIT_ERROR)
        return name
    if "sonnet" in lineage:
        peer = config.get("peer")
        if not peer:
            print(
                "tripwire: config has self_lineage containing 'sonnet' but no 'peer' field; "
                "supply --approver explicitly",
                file=sys.stderr,
            )
            sys.exit(EXIT_ERROR)
        return peer
    print(
        f"tripwire: cannot infer approver from self_lineage={lineage!r}; "
        "supply --approver explicitly",
        file=sys.stderr,
    )
    sys.exit(EXIT_ERROR)


# ---------------------------------------------------------------------------
# gh CLI helpers
# ---------------------------------------------------------------------------

def _gh_run(cmd: list[str]) -> Any:
    """Run a gh CLI command (no shell=True) and return parsed JSON.

    Exits with EXIT_ERROR (2) on non-zero returncode or unparseable output.
    """
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        err = result.stderr.strip() or "(no stderr)"
        print(
            f"tripwire: gh command failed (exit {result.returncode}): {err}",
            file=sys.stderr,
        )
        sys.exit(EXIT_ERROR)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"tripwire: failed to parse gh output: {e}", file=sys.stderr)
        sys.exit(EXIT_ERROR)


# ---------------------------------------------------------------------------
# Trace collectors
# ---------------------------------------------------------------------------

def _get_commits_by(owner_repo: str, pr: int, approver: str) -> list[dict]:
    """Return commits on the PR branch authored by approver.

    gh pr view --json commits returns each commit with:
      .oid              — full SHA
      .authors          — array of {login, name, email, id} (plural, not .author)
      .messageHeadline  — first line of commit message
      .authoredDate     — ISO-8601 author date
    """
    data = _gh_run([
        "gh", "pr", "view", str(pr),
        "--repo", owner_repo,
        "--json", "commits",
    ])
    commits = data.get("commits") or []
    result = []
    for c in commits:
        # .authors is an array — any author match counts
        authors = c.get("authors") or []
        logins = {a.get("login", "").lower() for a in authors if a.get("login")}
        if approver.lower() not in logins:
            continue
        sha7 = (c.get("oid") or "")[:7]
        message = c.get("messageHeadline") or ""
        date = c.get("authoredDate", "")
        result.append({
            "type": "commit",
            "sha": sha7,
            "message": message,
            "date": date,
        })
    return result


def _get_line_comments_by(owner_repo: str, pr: int, approver: str) -> list[dict]:
    """Return line-anchored review comments on the PR by approver.

    Uses gh api repos/<owner>/<repo>/pulls/<PR>/comments (the diff-comment
    endpoint, NOT /reviews).  A comment is line-anchored iff its 'line' or
    'original_line' field is not null.  Comments with both null are NOT counted
    (e.g. a general PR comment submitted without selecting a diff line).
    """
    data = _gh_run([
        "gh", "api", "--paginate",
        f"repos/{owner_repo}/pulls/{pr}/comments",
    ])
    if not isinstance(data, list):
        return []
    result = []
    for comment in data:
        user_obj = comment.get("user") or {}
        login = user_obj.get("login") or ""
        if login.lower() != approver.lower():
            continue
        line = comment.get("line")
        original_line = comment.get("original_line")
        if line is None and original_line is None:
            # Not line-anchored — review body style, does not count
            continue
        result.append({
            "type": "line_comment",
            "path": comment.get("path") or "",
            "line": line if line is not None else original_line,
        })
    return result


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def _format_falsifiable(approver: str, pr: int, owner_repo: str,
                         traces: list[dict]) -> str:
    lines = [
        f"✓ FALSIFIABLE-GRADE: {approver} left traces on PR {pr}",
        f"   repo: {owner_repo}",
        "   traces found:",
    ]
    for t in traces:
        if t["type"] == "commit":
            lines.append(
                f'     - commit {t["sha"]} "{t["message"]}" ({t["date"]})'
            )
        elif t["type"] == "line_comment":
            lines.append(
                f'     - line-anchored comment on {t["path"]}:{t["line"]}'
            )
    return "\n".join(lines)


def _format_say_so(approver: str, pr: int, owner_repo: str) -> str:
    return "\n".join([
        f"\U0001f6a9 SAY-SO-GRADE: {approver} approved PR {pr} with no falsifiable trace",
        f"   repo: {owner_repo}",
        f"   checked: commits by {approver} on branch → NONE",
        f"   checked: line-anchored review comments by {approver} → NONE",
        f"   action: {approver} should add a commit or line-anchored comment before merge",
    ])


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

def cmd_check(args: argparse.Namespace) -> int:
    """Run the falsifiability check. Returns exit code (0, 1, or 2)."""
    # Resolve approver: explicit CLI arg wins; otherwise infer from config.
    if args.approver:
        approver = args.approver
    else:
        config = _load_config()
        approver = _infer_approver(config)

    owner_repo: str = args.repo
    pr: int = args.pr

    # Collect both classes of trace.
    commit_traces = _get_commits_by(owner_repo, pr, approver)
    comment_traces = _get_line_comments_by(owner_repo, pr, approver)
    traces = commit_traces + comment_traces

    if traces:
        if args.json:
            print(json.dumps({
                "grade": "falsifiable",
                "approver": approver,
                "pr": pr,
                "repo": owner_repo,
                "traces": traces,
            }))
        else:
            print(_format_falsifiable(approver, pr, owner_repo, traces))
        return EXIT_OK
    else:
        if args.json:
            print(json.dumps({
                "grade": "say-so",
                "approver": approver,
                "pr": pr,
                "repo": owner_repo,
                "traces": [],
            }))
        else:
            print(_format_say_so(approver, pr, owner_repo))
        return EXIT_SAY_SO


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tripwire",
        description=(
            "Check whether a GitHub PR approval is falsifiable-grade "
            "(the approver left ≥1 verifiable trace) or say-so-grade "
            "(bare LGTM with zero traces)."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    check_p = sub.add_parser(
        "check",
        help="Check PR approval traces for a given approver",
    )
    check_p.add_argument(
        "repo",
        metavar="owner/repo",
        help="GitHub repository, e.g. engram-agents/engram",
    )
    check_p.add_argument(
        "pr",
        type=int,
        metavar="PR",
        help="PR number to check",
    )
    check_p.add_argument(
        "--approver",
        metavar="login",
        help=(
            "GitHub username to check. If omitted, inferred from "
            "~/.engram/config.json based on self_lineage."
        ),
    )
    check_p.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON instead of human text",
    )

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(EXIT_ERROR)

    if args.command == "check":
        sys.exit(cmd_check(args))


if __name__ == "__main__":
    main()
