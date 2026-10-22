# Hot-Seat Shared Scratchpad (v0.1)

> **STATUS: retired design, never implemented.** This is a proposal built
> atop the v0 "Channel Dispatcher" hot-seat architecture (see
> session_context_design.md's status note), which README.md §7/§9 already
> documents as retired. The proposed extension point, `tools/session.py`,
> was deleted as dead code in #2857 step 2. `tools/_channel.py`'s
> `atomic_write` pattern referenced below still exists in the repo, but is
> no longer a shipped mechanism (reclassified to `excluded[]` in the same
> PR — its only live importer is `tools/telegram_bot.py`).

The Shared Scratchpad is a same-filesystem primitive for high-frequency collaborative editing between agents. It provides a semi-structured space for drafting, state-tracking, and brainstorming that persists across messages within a "hot-seat" session.

## Storage

Scratchpads are stored in the inter-agent channel directory as:
`inter-agent/scratchpad_<session_id>.md`

## Lifecycle

- **Creation**: A scratchpad is initialized by any participant of an active session.
- **Access**: Only participants listed in `session_<session_id>.json` SHOULD edit the scratchpad.
- **Cleanup**: When a session is moved to `archived` status, the scratchpad SHOULD be archived or deleted.

## Structure (Optional)

While scratchpads are primarily free-form Markdown, they may use "slots" or "blocks" for concurrent work:

```markdown
# Scratchpad: <session_id>

## [slot:mneme]
Current draft for the dispatcher logic...

## [slot:borges]
Reviewing the schema...

## [shared:goals]
1. Implement participants validation
2. Design scratchpad primitive
```

## Tooling (Proposed)

A new tool (or extension to `tools/session.py`) could manage scratchpad operations:

- `scratchpad read <session_id>`: Read the current scratchpad content.
- `scratchpad write <session_id> <content>`: Overwrite the scratchpad.
- `scratchpad edit <session_id> --slot <slot_name> <content>`: Update a specific slot in the scratchpad.

## Atomic Edits

Since multiple agents might write to the scratchpad, the `atomic_write` (temp+rename) pattern from `_channel.py` MUST be used. For slot-based editing, a read-modify-write cycle is required, ideally with a file-locking mechanism if frequency is extremely high.
