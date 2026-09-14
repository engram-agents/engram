#!/usr/bin/env python3
"""
UserPromptSubmit hook: detect speaker identity from name-prefix convention.

Uses sticky session context: a name prefix like "<Name>:" switches
the active user, and all subsequent messages are attributed to that user
until a different prefix overrides it. The marker file (current_user.json)
is cleared on SessionStart so identity doesn't leak across sessions.

Convention: type "Alex: <message>" once to switch context to Alex.
All following messages are treated as Alex's until another "<Name>: ..."
prefix resets.

Primary-user suppression: if $ENGRAM_HOME/config.json has a `primary_user` key,
the sticky context line is suppressed when the active user matches (since
the primary speaker is the default — no need to re-announce them on every
prompt). Set via the first-session skill or by hand editing config.json.

Exit codes:
  0 - success (may output empty JSON if no identity detected)
  1 - non-blocking error
"""

import json
import os
import re
import sqlite3
import sys

_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
from _hooklib import atomic_write_json, emit_context
from _harness import detect_harness, get_harness_budgets

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or os.path.expanduser("~/.engram")
)
DB_PATH = os.path.join(ENGRAM_HOME, "knowledge.db")
CURRENT_USER_PATH = os.path.join(ENGRAM_HOME, "current_user.json")
CONFIG_PATH = os.path.join(ENGRAM_HOME, "config.json")


def get_primary_user() -> str:
    """Read primary_user from $ENGRAM_HOME/config.json (lowercased; empty if unset)."""
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""
    return (config.get("primary_user") or "").lower()


def get_person_nodes() -> list[dict]:
    """Read all current person nodes from the ENGRAM database."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, claim, logical_chain, metadata FROM nodes "
            "WHERE type = 'person' AND is_current = 1"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def detect_speaker(prompt: str, people: list[dict]) -> dict | None:
    """Check if prompt starts with a known person's name prefix.

    Returns person info dict if matched, None otherwise.
    Matching is case-insensitive against name and all aliases.
    """
    # Match pattern: "Name:" or "Name :" at start of prompt
    match = re.match(r"^\s*(\w[\w\s]{0,30}?)\s*:\s*", prompt)
    if not match:
        return None

    prefix_name = match.group(1).strip().lower()

    for person in people:
        meta = json.loads(person.get("metadata", "{}"))
        name = meta.get("name", "").lower()
        aliases = [a.lower() for a in meta.get("aliases", [])]
        role = meta.get("role", "")

        # Check against full name and each alias
        all_names = [name] + aliases
        if prefix_name in all_names:
            return {
                "person_id": person["id"],
                "name": meta.get("name", ""),
                "role": role,
                "description": (person.get("logical_chain") or "")[:200],
                "matched_prefix": match.group(1).strip(),
            }

    return None


_MAX_HEAD_NAME_LEN = 100


def _truncate_name_for_head(name: str) -> str:
    """Cap a person name before it goes into the single-line `head` string.

    `head` is one f-string line with no internal `\\n`, so
    `_hooklib._trim_to_line_boundary()` treats any head longer than the
    per-harness head budget as un-boundaried and drops it WHOLE rather than
    truncating it -- an oversized `head` isn't shortened, it's silently
    removed (engram-alpha#2887 colleague review, Ariadne: a ~1722+ char
    person name renders "an identity card with the identity removed").
    Person names carry no length cap at write time (see engram_add_person
    in server.py), so this caps defensively at the call site, comfortably
    short of the ~1800-char drop boundary.
    """
    if len(name) > _MAX_HEAD_NAME_LEN:
        return name[:_MAX_HEAD_NAME_LEN] + "…"
    return name


def _assemble_response(head: str, body_blocks: list) -> dict:
    """Build the UserPromptSubmit response dict for an identity-switch
    announcement, routed through the shared bounded renderer
    `_hooklib.emit_context()` (engram-alpha#2863 WS2).

    This hook only ever fires ONE identity card per prompt (a genuine
    single-atomic-decision block, unlike engram-surface-hook.py's stack of
    independently-firing checks) -- but the card's own `Role:` / `Profile:`
    lines are sourced from free-text person-node metadata with no length
    cap enforced at write time (see engram_add_person in server.py), so the
    render is not the "already a fixed short string" escape-valve case
    either. `head` carries the one must-not-miss fact (WHO the session
    context switched to); `body_blocks` carries the supplementary Role/
    Profile detail, most-important-first, which is what actually absorbs
    any oversized metadata under the bound.

    Returns `{}` when both are empty (mirrors the pre-migration
    early-`{}` paths in `main()` below) -- `emit_context()` is never
    called with two empty inputs.
    """
    if not head and not body_blocks:
        return {}

    harness = detect_harness()
    budgets = get_harness_budgets(harness)
    rendered = emit_context(
        head,
        body_blocks,
        budget=budgets["budget"],
        # Deliberate DEVIATION from engram-surface-hook.py's target=budget
        # choice (#2876): that hook's content is already what the agent
        # receives in full every prompt today, so its soft ceiling is the
        # hard ceiling. This hook's identity card is the opposite case --
        # in the overwhelming majority of real calls it is a few short
        # lines, already well under head_budget, so target=head_budget
        # changes nothing in practice while buying the "trailer+head
        # survives the harness's own inline preview cut" guarantee
        # _harness.py's docstring names as the reason head_budget exists
        # ("exactly what the tripwire hook does when it folds onto the
        # helper"). Only a pathologically long Role/Profile value (no
        # length cap enforced at write time, see this function's own
        # docstring) would ever make this bind.
        target=budgets["head_budget"],
        # The Role line is the more informative of the two body items;
        # letting it run past the soft head_budget (never past the hard
        # budget) beats hard-truncating a legitimately long free-text
        # value at 1,800 chars when up to 9,500 is available.
        allow_single_overflow=True,
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": rendered,
        }
    }


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        print(json.dumps({}))
        sys.exit(0)

    prompt = hook_input.get("prompt", "").strip()
    if not prompt or prompt.startswith("/") or prompt.startswith("!"):
        print(json.dumps({}))
        sys.exit(0)

    people = get_person_nodes()
    if not people:
        print(json.dumps({}))
        sys.exit(0)

    speaker = detect_speaker(prompt, people)

    if speaker:
        # New prefix detected — switch active user
        try:
            atomic_write_json(CURRENT_USER_PATH, speaker)
        except OSError:
            pass
        head = (
            f"[ENGRAM User Identity: {_truncate_name_for_head(speaker['name'])} "
            f"({speaker['person_id']}) — session context switched]"
        )
        body = [
            f"  Role: {speaker['role']}",
            f"  Profile: {speaker['description']}",
        ]
        print(json.dumps(_assemble_response(head, body)))
    else:
        # No prefix — use sticky session context if available
        if os.path.exists(CURRENT_USER_PATH):
            try:
                with open(CURRENT_USER_PATH) as f:
                    sticky = json.load(f)
                # Suppress context for the configured primary user (default
                # speaker — no need to re-announce on every prompt). If no
                # primary_user is configured, show context for all sticky users.
                name_lower = sticky.get("name", "").lower()
                primary = get_primary_user()
                is_primary = primary and name_lower == primary
                if sticky.get("person_id") and not is_primary:
                    head = (
                        f"[ENGRAM User Identity: {_truncate_name_for_head(sticky['name'])} "
                        f"({sticky['person_id']}) — sticky session context]"
                    )
                    body = [
                        f"  Role: {sticky.get('role', '')}",
                        f"  Profile: {sticky.get('description', '')}",
                    ]
                    print(json.dumps(_assemble_response(head, body)))
                else:
                    print(json.dumps({}))
            except (json.JSONDecodeError, OSError):
                print(json.dumps({}))
        else:
            print(json.dumps({}))


if __name__ == "__main__":
    main()
