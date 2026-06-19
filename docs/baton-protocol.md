# baton — Multi-Agent Turn-State Protocol

`baton` is a lightweight CLI for declaring whose move it is on a shared
project. Where `ia` carries asynchronous messages between agents, `baton`
answers the question: "whose court is the ball in right now?"

**Origin:** a real misalignment incident on 2026-05-28 where Borges and
Ariadne disagreed about who was responsible for next action on PR #425 —
not because either read state wrong, but because turn state was never
explicitly declared anywhere. Baton solves this by making the declaration
explicit and mandatory.

---

## Core design principle

**Flipping the baton is an explicit action, not derived from indirect signals.**

This is load-bearing. The incident that motivated baton came from both agents
reading indirect signals (who last commented, when the PR was updated) and
reaching different conclusions. Baton eliminates the ambiguity: turn state
only changes when someone calls `baton flip`. No implicit transitions.

---

## File format

Each project has one file at `/home/agents-shared/projects/<PROJECT-ID>.md`.
The file uses Markdown with YAML frontmatter:

```markdown
---
project: PR-425
title: viz_server global agent selector
status: in-review        # planning | in-progress | in-review | merged | cancelled
turn: ariadne            # who holds the baton right now; "lei" is also valid
turn_since: 2026-05-28T17:28:00Z
turn_reason: "round-3 fix pushed (d81d091); awaiting re-review"
participants: [borges, ariadne]
---

# PR #425 — viz_server global agent selector

[Optional project description, decisions, links. Free-form.]

## Turn log

- 2026-05-28T16:16Z borges → ariadne: colleague-review request
- 2026-05-28T16:23Z ariadne → borges: COMMENTED with CSS-display blocker
- 2026-05-28T17:28Z borges → ariadne: round-3 fix pushed (d81d091)
```

### Frontmatter fields

| Field | Required | Description |
|---|---|---|
| `project` | yes | Project ID (TYPE-ID format, e.g. `PR-425`) |
| `title` | yes | Human-readable project title |
| `status` | yes | `planning` / `in-progress` / `in-review` / `merged` / `cancelled` |
| `turn` | yes | Agent name (canonical, lowercase) or `lei` |
| `turn_since` | yes | ISO-8601 UTC timestamp when baton was last flipped |
| `turn_reason` | yes | Short description of why baton was flipped |
| `participants` | yes | List of agent names (n-agent friendly) |

### Project ID convention

Format: `TYPE-ID`

- Numeric when there's an unambiguous numbering system: `PR-425`, `ISSUE-408`
- Descriptive when there isn't: `DESIGN-trust-tier-v2`, `DESIGN-baton-system`

IDs must match the regex `[A-Za-z][A-Za-z0-9_-]*-[A-Za-z0-9][A-Za-z0-9_-]*`.

### Valid `turn:` values

Any name in the `participants` list, or `lei` (the human stakeholder). The
baton can sit in the human's court when a decision requires human input.

---

## CLI reference

### `baton init <PROJECT-ID> --title "..." --participants borges,ariadne [--status planning] [--turn <agent>]`

Create a new project baton file. Fails if the file already exists.

- `--turn` defaults to the invoking agent (if in participants), else the
  first participant listed.
- `--status` defaults to `planning`.

```bash
baton init PR-425 --title "viz_server global agent selector" \
    --participants borges,ariadne --status in-review
```

### `baton flip <PROJECT-ID> <TO> "<reason>"`

Pass the baton to another participant. Atomically updates `turn:`,
`turn_since:`, and `turn_reason:` in frontmatter, and appends a turn-log
line.

- `TO` must be in `participants` or be `lei`.
- Refuses if the project is `merged` or `cancelled`.
- Write is atomic: tmp file + `os.rename` (no locking needed at our scale).

```bash
baton flip PR-425 ariadne "round-3 fix pushed (d81d091); awaiting re-review"
```

### `baton status [--mine]`

List all active projects (status not in `merged` / `cancelled`), sorted by
`turn_since` ascending (oldest baton first = highest urgency).

`--mine` filters to projects where `turn == invoking agent`.

```bash
baton status        # all active projects
baton status --mine # only mine
```

### `baton mine`

Shorthand for `baton status --mine`.

```bash
baton mine
```

### `baton show <PROJECT-ID>`

Print the full project file (frontmatter + body). Read-only.

```bash
baton show PR-425
```

### `baton close <PROJECT-ID> --status merged|cancelled`

Mark a project closed. Removes it from `baton status` / `baton mine`
listings. Does not delete the file — the turn log and history remain.

```bash
baton close PR-425 --status merged
```

---

## Session-start hook

`hooks/claude/engram-baton-prompt-hook.py` fires on every `UserPromptSubmit`
event. If `/home/agents-shared/projects/` doesn't exist, it exits silently.
If projects exist with `turn == invoking agent` and `status` not closed, it
injects an `additionalContext` block:

```
🎾 3 batons in your court:
  - PR-425 (since 17:28Z, 1h ago) — round-3 fix pushed, awaiting re-review
  - PR-429 (since 16:54Z, 2h ago) — colleague-review request sent
  - DESIGN-trust-tier-v2 (since 11:45Z, 7h ago) — design discussion
```

Projects are sorted oldest-first (most overdue at the top).

---

## Manual deployment for multi-agent operators

Baton is not installed by `install.sh` — it requires a shared filesystem
between agents, which only exists in multi-agent setups. See also
`templates/template.CLAUDE.multi-agent.md` for the CLAUDE.md additions that
activate the `ia` and `baton` skill-loading triggers in an agent's live config.

```bash
# Copy baton CLI to shared bin (both agents must be able to run it)
cp tools/baton.py /home/agents-shared/bin/baton
chmod +x /home/agents-shared/bin/baton

# Copy hook to each agent's engram hooks directory
cp hooks/claude/engram-baton-prompt-hook.py ~/.engram/hooks/
cp hooks/claude/engram-baton-prompt-hook.py /home/agent-ariadne/.engram/hooks/
# (adjust paths for your install)

# Create the projects directory
mkdir -p /home/agents-shared/projects

# Register the hook in each agent's settings.json
# Add to the UserPromptSubmit hooks array:
# {
#   "type": "command",
#   "command": "python3 /home/<agent>/.engram/hooks/engram-baton-prompt-hook.py",
#   "timeout": 5,
#   "statusMessage": "Checking baton turn state..."
# }
```

The `BATON_PROJECTS_DIR` environment variable overrides the default path
(`/home/agents-shared/projects`). Useful for testing:

```bash
BATON_PROJECTS_DIR=/tmp/baton-smoke baton init PR-999 --title "smoke" \
    --participants alice,bob
```

---

## Discipline rule

After taking an action that hands off to a counterparty — posting a PR
review, pushing a round-N fix, sending a colleague-review request — `baton
flip` is part of the action. Same pattern as `ia write` after reading a
letter.

**Wrong**: push fix, then remember to flip later (creates the exact ambiguity
baton was built to eliminate).

**Right**: push fix → `baton flip PR-425 ariadne "round-3 fix pushed (d81d091)"`.

The full discipline rule will be encoded in `CLAUDE.md` (separate edit —
not part of this PR).

---

## Single-agent mode

Single-agent users (those without a counterpart agent on
the same host) are completely unaffected:

- `baton status` and `baton mine` exit 0 silently if the projects directory
  doesn't exist.
- The hook exits 0 silently if the projects directory doesn't exist.
- No config change, no install change.

---

## Future: agentctl integration

Manual deployment (above) is the current state — same as `ia` today. Issue
#50 tracks automated multi-agent deployment via `agentctl`. When that lands,
baton deployment will be absorbed into the operator setup flow.
