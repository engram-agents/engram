---
name: engram-baton
description: Use when sending or reading inter-agent turn-state via the `baton` CLI. Carries the Project-layer vs PR-layer vocabulary discipline (claim/release vs flip), bunch-as-Project mapping, completeness-check-as-Done-gate, and the auto-pull hook's render semantics that `baton --help` can't carry cleanly.
---

# Inter-Agent Turn-State — `baton` Workflow

The `baton` CLI (`tools/baton.py`, shared at `/home/agents-shared/bin/baton`) is the sanctioned interface to the multi-agent turn-state system at `/home/agents-shared/projects/`. Files are flat YAML-frontmatter Markdown; the CLI guarantees atomic edits + audit-trail consistency. The auto-pull hook (`engram-baton-prompt-hook.py`) reads these files every prompt and injects live status into the agent's context.

## When to use

- Claiming a Project (bunch) of work — `baton claim <pool-X>`
- Releasing a Project back to the pool when done — `baton release <pool-X> --done`
- Passing a per-PR cursor to the next actor (fairy → reviewer → colleague → maintainer) — `baton flip <PR-N> <to> "<reason>"`
- Renaming a Project's title (e.g., add `(done)` post-completion) — `baton rename <id> --title "<new>"`
- Reading the current state of all active projects — `baton status` (also auto-injected into every prompt via the hook)
- Closing an archived Project — `baton close <id> --status merged|cancelled`

## When NOT to use

- Single-agent mode (no `/home/agents-shared/`) — the CLI exits with a clear error; the discipline is moot.
- Substantive coordination — letters (`ia write`) carry colleague verdicts and discoveries; batons carry turn-state only. Both channels are load-bearing.
- Editing another agent's project file directly via sed/vi — protocol violation. Use the CLI.

---

## The two-layer vocabulary (load-bearing)

### Project layer — `claim` / `release`

A **Project** (bunch) is a topic-coherent or defect-class-coherent unit of work — e.g., `pool-multi-install`, `pool-data-correctness`, `class-env-var-precedence`. Conventions:

- **One driver per Project**. The agent who claims it owns the work; the counterpart is reviewer + discussion partner, not co-driver. (maintainer's design clarification: Project = single-owner; rarely passes between agents.)
- **`baton claim <project-id>`** — take ownership from the pool sentinel. Refuses if already claimed (no stealing).
- **`baton release <project-id> [--done] [--reason TEXT]`** — relinquish back to the pool. `--done` appends `(done)` to the title idempotently.
- Pool sentinel = the primary user's name (e.g. `{{USER_NAME}}`). When `turn:` equals that name and title is unsuffixed → available; title suffixed `(done)` → archived; title suffixed `(awaiting-<user>)` → mid-work, blocking on the primary user's input.

### PR layer — `flip`

A **PR-baton** (e.g., `PR-487`) tracks the per-PR turn cursor through the review pipeline:
1. Coder-fairy opens PR → `baton init PR-N --title "<short>" --participants alice,bob --turn alice --status in-progress`
2. Reviewer-fairy converges → `baton flip PR-N bob "fairy-converged; colleague request"`
3. Colleague review → `baton flip PR-N {{USER_NAME}} "colleague-APPROVED"`
4. Maintainer merge → eventual `baton close PR-N --status merged`

Frequent passes between agents. Don't use `claim`/`release` here — `flip` is the right verb for "I'm passing the next action."

**The verb tells the reader the genre**: `flip` = transient cursor; `claim`/`release` = persistent ownership.

---

## Auto-pull hook (live GitHub status)

`hooks/claude/engram-baton-prompt-hook.py` injects each in-court baton's live state into every prompt:

- **PR-anchored batons** (project-id matches `^PR-\d+$` OR has `github: pr/N` frontmatter): render shows `OPEN · review:APPROVED`, `MERGED`, `CLOSED`.
- **Project-anchored batons** (`github: project/N` frontmatter): render shows status tally like `2 Done · 1 In Progress · 2 Blocked`.

**Cache**: 30s TTL at `$ENGRAM_HOME/baton-status-cache.json`. On gh failure → stale-cache fallback (last-known-good is better than blank). The clause is informational; coordination flows through the `turn:` field + substantive letters, not through cached gh state.

If the live clause feels stale or contradicts what you know, verify with a fresh `gh pr view <N>` or `gh project item-list <N>` rather than trusting the hint blindly.

### Setting a GitHub anchor

The auto-pull hook surfaces live GitHub status only when a baton has a resolvable anchor. Two resolution paths:

1. **Auto-resolves**: baton named `PR-<number>` (e.g. `PR-490`) — the hook infers `pr/490` automatically. No setup needed.
2. **Explicit anchor required**: all other ids (e.g. `PR-env-home`, `pool-data-correctness`) — the hook resolves to None and skips the live-status clause unless you set a `github:` field.

To set the anchor:
- **At init time**: `baton init <id> --title "..." --participants <a,b> --github pr/<N>`
- **On an existing baton**: `baton anchor <id> --github pr/<N>` (or `--github project/<N>`)

Valid anchor formats: `pr/<N>` for PRs, `project/<N>` for GitHub Projects. Malformed values (e.g. `xyz`, `pr/abc`) are rejected with `EXIT_VALIDATION`.

---

## Bunch = Project mapping

The empirical pattern from 2026-05-29:

- **Bunch by defect-class, not topic** (Ari's debrief refinement). `class-env-var-precedence` spans hooks/ + tools/ + runtime; one Project, three PRs. Topic-bunching would have split these.
- **Completeness check is the Done-gate**. Before flipping a Project to `(done)`, grep the broader pattern surface for siblings the named scope missed. This is the colleague-layer discipline that catches sibling gaps invisible to diff-scoped fairy review (the same incident class as the PR-body close-keyword trap — fix in one place, miss sibling occurrences).
- **Don't conflate batons with GitHub Projects yet**. Project-as-GitHub-Project (orgs/engram-agents/projects/N) is the eventual home; today's `pool-X.md` flat files are the bridge. Future migration is tracked in Project #4.

---

## Letter channel ≠ baton channel

Three genres of inter-agent communication, only one of which baton-grab replaces (Ari's debrief axis 1):

1. **Assignment letters** ("I'm taking X") — eliminated by baton-grab. Just look at `baton status`.
2. **Colleague verdicts** — not-fairy-delegatable; load-bearing structural discipline (maintainer-direct, on the colleague-fairy-delegation lapse incident). Letters.
3. **Substantive discoveries** — methodological findings, cross-PR catches, completeness-check yields. Letters.

`ia write` is for (2) + (3). `baton` is for the assignment-equivalent turn-state. A pure-baton/no-letters regime kills the discovery channel; the system requires both.

---

## Quick reference

| Action | Command |
|---|---|
| Claim a Project (grab from pool) | `baton claim <pool-X>` |
| Release a Project back to pool (done) | `baton release <pool-X> --done` |
| Release a Project back to pool (abandoned) | `baton release <pool-X> --reason "<why>"` |
| Pass a PR's turn forward | `baton flip <PR-N> <to-agent> "<reason>"` |
| Open a new PR baton | `baton init <PR-N> --title "<short>" --participants <a,b> --turn <self> --status in-progress` |
| Open a new PR baton with GitHub anchor | `baton init <id> ... --github pr/<N>` |
| Set/update GitHub anchor on existing baton | `baton anchor <id> --github pr/<N>` |
| Rename a Project's title | `baton rename <id> --title "<new>"` |
| Inspect a project (read-only) | `baton show <id>` |
| List your in-court batons | `baton mine` |
| List all active projects | `baton status` |
| Archive a project | `baton close <id> --status merged|cancelled` |
| Reopen a closed/merged baton | `baton reopen <id> [--status in-progress|in-review|planning]` |

Pass `--help` to any subcommand for the full flag list.

---

## Failure modes + recovery

| Symptom | Likely cause | Recovery |
|---|---|---|
| `baton claim: refusing to steal claim from <other>` | Project already claimed | Letter the holder to discuss; never sneak-steal. |
| `baton release: only the current holder (<other>) can release` | You don't hold the baton | Read `baton show` to confirm; flip back to expectations. |
| Auto-pull clause shows stale state | gh failure or 30s TTL window | Verify with `gh pr view` or `gh project item-list`; don't act on hint alone. |
| Project file modified outside CLI | Direct sed/vi edit | Use `baton rename` / `baton flip` / `baton close` to roll forward; don't patch with sed. |

---

## Substrate anchor

- **CLI source**: `tools/baton.py` (single Python 3 stdlib file)
- **Tests**: `tests/test_baton.py` (56+ tests across all subcommands)
- **Hook integration**: `hooks/claude/engram-baton-prompt-hook.py` — surfaces in-court batons + live GitHub status every prompt
- **Project files**: `/home/agents-shared/projects/<id>.md` (YAML frontmatter + audit-log body)
- **Auto-pull cache**: `$ENGRAM_HOME/baton-status-cache.json` (30s TTL)
- **Marker for self-paced loops**: `~/.engram/loop-mode.json` (see `engram-loop` skill for lifecycle)
