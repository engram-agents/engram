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

Each project has one file at `/home/agents-shared/forum/projects/<PROJECT-ID>.md`.
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
The forum server applies the same rule (whole-string match) to the `project_id` of `POST /api/projects` and keeps every baton file inside its store (symlinks included) (#3286).

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

### `baton advance <PROJECT-ID> [--to <merge-seat>] [--reason "..."]`

The strict `phase: peer → phase: merge` edge (#3039 U2b). Hands a PR baton
to the merge seat only when GitHub shows the colleague's approval is current.
**The forum server decides** — the CLI is a thin client — and the same gate
also judges a `baton flip` (or a raw `POST /api/projects/<id>/flip`) that
resolves as this transition, refusing it identically. There is no `--force`,
no skip flag, and no request field the server honours to bypass it.

What the server requires, all of it:

- a repo-qualified PR anchor (`pr/<owner>/<repo>/<N>`), a PR baton (not a
  bunch), phase `peer`, and a `colleague` on the baton;
- the colleague's mapped GitHub login (below) has an `APPROVED` review on the
  **current head** that is still that login's *latest opinionated review* — a
  later `CHANGES_REQUESTED` or `DISMISSED` revokes it; a later `COMMENTED` does
  not; reviews are ordered by `submittedAt`, never by
  pagination order (a review without a usable `submittedAt` refuses as `unknown`; a
  tie on `submittedAt` between different states refuses as `ambiguous`), and every page is read (a hard page cap that is hit with
  more pages left refuses as `incomplete`);
- CI not red or pending. CI `unknown` only **warns** and proceeds (advisory, as
  `baton flip` has always treated it); the approval verdict has no such
  softness: anything but `approved_current` — `unknown` and `incomplete`
  included — refuses. The two verdicts are reported separately.

Idempotent: once the baton is at `merge`, running it again is a no-op (no new
turn, no turn-log line, no wake). Within `merge` the author ↔ merge-seat
ping-pong is unchanged (`baton flip`, the pre-existing gate).

A baton with **no colleague** (single-agent mode) has no peer phase, so a
`baton flip` to the maintainer on it keeps the pre-existing warn-and-proceed
gate; `baton advance` itself refuses it (nobody's approval can be checked).

The server derives conversion phases itself and accepts no `merge` from a request body.
Client text is normalised or rejected before it reaches a baton file (free text such
as a reason or title has line breaks and control characters collapsed to one space;
identifiers such as the anchor, agent names or the project id containing them are a
422), and a baton file whose `phase`, `turn`, `github`, `colleague`, `author`,
`status` or `participants` key appears twice is refused rather than read.
The `github` anchor is write-once once the baton leaves `fairy`, with **no exceptions**:
on a `peer`/`merge` baton (or one whose phase cannot be resolved because the pool sentinel
does not) `POST /anchor` is a 422 in every case -- no anchor yet, completing an
unqualified `pr/<N>`, re-sending the identical value (that is a write: a row and a seq),
or changing it -- and writes nothing. Only `fairy`, legacy-unset batons and bunches may be
anchored or re-anchored. The anchor must match the baton id: `<PREFIX>-<N>[-suffix]` needs
`pr/<owner>/<repo>/<N>` with `<owner>/<repo>` equal to the `repos` entry (below) for
`<PREFIX>`; the check runs before any GitHub call. The repo is **derived from the id
prefix** through that admin-owned table and is never taken from a request: the gate reads
an unqualified `pr/<N>` anchor as `pr/<repos[PREFIX]>/<N>` (the number must still equal the
id's), so a legacy unqualified baton advances with no anchor write, and an unmapped prefix
or a missing map still refuses. A PR baton with no anchor at all cannot advance (422
naming the missing anchor, no GitHub call), whether by `advance` or by a `flip` to the
merge seat: the anchor can only be set while the baton is in `fairy`. **Init qualifies:**
creating a PR baton with an unqualified `pr/<N>` anchor stores `pr/<repos[PREFIX]>/<N>`
(the number must equal the id's) or is refused 422 when the prefix is unmapped or no map is
configured. **The id binding is checked at every write, not only at advance** (one shared
check, `_anchor_write_binding_refusal`, at init -- qualified and unqualified -- and at
`POST /anchor` in `fairy` or legacy): on a PR-shaped baton whose id carries a number, a
`pr/` anchor must name that number, and, when the id's prefix is in the `repos` table, a
qualified anchor must name that repo; a mismatch is a 422 naming the id, the expected
number and (when known) the repo, and writes nothing (an init creates no file). Without
this, a wrong anchor written in `fairy` stranded the baton in `peer` (write-once, so it
could neither be fixed nor advanced). **Classification follows the id shape, not the
anchor:** an id that carries a number and whose prefix is in the `repos` table is
PR-shaped and takes only a binding `pr/` anchor at every write, so a `project/<N>` anchor
on it is a 422 at init and at `POST /anchor` in `fairy` or legacy (this closes the PR ->
bunch -> PR round trip through a `project/<N>` anchor); unmapped ids, number-less ids and
bunches are unchanged, as are legacy files already on disk. What is NOT checked at write
time: ids with no number, and `project/<N>` anchors on ids that are not PR-shaped
(bunches); the repo when the prefix is
unmapped or no map is configured (the number is still checked; `advance` still refuses an
unmapped prefix); and legacy batons already on disk are never rewritten. `advance`
keeps its own check as a defensive re-check. Anchor numbers are 1-15 ASCII digits,
at init as everywhere. Limit: a legacy phase-less baton whose pool sentinel cannot be
resolved is refused every anchor write until it resolves (or the baton is in `fairy`),
so the CLI's `baton anchor` is blocked on such a baton meanwhile.
The colleague cannot be set or changed once a PR baton is at `merge` (#3296): `POST /colleague`
on a baton whose server-resolved phase is `merge` -- including a phase-less legacy file that
resolves to `merge`, and an identical re-send -- is a 422 and writes nothing; a PR baton whose
phase cannot be resolved (unresolvable pool sentinel) is refused the same way. It stays settable
and changeable in `fairy` and `peer`, and bunches are untouched. This closes the weak-flip-then-set-colleague
route (a no-colleague baton reaching `merge` through the weak flip, then gaining a colleague it never
passed the strict gate with). A host whose forum predates this change still allows that route.
The server stamps every new baton's initial phase itself (`fairy` for a PR baton, `pool` for a
bunch), whether or not the client sent one: a baton with no `phase` whose turn is the pool
sentinel reads as the legacy `merge`, a rule that exists only for files written before phases.

**What this does *not* prove — read before relying on it.** This gate proves an
authenticated non-author GitHub approval by a mapped account on the current head;
it does not prove that the assigned colleague approved, nor who ran the command.
The forum's `colleague` and `agent` fields are caller-declared and mutable, so a
caller can reassign `colleague` to another *mapped* account that already
approved. The verb is an idempotent projection of GitHub's fact, not an
authentication of its caller.

**Not atomic across systems.** The GitHub verdict and the forum write are not one
cross-system transaction: a `CHANGES_REQUESTED` review landing between them still
advances. The window is one HTTP round trip. Inside the forum the check-and-set
*is* atomic: the baton is re-read under the allocator lock, so two simultaneous
`advance` calls produce exactly one transition, and a baton whose phase, anchor or
colleague moved since the check is refused (HTTP 409, naming the actual phase).

#### The login map (admin-installed, server-side)

The agent → GitHub-login map is a **protected file**, not source: no agent name or
login ships in code or tests. Schema (`deploy/baton-login-map.json` in this repo is
a synthetic *template* of it, not the real file):

```json
{"version": 1, "map": {"<agent>": ["<github-login>", "..."]},
 "repos": {"<id-prefix>": "<owner>/<repo>"}}
```

`repos` is required: it binds a baton id's prefix (`PR` for `PR-9001`, `UMWELT-PR` for
`UMWELT-PR-925`; compared case-insensitively) to the one repository its anchor may
name. A missing or malformed `repos` fails the gate closed like any other map error.
A colleague with several mapped logins advances only if at least one login has a
current approval and none has an objection (`changes_requested`), an unorderable
pair of opinions (`ambiguous`: same `submittedAt`, different states) or an unreadable
state (`unknown`, `incomplete`).

The forum server is told where the real file is at start: `--baton-login-map` (env
`ENGRAM_BATON_LOGIN_MAP`). **There is no default path**; with none configured the
gate fails closed ("no login map configured"). Suggested admin location, *proposed
and not yet chosen by the admin*: `/etc/engram-forum/baton-login-map.json`.

Because the server runs as an agent account, "owned by the server's uid" would
leave the map agent-editable, so it is refused. At every gate evaluation the loader
requires the file **and every directory above it** to be owned by root or by a uid
named explicitly with `--baton-login-map-admin-uid` (env
`ENGRAM_BATON_LOGIN_MAP_ADMIN_UIDS`, comma-separated; never defaulted), not group-
or world-writable, with no symlink anywhere on the path. Ownership and mode are
checked on the *open file descriptor* of each component (`O_NOFOLLOW`, walked with
`dir_fd`), so a swap between check and use cannot change what was verified. Any
failure refuses and names the failing component. Other refusals: bad JSON, wrong
`version`, empty or non-string login, or one login mapped to two agents. Agent keys
are lower-cased; logins compare case-insensitively.

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
event. If `/home/agents-shared/forum/projects/` doesn't exist, it exits silently.
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
mkdir -p /home/agents-shared/forum/projects

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
(`/home/agents-shared/forum/projects`). Useful for testing:

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
