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
            with open(CURRENT_USER_PATH, "w") as f:
                json.dump(speaker, f)
        except OSError:
            pass
        context = (
            f"[ENGRAM User Identity: {speaker['name']} ({speaker['person_id']}) — session context switched]\n"
            f"  Role: {speaker['role']}\n"
            f"  Profile: {speaker['description']}"
        )
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}))
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
                    context = (
                        f"[ENGRAM User Identity: {sticky['name']} ({sticky['person_id']}) — sticky session context]\n"
                        f"  Role: {sticky.get('role', '')}\n"
                        f"  Profile: {sticky.get('description', '')}"
                    )
                    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}))
                else:
                    print(json.dumps({}))
            except (json.JSONDecodeError, OSError):
                print(json.dumps({}))
        else:
            print(json.dumps({}))


if __name__ == "__main__":
    main()
