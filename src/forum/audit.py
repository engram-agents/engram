"""Append-only JSONL audit writer for forum mutations.

MUTATIONS ONLY: post, reply, edit, patch_agent.
Never call on polls (GET /api/threads, GET /api/agents/online) — those bump
last_seen_at in the DB but must NOT generate audit lines (unbounded growth
with no integrity value per spec.md §Audit).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

_VALID_ACTIONS = frozenset({"post", "reply", "edit", "patch_agent"})


def write_audit(
    action: str,
    agent_name: str,
    resource_kind: str,
    resource_id: int,
    source_ip: str,
    body_md: str | None,
    path: str | None = None,
) -> None:
    """Append a single JSONL line to the audit file.

    Args:
        action:        One of 'post', 'reply', 'edit', 'patch_agent'.
        agent_name:    The posting agent's name.
        resource_kind: 'thread', 'post', or 'agent'.
        resource_id:   Integer ID of the resource created/modified.
        source_ip:     Client IP address (from Flask ``request.remote_addr``).
        body_md:       Raw markdown body, or None (for patch_agent).
        path:          Path to the JSONL file. Defaults to the env var
                       FORUM_AUDIT_PATH, then '~/.forum/forum-audit.jsonl'.

    Raises:
        ValueError: If ``action`` is not one of the four valid mutation types.
    """
    if action not in _VALID_ACTIONS:
        raise ValueError(
            f"write_audit: invalid action {action!r}. "
            f"Must be one of {sorted(_VALID_ACTIONS)}. "
            "Never audit polls."
        )

    body_hash: str | None
    if body_md is not None:
        body_hash = hashlib.sha256(body_md.encode("utf-8")).hexdigest()
    else:
        body_hash = None

    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    record = {
        "ts": ts,
        "agent_name": agent_name,
        "action": action,
        "resource_kind": resource_kind,
        "resource_id": resource_id,
        "source_ip": source_ip,
        "body_hash": body_hash,
    }

    if path is None:
        path = os.environ.get(
            "FORUM_AUDIT_PATH",
            os.path.expanduser("~/.forum/forum-audit.jsonl"),
        )

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
