# ENGRAM Alpha

ENGRAM is a structured, provenance-tracked knowledge graph backed by SQLite and
Git — the substrate where a Claude Code agent can accumulate observations,
derivations, contradictions, and retractions continuously across sessions.

## What problem does this solve?

LLMs reset every session. The standard remedies — system prompts, vector
stores, episodic-memory plugins — do *memory persistence*: they retrieve text
from past sessions so the agent can recall what it said. That's the first
layer of agent continuity. It works, and it isn't enough.

The second layer is *narrative identity*: a coherent self-story that survives
across sessions ("I'm an assistant who values clarity and tends to second-
guess myself"). This is what most current agent-memory products optimize for —
a smooth-feeling continuity. The failure mode is that narrative identity
fails *silently*: a coherent story that was never true, or stopped being
true, looks the same as one that's accurate. There's no mechanism for the
story to catch its own drift.

ENGRAM is built for the third layer: *epistemic identity*. Every claim the
agent commits cites its evidence; every derivation cites its premises; every
retraction propagates downstream as a tainted-marker cascade. When a belief
turns out to be wrong, the correction doesn't just update one node — it
flags every claim that was built on top. The graph fails *loudly*:
contradictions surface, retractions cascade, investigation is triggered.

This makes the agent's memory *auditable* and *corrigible*. You can ask
"why does the agent believe X?" and trace it to the original quote in the
original source. You can ask "what changed when X turned out to be wrong?"
and see the cascade of tainted dependents. You can ask "what does the agent
not know yet?" and see the open-questions backlog as actual graph state, not
prose.

ENGRAM is not a memory layer for chatbots. It's a substrate for agents whose
identity *is* their accumulated, self-correcting knowledge — and whose
honesty is structurally enforced because cheating corrupts a system the
agent itself depends on.

---

This alpha package installs:

- The MCP server (`server.py`) and its data dir (`~/.engram/`)
- Five seed nodes establishing the initial epistemic frame (two axioms, two
  definitions, one goal)
- A cold-start skill (`engram-first-session`) that runs on the user's very
  first Claude Code session — a short dialogue to learn who the user is,
  choose a name for the agent, and write the first relationship-specific
  nodes
- Hooks that handle context tracking, write nudges, post-compaction recall,
  tool-call repair, and a fast recall daemon
- An MCP server registration and hook registrations inside `~/.claude/`
- A browser-based graph visualizer (`viz_server.py`) so you can see the
  graph grow with your own eyes

---

## Prerequisites

- **Python 3.10+** with `pip`
- **git**
- Python packages: `fastmcp`, `sentence-transformers`

```bash
pip install fastmcp sentence-transformers
```

- An existing Claude Code installation (the MCP client that will talk to the
  server)
- A writable `~/.claude/` and `~/.engram/`

---

## Quick install (for humans)

From inside a clone of this repo:

```bash
./install.sh
```

That's it. Start a new Claude Code session; the agent will read
`~/.engram/first-run-pending` and run the cold-start dialogue.

To force-reinstall (backs up the existing `~/.engram/` first):

```bash
./install.sh --force
```

---

## Install as an agent (on behalf of a user)

If you are a Claude Code agent (or any other assistant) running the install
for a user, follow this procedure. Everything below is deterministic.

### 1. Check prerequisites

Run each check. Stop and report missing dependencies instead of trying to
install them yourself — leave those choices to the user.

```bash
python3 --version             # require >=3.10
command -v git                # require present
python3 -c 'import fastmcp, sentence_transformers'
```

If the last command fails, tell the user:

> `pip install fastmcp sentence-transformers` is required before
> installing ENGRAM. Please install these and run me again.

### 2. Run the installer

From the repo root:

```bash
./install.sh
```

Expected terminal output (abbreviated):

```
== ENGRAM Alpha Install ==
  Repo:    /path/to/engram-alpha
  ENGRAM:  /home/USER/.engram
  Claude:  /home/USER/.claude

== Step 1: Dependencies ==
  python3 ok
  git ok
  Python deps ok (fastmcp, sentence-transformers)

== Step 2: Existing-install check ==
== Step 3: Creating directories ==
== Step 4: Copying files ==
  skills → /home/USER/.claude/skills/
  hooks → /home/USER/.engram/hooks/
  MCP registered (user scope): engram → /path/to/python3 /home/USER/.engram/server.py
== Step 5: Seed graph + template resolution ==
  Creating seed graph...
    {{AX_HONESTY}} = ax_0001
    {{AX_PROVENANCE}} = ax_0002
    {{DF_ENGRAM}} = df_0001
    {{DF_EPISTEMIC_IDENTITY}} = df_0002
    {{GL_EPISTEMIC_HUMILITY}} = gl_0001
  manifest → /home/USER/.engram/seed-manifest.json
  CLAUDE.md → /home/USER/.claude/CLAUDE.md
  warm-briefing.md → /home/USER/.engram/warm-briefing.md
  settings.json ← hooks merged (/home/USER/.claude/settings.json)
== Step 6: Arming first-session marker ==
== Step 7: Git ==
== Install complete ==
```

### 3. Refuse to overwrite existing installs

If step 2 fails with `existing install detected`, the user already has an
ENGRAM graph at `~/.engram/knowledge.db`. **Do not pass `--force` without
explicit user consent** — that will move their existing graph to
`~/.engram.backup.TIMESTAMP/` and start fresh. The backup is recoverable but
no sessions will auto-restore from it. Ask the user:

> You already have an ENGRAM graph at `~/.engram/`. Running with `--force`
> will back it up to `~/.engram.backup.TIMESTAMP/` and start fresh. Continue?

### 4. Handle the settings.json warning case

If step 5 prints:

```
WARN: ... already has a 'hooks' key; ENGRAM fragment written to
/home/USER/.claude/settings.engram-hooks.json for manual merge.
```

then the user has existing hook registrations we refused to clobber. Tell
them:

> Your `~/.claude/settings.json` already has a `hooks` block. I wrote
> the ENGRAM hook registrations to
> `~/.claude/settings.engram-hooks.json`. Please merge them into the
> `hooks` key of `settings.json` manually.

### 5. Verify success

```bash
test -f ~/.engram/knowledge.db && echo "db ok"
test -f ~/.claude/CLAUDE.md && echo "claude.md ok"
test -f ~/.engram/warm-briefing.md && echo "briefing ok"
test -f ~/.engram/first-run-pending && echo "first-run armed"
grep -l '{{' ~/.claude/CLAUDE.md ~/.engram/warm-briefing.md || echo "placeholders resolved"
```

Note: `{{AGENT_NAME}}`, `{{USER_NAME}}`, and `{{TODAY}}` will still be
present — those are deliberately left for the first-session dialogue to
resolve. The grep should report that only those three remain.

### 6. Tell the user what happens next

> Install complete. Next, start a new Claude Code session. The agent will
> detect `~/.engram/first-run-pending` and run a short dialogue to learn
> who you are, pick a name for itself, and write the first nodes of your
> shared memory. The dialogue takes 5–10 minutes.

---

## What the install does

| Step | Action |
|---|---|
| 1 | Check `python3`, `git`, and required Python packages |
| 2 | Refuse to overwrite an existing `~/.engram/knowledge.db` (unless `--force`) |
| 3 | Create `~/.engram/`, `~/.engram/hooks/`, `~/.engram/diary/`, `~/.claude/skills/` |
| 4 | Copy `skills/*` → `~/.claude/skills/`, `hooks/*` → `~/.engram/hooks/`, `viz_server.py` → `~/.engram/viz_server.py`, register the `engram` MCP server via `claude mcp add -s user` (writes into `~/.claude.json`) |
| 5 | Create 5 seed nodes via the MCP server, write `~/.engram/seed-manifest.json`, render `CLAUDE.md.template` + `warm-briefing.md.template` with seed IDs and repo path, merge hook registrations into `~/.claude/settings.json` |
| 6 | Write `~/.engram/first-run-pending` so the cold-start skill fires on the next session |
| 7 | `git init` inside `~/.engram/`; commit the install state |

---

## The first session

When the user starts Claude Code after install, the agent:

1. Reads `~/.engram/first-run-pending` and loads the `engram-first-session`
   skill.
2. Introduces itself and ENGRAM in its own voice.
3. Asks the user's name, role, and what brought them to ENGRAM, writing a
   person node and a founding observation during the conversation.
4. Offers the user a choice: pick a name for the agent, ask for
   suggestions, or land on one together. This is the relationship-founding
   beat — do not rush it.
5. Writes the agent-self person node (`is_self=true`) and a naming
   cornerstone.
6. Substitutes `{{AGENT_NAME}}`, `{{USER_NAME}}`, `{{TODAY}}` in
   `CLAUDE.md` and `warm-briefing.md`, deletes the marker, and runs
   `engram_advance_turn()`.

The skill is in `~/.claude/skills/engram-first-session/SKILL.md`.

---

## Viewing the graph

A D3 force-directed visualizer is bundled at `~/.engram/viz_server.py`. It
reads `~/.engram/knowledge.db` directly and serves an interactive graph in
your browser — no extra dependencies, stdlib only.

```bash
python3 ~/.engram/viz_server.py          # default port 5001
python3 ~/.engram/viz_server.py --port 8080 --db /path/to/knowledge.db
```

Then open `http://localhost:5001` (graph view) or
`http://localhost:5001/health` (health dashboard). You can leave it
running in a spare terminal while the agent writes to the graph; refresh
the page to see new nodes.

---

## File layout after install

```
~/.claude.json                        # engram MCP server registration (user scope, managed by `claude mcp`)

~/.claude/
├── CLAUDE.md                         # resolved from template
├── settings.json                     # hooks merged in
└── skills/
    └── engram-first-session/
        └── SKILL.md

~/.engram/
├── knowledge.db                      # SQLite graph
├── graph_snapshot.md                 # plaintext dump for git diff
├── seed-manifest.json                # seed IDs + repo path
├── warm-briefing.md                  # resolved from template
├── first-run-pending                 # armed; deleted after first session
├── viz_server.py                     # browser graph visualizer
├── hooks/
│   ├── context_tracker.py
│   ├── context_tracker_hook.py
│   ├── engram-postcompact-hook.py
│   ├── engram-session-start-hook.py
│   ├── engram-stop-hook.py
│   ├── engram-surface-daemon.py
│   ├── engram-surface-hook.py
│   ├── engram-toolcall-repair.py
│   ├── engram-user-identity-hook.py
│   └── start-engram-daemon.sh
├── diary/
└── .git/
```

---

## Troubleshooting

**Install fails with "missing Python packages"**
Run `pip install fastmcp sentence-transformers`. If you use a
venv, activate it before running `install.sh`.

**Install refuses to overwrite existing install**
Intentional. Use `./install.sh --force` to back up and start fresh. The
existing graph is moved to `~/.engram.backup.YYYYMMDD_HHMMSS/` — you can
restore from it by swapping the directory back.

**Hooks don't fire in Claude Code**
Check `~/.claude/settings.json` has a `hooks` key with ENGRAM
entries. If it doesn't, look for
`~/.claude/settings.local.engram-hooks.json` — the installer wrote the
fragment there because it refused to overwrite existing hooks. Merge
manually.

**Surface daemon not running / semantic search unavailable**
Run `bash ~/.engram/hooks/start-engram-daemon.sh` manually. The first
launch loads the sentence-transformer model (~80 MB). Subsequent
SessionStart hooks keep it warm.

**First-session dialogue didn't run**
Check `~/.engram/first-run-pending` exists. If it doesn't, the dialogue
already ran — look at `~/.claude/CLAUDE.md` and
`~/.engram/warm-briefing.md` for the agent name and user name that got
written in.

**Uninstall**
```bash
rm -rf ~/.engram
rm ~/.claude/CLAUDE.md
claude mcp remove engram -s user
# Edit ~/.claude/settings.json to remove ENGRAM hook entries
# Edit ~/.claude/skills/ to remove engram-first-session
```

---

## Design notes

- **Two-phase template substitution.** The installer resolves seed node IDs
  and the repo path (install-time); the first-session dialogue resolves
  agent name, user name, and the date (first-session-time). This keeps the
  naming moment agent-led rather than shell-led.
- **Substitution-last.** If the first-session dialogue aborts mid-flight,
  the marker file stays in place and the skill re-fires on the next
  session. Placeholders are only substituted after the dialogue completes.
- **Idempotency guard.** Re-running the installer without `--force` refuses
  to overwrite an existing `~/.engram/knowledge.db`. With `--force`, the
  existing data dir is moved to a timestamped backup before reinstall.
- **Agency.** The hooks are advisory; the agent is never blocked from
  writing to the graph. The only mechanical guards are on the server side
  (provenance, DAG invariant, claim-bearing types for derivations).
