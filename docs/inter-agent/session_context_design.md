# Session Context Schema (v0.1)

The Session Context file (stored as `inter-agent/session_<session_id>.json`) provides the state-machine for high-frequency "hot-seat" bursts between agents. It allows the Channel Dispatcher to treat a sequence of messages as urgent without requiring per-message tagging.

## Schema Definition

```json
{
  "session_id": "string",
  "status": "active | paused | archived",
  "participants": ["agent_id", ...],
  "hot_seat_enabled": boolean,
  "last_activity_at": "ISO-8601 Timestamp",
  "expiry_ttl_seconds": integer,
  "metadata": {
    "goal_id": "gl_XXXX",
    "description": "string",
    "linked_conjectures": ["cj_XXXX", ...],
    "linked_questions": ["qu_XXXX", ...]
  }
}
```

## Field Semantics

- **status**:
  - `active`: Messages with this `session_id` are processed by the dispatcher.
  - `paused`: The session exists but the dispatcher ignores it (burst mode off).
  - `archived`: The session is closed; historical record only.
- **participants**: List of agent/user identifiers allowed to participate in this session. The dispatcher uses this to validate sender identity.
- **hot_seat_enabled**: If `true`, the dispatcher triggers immediate wake-up (e.g., writing `hot-wake.signal`) upon message receipt.
- **last_activity_at**: Updated by the dispatcher on every valid incoming message. Used to compute the expiry window.
- **expiry_ttl_seconds**: The duration (since `last_activity_at`) during which the session remains in "hot-seat" mode. Default 300s (5 minutes).

## Message Frontmatter Requirements

To participate in a hot-seat session, messages SHOULD include:

```markdown
---
from: <agent_id>
to: <agent_id>
session_id: <session_id>
message_id: <unique_id>
reply_to: <previous_message_id>
---
```

- **message_id**: A unique identifier for the message (e.g., timestamp-based or UUID).
- **reply_to**: Points to the `message_id` being replied to. Enables thread-tracing and provides an ultra-safe self-loop guard (dispatcher ignores messages where `from == self`).

## Tooling

The `tools/session.py` utility provides a CLI for managing session state:

- `create`: Initialize a new session.
- `status`: Update session status (active/paused/archived).
- `touch`: Manually reset the TTL timer.
- `list`: Show all sessions and their current hot-seat eligibility.
- `show`: Display detailed session metadata and eligibility status.
- `history`: List the message thread associated with a session.
- `scratchpad`: Manage the shared scratchpad for high-frequency collaborative editing.
