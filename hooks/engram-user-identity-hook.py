#!/usr/bin/env python3
"""
UserPromptSubmit hook: detect speaker identity from name-prefix convention.

Uses sticky session context (ob_0681): a name prefix like "Lin:" switches
the active user, and all subsequent messages are attributed to that user
until a different prefix (e.g. "Lei:") overrides it. The marker file
(current_user.json) is cleared on SessionStart so identity doesn't leak
across sessions.

Convention: type "Lin: <message>" once to switch context to Lin.
All following messages are treated as Lin's until "Lei: <message>" resets.

Exit codes:
  0 - success (may output empty JSON if no identity detected)
  1 - non-blocking error
"""

import json
import os
import re
import sqlite3
import sys

DB_PATH = os.path.expanduser("~/.engram/knowledge.db")
CURRENT_USER_PATH = os.path.expanduser("~/.engram/current_user.json")


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
        print(json.dumps({"additionalContext": context}))
    else:
        # No prefix — use sticky session context if available
        if os.path.exists(CURRENT_USER_PATH):
            try:
                with open(CURRENT_USER_PATH) as f:
                    sticky = json.load(f)
                # Only output context for non-default users (Lei is default)
                name_lower = sticky.get("name", "").lower()
                if sticky.get("person_id") and not name_lower.startswith("lei"):
                    context = (
                        f"[ENGRAM User Identity: {sticky['name']} ({sticky['person_id']}) — sticky session context]\n"
                        f"  Role: {sticky.get('role', '')}\n"
                        f"  Profile: {sticky.get('description', '')}"
                    )
                    print(json.dumps({"additionalContext": context}))
                else:
                    print(json.dumps({}))
            except (json.JSONDecodeError, OSError):
                print(json.dumps({}))
        else:
            print(json.dumps({}))


if __name__ == "__main__":
    main()
