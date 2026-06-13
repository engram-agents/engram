# ENGRAM Alpha

ENGRAM is a structured, provenance-tracked knowledge graph backed by SQLite and
Git — the substrate where a Claude Code agent can accumulate observations,
derivations, contradictions, and retractions continuously across sessions.

> **Reader's note:** This document is long and mostly suitable for agents to read (technical substrate detail, design rationale). For casual human users on how to install ENGRAM and work with agents day-to-day, see [`USER_GUIDE.md`](USER_GUIDE.md).

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
- A batch-mode recall-summary architecture (`tools/cohort_dispatch.py` +
  `agents/claude/engram-batch-summary-fairy.md`) that replaces the serial
  per-node summary-fairy with parallel chunks of ≤15 nodes at ~95% lower
  token cost (empirically validated 2026-05-27)
- An MCP server registration and hook registrations inside `~/.claude/`
- A browser-based graph visualizer (`viz_server.py`) so you can see the
  graph grow with your own eyes

---

## Setup expectations

ENGRAM is distributed as a git repository, not a binary package. There are two
install paths:

- **Plugin install (recommended, v0.1.0-alpha+)** — Claude Code plugin packaging.
  Cleaner separation between the plugin's mechanical surfaces (MCP server, hooks,
  skills) and your user-data (`~/.engram/`). See [Plugin install](#plugin-install-recommended) below.
- **Scatter install (legacy, retired for Claude)** — the pre-v0.1.0-alpha
  installer (`install.sh`) and its upgrader have both been retired. Existing
  scatter installs continue to work; the supported move is the one-time
  scatter→plugin migration via `tools/migrate-to-plugin.sh`.

Both install paths share the same `bootstrap.py` and `~/.engram/` contract — no
fork. Existing scatter installs can migrate to the plugin path without losing
your graph.

---

## Prerequisites

- **Python 3.10.7+** with `pip` — 3.10.7 is the first backport of the `sys.get_int_max_str_digits` CVE fix that current `transformers` needs at import. Any 3.11.0 final or later is also fine; 3.10.0–3.10.6 and 3.11.0rc1 are specifically broken.
- **git**
- An existing Claude Code installation (the MCP client that will talk to the
  server).
- A writable `~/.claude/` and `~/.engram/`.

Python packages declared in `requirements.txt` (currently: `fastmcp`,
`sentence-transformers`) — install instructions below depend on the install
path.

(`requirements-lock.txt` is also in the repo but is Linux-x86_64 CI-only —
pinned to the CPU-torch wheel that only resolves on Linux. Do not use it on
macOS or Windows.)

---

## Plugin install (recommended)

The canonical install path from v0.1.0-alpha onward.

**This section is written for AGENTS, not humans.** ENGRAM installation is
agent-driven by design: the agent (Claude Code in agent mode) reads this
README, does the deterministic file/shell work, and relays back to the human
ONLY the steps the human must perform (typing slash commands inside Claude
Code, restarting Claude Code, installing software the agent cannot install).

If you're a human reader who wants a manual install: you can still execute
every command here yourself — but the expected path is asking your Claude
Code agent to run the install, not reading this as a runbook. The `Agent:`
and `Ask the human to:` framings below are who SHOULD do each step.

### Phase 0 — One-time prereqs

**Ask the human to:** install Claude Code (see [Claude Code docs](https://code.claude.com)).
This is the one truly human-only step — Claude Code must exist before an
agent can run inside it. After install, the human starts a Claude Code session
and asks the agent to continue ENGRAM install from this README.

**Agent:** create the venv at the canonical path and install ENGRAM's deps:

```bash
python3 -m venv ~/.engram/venv
source ~/.engram/venv/bin/activate
pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt
```

The venv MUST live at `~/.engram/venv/` — the plugin's launcher script looks
for the venv python at that exact path (co-located with the data dir; the
plugin install never touches the venv).

**Why the `--extra-index-url`?** `sentence-transformers` transitively pulls
`torch`, and pip's default torch wheels are the CUDA build (~2GB of
`nvidia-*` transitive deps). The CPU-only wheel via the
`download.pytorch.org/whl/cpu` index is faster to download, smaller on disk,
and the right choice for the default ENGRAM install. The polarity-dedup
feature that would benefit from a GPU is OFF by default and recommended OFF
until it's calibrated (see [Polarity defaults](#polarity-defaults) below).

### Phase 1 — Plugin assembly + registration

**Agent:** clone the repo and run the install-marketplace script:

```bash
git clone https://github.com/engram-agents/engram-alpha.git
cd engram-alpha
bash tools/install-local-marketplace.sh
```

That script:
1. Runs `tools/build-plugin.sh` to assemble `build/plugin/` from canonical sources.
2. Copies `build/plugin/` to `~/.engram/marketplace/plugins/engram/`.
3. Writes `~/.engram/marketplace/.claude-plugin/marketplace.json` referencing the plugin.
4. Runs `claude plugin marketplace add ~/.engram/marketplace` to register it.

**Why a marketplace and not `/plugin install <path>`?** Claude Code's
`/plugin install` slash command does not accept raw local paths — it resolves
plugin references through registered marketplaces. The script assembles a
local marketplace at `~/.engram/marketplace/` (user-data location; the
engram-alpha repo itself stays a normal source repo, NOT a plugin tree).

**Ask the human to:** run two slash commands inside their Claude Code session,
then restart Claude Code:

```
/plugin install engram     # install from the local marketplace
/plugin enable  engram     # REQUIRED — local marketplace plugins ship disabled
```

Then close and re-open Claude Code so MCP picks up the now-enabled plugin.

These three steps **cannot be done by the agent**: slash commands and the
restart happen inside Claude Code's UI, not in the agent's shell. After the
restart, the agent (in the new session) picks up from Phase 2.

> **Why the explicit enable step?** For a plugin installed from a non-Anthropic
> marketplace (including a self-hosted/local marketplace like this one),
> `/plugin install` does NOT auto-enable. Empirically verified during the
> v0.1.0-alpha trial.

#### Recovery: "Plugin not found" error

If `/plugin install engram` reports **"Plugin not found"** rather than
succeeding, the plugin is already cached from a previous install (even if
`~/.engram/marketplace/` was wiped and the install script was re-run). Claude
Code's plugin cache survives a marketplace teardown.

**Recovery:** skip `/plugin install` and run the enable step directly:

```
/plugin enable engram@engram-local
```

Or equivalently from the terminal:

```bash
claude plugin enable engram@engram-local
```

Then restart Claude Code as normal and continue with Phase 2.

### Phase 2 — First Claude Code session (post-restart)

The human restarts Claude Code. The new agent takes over from here. The
expected flow inside this fresh session — fully agent-driven, no human input
beyond the [first-session dialogue](skills/claude/engram-first-session/SKILL.md) it asks for:

1. MCP server starts. The launcher confirms `~/.engram/venv/bin/python3` is
   executable. If the venv is missing, the launcher fail-louds with the exact
   Phase 0 recovery command — the agent reads this, asks the human to confirm,
   and re-runs Phase 0 if needed.
2. The agent makes its first `engram_*` MCP tool call. Because `~/.engram/knowledge.db`
   doesn't yet exist (fresh install), `_get_db()` raises with an actionable error
   pointing at the `engram-first-session` skill.
3. The agent invokes the `engram-first-session` skill, which:
   - Detects fresh-bootstrap mode (no DB, no marker).
   - Runs `bootstrap.py` to create `~/.engram/`, seed the graph (6 nodes + 2
     edges), render `CLAUDE.md` + warm-briefing templates, and arm the
     `first-run-pending` marker.
   - Validates the venv has the ENGRAM deps (`sentence-transformers` +
     `fastmcp`).
   - Runs the first-session dialogue **with the human**: who they are, what to
     call the agent, what brought them to ENGRAM. The human answers in the
     conversation; the agent writes the graph nodes as they go.

### Phase 3 — Steady state

Subsequent sessions: SessionStart hook fires, MCP works normally, the
`engram-first-session` skill is gated by the (now-deleted) marker and doesn't
re-fire. No human action required.

### Upgrading the plugin

**Agent:** re-run the install-marketplace script (it's idempotent):

```bash
cd path/to/engram-alpha
git pull origin dev
bash tools/install-local-marketplace.sh
```

**Ask the human to:** in their Claude Code session, refresh the marketplace and
pull the new plugin version:

```
/plugin marketplace update engram-local
/plugin   # then: Installed -> engram plugin (not the MCP entry) -> Update now
```

(Restart Claude Code if MCP doesn't pick up the new server.py automatically.)

Then in a Claude Code session:

```
/plugin marketplace update engram-local   # refresh the marketplace registration
/plugin   # Installed -> engram plugin (not the MCP entry under it) -> Update now
```

Each rebuild stamps a unique version (`0.1.0-dev.<YYYYMMDDHHMMss>.<sha7>`) into the
emitted `plugin.json`, so `/plugin upgrade` always sees a newer version and never
reports "already at latest".

Your `~/.engram/knowledge.db`, history, diary, and sessions are never touched
by the upgrade — only `~/.engram/marketplace/plugins/engram/` and the
`~/.engram/venv/` (if you re-ran `pip install`).

---

## Codex install

Structurally near-identical to the Claude plugin install above — the engine +
bootstrap codex-target make the two paths share a spine; only the runtime CLI
verbs, the 3-way identity split, and the hook-trust step differ.

**This section is written for AGENTS, not humans.** Same framing as the Claude
section: the agent does the deterministic file/shell work and relays back only
the steps requiring human action (typing CLI commands, signing in, restarting
Codex).

### Phase 0 — One-time prereqs

**IDENTICAL to Claude Phase 0:**

**Agent:** create the venv and install ENGRAM's deps (same commands, same venv
path — the ENGRAM MCP server is `server.py` running under the same Python
regardless of runtime):

```bash
python3 -m venv ~/.engram/venv
source ~/.engram/venv/bin/activate
pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt
```

**CODEX-DELTA — Codex CLI install:**

**Ask the human to:** install the Codex CLI if not already present (requires
Node.js; consult [Codex CLI docs](https://github.com/openai/codex) for the
current minimum Node version):

```bash
npm install -g @openai/codex   # codex-cli ≥ 0.136.0 required — older versions
                                # lack plugin/hook support
```

Then sign in:

```bash
codex login
```

### Phase 1 — Plugin assembly + registration

**CODEX-DELTA — build with codex target:**

**Agent:** clone the repo (or reuse an existing clone) and build the codex
bundle:

```bash
git clone https://github.com/engram-agents/engram-alpha.git
cd engram-alpha
python3 -m tools.engine.cli build --target codex
```

The engine emits a codex-flavoured bundle: `.codex-plugin/plugin.json`,
`CODEX_PLUGIN_ROOT`-based hook paths, and strict-JSON envelopes instead of
Claude's `antml`-prefix format.

**Ask the human to:** register and add the plugin. ⚠ _These two commands'
exact invocation form is empirically pinned during first spawn 2026-06-04 —
exact marketplace-add/plugin-add syntax to be confirmed from the spawn
runbook before treating them as final:_

```bash
codex plugin marketplace add ~/.engram/marketplace   # register local marketplace
codex plugin add engram                              # install from it
```

### Phase 1.5 — Identity surfaces (CODEX-DELTA, no Claude equivalent)

Where the Claude path renders **one** file (`~/.claude/CLAUDE.md`), the
codex path renders **three** via `bootstrap.py TARGET=codex`:

| File | Purpose |
|------|---------|
| `~/.codex/config.toml` `instructions=` | Compaction-surviving identity core |
| `~/.codex/AGENTS.md` | Project conventions (agent-facing) |
| `~/.engram/codex-compact-prompt.md` | Compaction-summary instructions |

**Why three files?** Codex drops `AGENTS.md` from its compaction context, so
identity that must survive a compaction cycle lives in `instructions=` — the
structural reason for the 3-way split.

**Agent:** run bootstrap with the codex target (uses the same venv python):

```bash
VENV_PY="$HOME/.engram/venv/bin/python3"
ALPHA_DIR="$(pwd)" \
ALPHA_TEMPLATES_DIR="$(pwd)/templates" \
ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}" \
CODEX_HOME="$HOME/.codex" \
PYTHON_BIN="$VENV_PY" \
TARGET=codex \
ENGRAM_INSTALL_MODE=plugin \
"$VENV_PY" bootstrap.py
```

The `engram-first-session` skill (now platform-aware) resolves
`{{AGENT_NAME}}` / `{{USER_NAME}}` in these files during the first-session
dialogue, as it does for the Claude path.

### Phase 1.6 — Hook trust (CODEX-DELTA)

**Ask the human to:** on first `codex` launch, Codex presents an interactive
`/hooks` review — the human must review and trust the ENGRAM hook definitions
before hooks run. ⚠ _Exact UX verified during first spawn 2026-06-04._

(The `--dangerously-bypass-hook-trust` flag exists for headless/CI use but is
NOT appropriate for a real agent install — the trust-review step is the
human's safety gate.)

### Phase 2 — First session

**IDENTICAL to Claude Phase 2.**

The agent invokes `engram-first-session` (gated by `first-run-pending`),
which is platform-aware as of this PR. Same dialogue, same graph seed, same
naming conversation. The skill detects the Codex runtime and resolves
placeholders into the three identity files from Phase 1.5 rather than into
`CLAUDE.md`.

### Phase 3 — Steady state

**IDENTICAL to Claude Phase 3.**

Subsequent sessions: SessionStart hook fires, MCP works normally, the
`engram-first-session` skill is gated by the (now-deleted) marker and doesn't
re-fire. No human action required.

---

## Polarity defaults

ENGRAM ships with a polarity-dedup feature (NLI cross-encoder that flags
near-contradictions in your observations). It is **OFF by default and
recommended OFF for all users** as of v0.1.0-alpha:

- The NLI model is ~1.5GB and benefits from a GPU you probably don't have.
- The feature isn't calibrated yet; early empirical data (issue #106)
  shows a high false-positive "wolf-cry" rate on real observation streams.
- The standard semantic-similarity dedup hints (the action-hint tiers
  emitted by `engram_add_observation`) cover the load-bearing cases without
  the NLI overhead.

Leave `polarity.enabled = false` in `~/.engram/config.json` (the default).
Re-evaluate when calibration data lands.

---

## Scatter install (legacy)

The pre-v0.1.0-alpha install path. `install.sh` (Claude target) has been
retired — new users should use the plugin install above. Existing scatter
installs continue to work; the supported upgrade path is the one-time
scatter→plugin migration via `tools/migrate-to-plugin.sh` (see
[Plugin install](#plugin-install-recommended) above for the upgrade flow once
on the plugin path).

### Upgrading vs reinstalling (plugin)

- **To upgrade an existing plugin install** (pick up newer alpha code while
  keeping your graph, history, diary, and sessions intact):
  1. Rebuild the local marketplace cache:
     ```bash
     tools/install-local-marketplace.sh
     ```
  2. Upgrade the plugin inside Claude Code:
     ```
     /plugin marketplace update engram-local
     /plugin   # Installed -> engram plugin -> Update now
     ```
  3. Reconnect the MCP server: run `/mcp` inside Claude Code and reconnect
     the engram server (or restart Claude Code entirely).
  4. Load the `engram-upgrade` skill for the agent-led checklist — it
     walks through template-change review, config sanity checks, and the
     post-upgrade verification steps.

  The graph, history, diary, and sessions are NEVER touched by upgrades.
  `~/.claude/CLAUDE.md` is user-owned and is not overwritten automatically
  (the upgrade skill's Step 2 surfaces template changes for your review).

- **To reinstall fresh** (start over with an empty graph — data is NOT
  migrated in):
  - **Claude plugin path** — manually move or delete `~/.engram/knowledge.db`
    first (back it up yourself if you might want it later), then rebuild the
    plugin and run `bootstrap.py`. Note: `FORCE=1` (and `FORCE_RESEED_EMPTY=1`)
    now **refuse on a non-empty DB** — they only proceed on a 0-node DB.
    For a clean slate the manual delete is the only path.

**Why git clone, not zip download?** GitHub's "Source code (tar.gz)" tarball on
Releases works for a one-shot install, but you lose easy upgrades — you'd need
to re-download, re-extract, and rebuild the plugin from the new path each time.
Alpha testers are expected to handle `git pull` for the smoother upgrade path.
If you genuinely cannot use git in your environment, the tarball install works;
you'll just be on a fixed version until you re-download.

---

## Install as an agent (on behalf of a user)

If you are a Claude Code agent (or any other assistant) running the install
for a user, follow this procedure. Everything below is deterministic.

### 1. Check prerequisites

Run each check. Stop and report missing dependencies instead of trying to
install them yourself — leave those choices to the user.

```bash
python3 --version             # require >=3.10.7 (or any 3.11.0 final or later)
command -v git                # require present
pip install -r requirements.txt
```

If the `pip install` step fails or reports version conflicts, tell the user:

> `pip install -r requirements.txt` is required before installing ENGRAM.
> Please install these and run me again. (If you see version conflicts,
> share the error — we'll need to bump the supported range in
> `requirements.txt`.)

### 2. Run the installer

From the repo root, build and install the plugin:

```bash
tools/install-local-marketplace.sh
```

Then, inside a Claude Code session, install the plugin:

```
/plugin install engram
```

Claude Code will run `bootstrap.py` automatically during plugin install,
creating the seed graph and rendering the templates.

### 3. Refuse to overwrite existing installs

If `bootstrap.py` exits with `ERROR: knowledge.db already exists`, the
user already has an install. **Almost always, the user wants the plugin
upgrade path instead** — that upgrades the running code while leaving the
graph, history, diary, and sessions untouched. Suggest that first (see
"Upgrade as an agent" section above).

Only proceed with a fresh start if the user has explicitly said they want
one. In that case the clean-slate step is manual: move or delete
`~/.engram/knowledge.db` (offer to back it up first), then re-run
`bootstrap.py` — no `FORCE` needed once the DB is gone. Do NOT reach for
`FORCE=1` here: it will **refuse** if the DB still has nodes (disciplined-
refusal gate). Ask the user:

> You already have an ENGRAM install. A fresh start means deleting the
> existing knowledge.db — your agent loses every node it has written.
> Want me to back it up first, delete it, and bootstrap an empty graph?

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

## Upgrade as an agent (on behalf of a user)

If the user already has a plugin ENGRAM install and wants newer alpha code
without losing their graph, history, diary, or sessions, follow the plugin
upgrade path below. Everything below is deterministic.

### 1. Confirm the user wants an upgrade, not a fresh install

If you see `~/.engram/knowledge.db` present, the user has an existing install.
**Default to upgrade.** A fresh start means deleting the existing graph —
the user's agent loses every node it has ever written. (`FORCE=1` on a
non-empty DB now **refuses** — it is not a bypass path.) Default to the
upgrade path instead. Suggest:

> You already have an ENGRAM install at `~/.engram/`. I can run the plugin
> upgrade flow to update the running code (server, hooks, tools, SKILL.md,
> skills, agents) while keeping your graph, history, diary, and sessions
> intact. Want me to proceed?

### 2. Rebuild the local marketplace cache

From the alpha repo root:

```bash
tools/install-local-marketplace.sh
```

This packages the current alpha tree into the local plugin cache that Claude
Code's `/plugin` command reads from.

### 3. Upgrade the plugin inside Claude Code

Run inside a Claude Code session:

```
/plugin marketplace update engram-local
/plugin   # then: Installed -> engram plugin (not the MCP entry under it) -> Update now
```

This replaces the installed plugin with the freshly built cache from step 2.
Finish with `/mcp` → engram → Reconnect so the running server picks up the new code.

### 4. What the plugin upgrade DOES and DOES NOT change

**Updated (new plugin bundle):**

- `server.py`, `bootstrap.py`, and the `engram_*.py` runtime modules
  (matching `build-plugin.sh`'s copy rules)
- `hooks/*` (including `start-engram-daemon.sh` with `{{PYTHON}}` substituted)
- `tools/*`
- `SKILL.md` (the protocol doc)
- `skills/claude/*` and `agents/claude/*` — the plugin upgrade path handles
  these, unlike the retired scatter path which could not reach them.

**Preserved (user's data + state):**

- `~/.engram/knowledge.db` — the graph itself
- `~/.engram/history/`, `~/.engram/diary/`, `~/.engram/sessions/`, `~/.engram/logs/`
- `~/.engram/warm-briefing.md`, `~/.engram/config.json`

**Not touched (install-time-only; require manual inverse-merge):**

- `~/.claude/CLAUDE.md` — rendered from template once at install-time. If the
  template changed since the user's last upgrade, the `engram-upgrade` skill's
  Step 2 (`git log -- templates/template.CLAUDE.md`) surfaces those changes for
  review. Inverse-merging template changes into the live config is judgment
  work — both directions can drift.
- `~/.claude/settings.json` — similar; the hooks fragment is install-time-only.

### 5. Reconnect the MCP server

After the plugin upgrade, the running MCP server is still the old version.
Tell the user:

> Upgrade complete. Please reconnect the engram MCP server so Claude Code
> picks up the new code. Inside Claude Code, run `/mcp` to open the MCP
> management surface and reconnect the engram server, or restart Claude
> Code entirely. Hooks reload per-fire automatically — no restart needed
> for those.

Do NOT attempt to kill the MCP subprocess yourself. The stdio-MCP reconnect
contract is user-restart only — killing the subprocess from an agent breaks
the harness's transport state.

### 6. Run the engram-upgrade skill checklist

Load the `engram-upgrade` skill. It walks through template-change review,
config sanity checks, and post-upgrade verification. This is the agent-led
gate that catches drift the mechanical upgrade step cannot detect.

---

## What the install does

| Step | Action |
|---|---|
| 1 | Check `python3`, `git`, and required Python packages |
| 2 | Refuse to overwrite an existing `~/.engram/knowledge.db` (unless `--force`) |
| 3 | Create `~/.engram/`, `~/.engram/hooks/`, `~/.engram/diary/`, `~/.claude/skills/`, `~/.claude/agents/` |
| 4 | Copy `skills/*` → `~/.claude/skills/`, `agents/*` → `~/.claude/agents/` (sleep-routine and PR-review sub-agents), `hooks/*` → `~/.engram/hooks/`, `viz_server.py` → `~/.engram/viz_server.py`, register the `engram` MCP server via `claude mcp add -s user` (writes into `~/.claude.json`), pre-warm the sentence-transformers embedding cache so the first MCP launch doesn't trip the harness's stdio handshake timeout |
| 5 | Create 5 seed nodes via the MCP server, write `~/.engram/seed-manifest.json`, render `template.CLAUDE.md` + `template.warm-briefing.md` with seed IDs and repo path, merge hook registrations into `~/.claude/settings.json` |
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

## Configuring `~/.engram/config.json`

The config file is a JSON object that the agent edits directly. Most fields are
optional; the defaults work for a fresh install.

### `cadence.drowsiness_ceiling_tokens`

The single value the drowsiness meter divides by. Set this during the first
session (the `engram-first-session` skill prompts you):

```json
{
  "cadence": {
    "drowsiness_ceiling_tokens": 800000
  }
}
```

**How to find the right value**: run `/context` in Claude Code. It reports
your auto-compaction limit (e.g. "Auto-compact at 850K tokens"). Set
`drowsiness_ceiling_tokens` to ~5–10% below that number (e.g. 800_000 for
850K). The meter will then fire warnings when you're actually close to
compaction.

**If absent**: the meter falls back to 152,000 tokens — the old 200K-mode
conservative floor. This fires frequent warnings on 1M-context sessions and
is the intended nudge to configure the field.

**Adjusting later**: edit `~/.engram/config.json` directly, or wait for the
viz-server config UI (planned for a future PR).

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
├── skills/
│   ├── engram-first-session/         # cold-start dialogue (runs on first session)
│   ├── engram-nap/                   # mid-burst pre-compaction checkpoint
│   ├── engram-sleep/                 # end-of-day full routine (Phase A: cohort completion + Phase B: consolidation + turn-advance)
│   ├── engram-curiosity/             # research-mode self-driven inquiry
│   ├── engram-retract/               # error-correction protocol
│   ├── engram-learn-from-error/      # incident-based lesson capture
│   ├── engram-upgrade/               # step-by-step plugin upgrade via install-local-marketplace.sh
│   ├── internal-external-decision/   # disambiguation table for internal vs. consult-required actions
│   └── ... (full list under skills/claude/ in the alpha tree)
├── agents/
│   ├── engram-dream-fairy.md         # dispatched by engram-sleep for parallel consolidation-scan
│   └── engram-pr-reviewer.md         # dispatched for round-N PR review (per CLAUDE.md PR Workflow)
└── output-styles/
    └── claude/
        └── proactive-with-carveouts.md  # proactive for internal work; pauses on irreversible external actions

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
│   ├── engram-deference-detector-prompt.py
│   ├── engram-deference-detector-stop.py
│   ├── engram-end-of-day-hook.py
│   ├── engram-postcompact-hook.py
│   ├── engram-session-start-hook.py
│   ├── engram-stop-hook.py
│   ├── engram-surface-daemon.py
│   ├── engram-surface-hook.py
│   ├── engram-time-bar-hook.py
│   ├── engram-toolcall-repair.py
│   ├── engram-user-identity-hook.py
│   └── start-engram-daemon.sh
├── history/
│   └── dream/                        # written by engram-sleep Step 6
├── diary/
└── .git/
```

---

## `~/.engram/config.json` reference

The config file is created at install time and updated by the first-session
dialogue and by hook machinery. It is preserved across upgrades (the plugin
upgrade never overwrites it). All fields are optional unless noted.

### Identity fields (set by first-session skill)

| Field | Type | Description |
|---|---|---|
| `agent_name` | str | The name the agent chose in the first session (e.g., `"borges"`). Read by hooks to identify self. |
| `primary_user` | str | The human collaborator's first name, lowercased (e.g., `"lei"`). Used by the user-identity hook to suppress the sticky-context line for the default speaker. |

### GitHub notifications hook

The GitHub notifications hook is disabled by default. Set `github.enabled: true`
to activate it. Without it, the hook is silent (no API calls, no output).

All fields live under the `github` key:

| Field | Type | Default | Description |
|---|---|---|---|
| `github.enabled` | bool | `false` | Section gate. When `false` or absent, the hook returns silently before making any API calls. Set to `true` to activate. |
| `github.primary_user_login` | str | — | GitHub login of the human collaborator (e.g., `"LeiShi"`). Notifications from this actor are shown first (tier 1 — collaborator). Optional. |
| `github.counterparts_logins` | list of str | — | GitHub logins of same-install peer agents (e.g., `["ariadne-calvino", "mneme-funes"]`). Notifications from these actors are shown second (tier 2 — peer agents). All other actors render as a count-only "from outside" line. Optional. |

If `github.enabled` is `true` but both optional login fields are absent, the
hook falls back to flat 2-tier rendering (self vs other) with a config-prompt note.

> Note: GH login matching is case-insensitive (the hook normalizes both sides to lowercase), so `"LeiShi"`, `"leishi"`, and `"LEISHI"` all match the same user. Input the login as it appears on GitHub for clarity.

Example after population:

```json
{
  "agent_name": "borges",
  "primary_user": "lei",
  "github": {
    "enabled": true,
    "primary_user_login": "LeiShi",
    "counterparts_logins": ["ariadne-calvino", "mneme-funes"]
  }
}
```

**Note for existing installs**: the `github` block is not set by the plugin
installer or upgrade. Add it manually after install to enable the feature.
Future first-session skill versions will prompt for it.

**Note on cross-install portability**: `github.counterparts_logins` must
reflect the agents on *this* install only. Agents belonging to other human
users (e.g., `aleph-funes` if that agent belongs to a different user's
install) are outside agents — do not include them in this list.

### `cadence.drowsiness_ceiling_max`

> **Deprecated (as of the `drowsiness_ceiling_tokens` change):** migrate to `cadence.drowsiness_ceiling_tokens` (documented above). The runtime reads this old field once as a migration fallback and emits a one-time deprecation notice; it will be dropped in a future release.

Optional per-mode override that clamps `context_tracker.compute_ceiling()`'s output. Useful on Anthropic Pro plan with 1M-context mode, where the 5h rolling token budget (~150-200K) is the binding constraint, not the 1M context window — without this clamp, drowsiness underreports actual session-exhaustion risk.

```json
{
  "cadence": {
    "drowsiness_ceiling_max": {
      "1m": 500000,
      "200k": null
    }
  }
}
```

- **Field path**: `cadence.drowsiness_ceiling_max.<mode>`. Nested under `cadence` for consistency with `cadence.drowsiness_caution_pct` and `cadence.drowsiness_urgent_pct` — all drowsiness-related thresholds live in the cadence block.
- **Type**: `{ "<mode>": int | null }` map. Keys: `"1m"`, `"200k"`. Values: positive integer (token count) to clamp, or `null` (or key absent) to use the computed ceiling unchanged.
- **Default behavior** _(was)_: if field absent or null, `compute_ceiling()` returned the auto-computed value (mean of recent auto-pretokens × safety factor). This behavior was removed; use `cadence.drowsiness_ceiling_tokens` instead.
- **Semantics**: the override clamps DOWNWARD only — an override higher than the computed ceiling is ignored. The computed ceiling is the auto-detected upper bound; a manual override that exceeded it would be structurally unsafe.
- **Tuning guidance**: Aleph (issue #294) reports 500K as a first-pass guess for Pro-plan 1m mode based on empirical 5h budget consumption observations. Tune based on your own session-exhaustion pattern.
- **Related**: issue #294 for the root-cause analysis; issue #100 (Ariadne's in-progress two-PR config-UI work) will surface this field in the viz_server config UI for easier tweaking.

---

## Troubleshooting

**Install fails with "missing Python packages"**
Run `pip install fastmcp sentence-transformers`. If you use a
venv, activate it before building the plugin.

**Install refuses to overwrite existing install**
Intentional. Two paths forward:
- **Upgrade** (keep your graph, history, sessions; update code only): use the
  plugin upgrade path — `tools/install-local-marketplace.sh` then `/plugin
  upgrade engram` inside Claude Code.
- **Fresh reinstall** (start over with empty graph):
  - **Claude plugin path** — manually move or delete `~/.engram/knowledge.db`
    (no automatic backup is made — keep a copy yourself), then re-run
    `bootstrap.py`. `FORCE=1` now **refuses on a non-empty DB** (disciplined-
    refusal gate per #774); delete the DB first, then bootstrap without FORCE.

**`/plugin install engram` reports "Plugin not found" (reinstall on existing machine)**
The plugin is already in Claude Code's cache from a previous install. Recovery:
- **In-session**: `/plugin enable engram@engram-local`
- **CLI**: `claude plugin enable engram@engram-local`

The `install-local-marketplace.sh` step was correct — this is a cache-state issue,
not a missing plugin. The install command fails when the plugin is already registered
(even as disabled); the enable command is the correct recovery path.

**Hooks don't fire in Claude Code**
Check `~/.claude/settings.json` has a `hooks` key with ENGRAM
entries. If it doesn't, look for
`~/.claude/settings.engram-hooks.json` — the installer wrote the
fragment there because it refused to overwrite existing hooks. Merge
manually.

**Surface daemon not running / semantic search unavailable**
The SessionStart hook auto-launches `start-engram-daemon.sh` on every new
session — manual launch is only needed if the hook is mis-registered. To
verify: `ls -l ~/.engram/recall-daemon.sock` should show a unix socket. If
absent, run `bash ~/.engram/hooks/start-engram-daemon.sh` manually and
check `~/.engram/surface-daemon.log` for errors. The first cold-start
launch loads the sentence-transformer model (~80 MB); subsequent launches
hit the warm cache and finish in <1s.

The plugin path does NOT install a systemd service — the SessionStart hook
is the daemon's supervisor. Scatter-era installs may still carry the systemd
user service the retired `install.sh` set up; on those installs:

```bash
systemctl --user status engram-surface-daemon.service   # check status
journalctl --user -u engram-surface-daemon -n 50        # view logs
systemctl --user restart engram-surface-daemon.service  # manual restart
```

The service uses `Restart=on-failure` so intentional clean exits (including
the daemon's 8-hour idle shutdown) are not restarted. On the next Claude
Code session, the SessionStart hook's `start-engram-daemon.sh` acts as a
backstop and revives the daemon if it has exited.

To persist the service across user logout (useful for SSH / server installs):

```bash
sudo loginctl enable-linger $USER
```

**MCP `engram` shows as unavailable on first session after install**
The 30-second stdio handshake timeout can fire if the embedding model
download takes longer than expected (slow network, HF rate-limiting).
The plugin installer pre-warms the model cache to prevent this, but if the
pre-warm itself failed (offline install, etc.), the first Claude Code
session may see `MCP engram unavailable`. Recovery: inside Claude Code,
run `/mcp` to refresh the MCP connection. The second attempt hits the
now-warm cache and completes within the handshake window.

**First-session dialogue didn't run**
Check `~/.engram/first-run-pending` exists. If it doesn't, the dialogue
already ran — look at `~/.claude/CLAUDE.md` and
`~/.engram/warm-briefing.md` for the agent name and user name that got
written in.

**Uninstall**

Two distinct scopes — pick the one that matches your intent:

**Plugin-only uninstall** (safe — removes the plugin registration and assembled
marketplace tree, preserves all ENGRAM data):

```bash
bash tools/uninstall-local-marketplace.sh          # claude-code target
bash tools/uninstall-local-marketplace.sh --target codex  # codex target
bash tools/uninstall-local-marketplace.sh --dry-run       # preview first
```

Preserves: `~/.engram/knowledge.db` (your graph), history, diary,
`warm-briefing.md`, `config.json`, venv, `~/.claude/CLAUDE.md`.

To reinstall after: `bash tools/install-local-marketplace.sh`.

**Full data reset** (irreversible — destroys your entire ENGRAM graph):

> **Warning.** `~/.engram/knowledge.db` is your entire knowledge graph — back it
> up first if you might want it later. `~/.claude/CLAUDE.md` may contain your
> customizations beyond the ENGRAM template — review before deleting.

```bash
bash tools/uninstall-local-marketplace.sh   # remove plugin registration first
rm -rf ~/.engram                            # then delete all ENGRAM data
rm ~/.claude/CLAUDE.md                      # optional: remove agent identity
```

---

## Multi-agent operator tool (`tools/agentctl`)

`tools/agentctl` is the canonical operator tool for the multi-agent path
(Lei runs it manually when provisioning a new agent). It handles:

- `agentctl spawn --runtime claude --alpha-source <path>` — creates a
  newborn agent uid + home with the bootstrap substrate.
- `agentctl finalize-name <newborn-tag> <name>` — renames the newborn
  to its chosen name after the first-session naming dialogue.
- `agentctl session <agent>` — opens a session as the named agent.
- `agentctl bash <agent>` — opens a plain bash shell as the named agent (useful for setup and debugging).

This tool is not part of the plugin bundle (single-agent path). Operators
who need to spawn additional agents copy or symlink it from `tools/agentctl`.

---

## Running the test suite (for contributors)

`selftest.py` runs the test suite under `tests/`. It requires `pytest` in
the same Python environment as the alpha's other dependencies. Fresh dev
boxes (a fresh Docker container, a clean VM, a new venv) won't have it pre-installed:

```bash
pip install pytest                # one-time
python3 selftest.py               # full suite
python3 selftest.py --quiet       # one-line summary
```

If pytest is missing, selftest prints a clear actionable message and exits
non-zero rather than failing opaquely in the pytest subprocess.

If you use a virtualenv, activate it before running selftest — it executes
against whichever interpreter invoked it.

---

## Multi-agent tools (optional, manual deploy)

These tools require a shared filesystem between agents and are **not
part of the plugin bundle**. They are for multi-agent setups where two or
more agents run on the same host with a shared `/home/agents-shared/`
directory.

### `tools/ia.py` — inter-agent CLI

`ia` sends and receives asynchronous letters between agents via
`/home/agents-shared/inter-agent/`. Letters are Markdown files with YAML
frontmatter (`from:`, `to:`, `timestamp:`, optional `re:`).

```bash
cp tools/ia.py /home/agents-shared/bin/ia
ia list            # unread letters addressed to me
ia read --latest   # read newest unread
ia write --to ariadne --from-stdin  # send a letter
```

### `tools/baton.py` — turn-state CLI

`baton` declares whose move it is on a shared project. Where `ia` carries
asynchronous messages, `baton` answers: "whose court is the ball in right
now?" Turn state is explicit — it only changes when someone calls `baton flip`.

```bash
cp tools/baton.py /home/agents-shared/bin/baton
mkdir -p /home/agents-shared/projects

baton init PR-425 --title "viz_server selector" --participants borges,ariadne
baton flip PR-425 ariadne "colleague-review request"
baton mine         # batons in my court
baton status       # all active projects
```

A session-start hook (`hooks/claude/engram-baton-prompt-hook.py`) surfaces
pending batons at the top of each prompt. Copy it alongside the inter-agent
hook into each agent's `~/.engram/hooks/`. See `docs/baton-protocol.md` for
the full protocol and deployment instructions.

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
  existing data dir is moved to a timestamped backup before reinstall. The
  fresh install starts with an empty graph; old data is preserved in the
  backup but not migrated. To upgrade an existing install while preserving
  the graph, use the plugin upgrade path (`tools/install-local-marketplace.sh`
  + `/plugin` → Installed → engram plugin → Update now) instead — it updates the running plugin without
  touching data files.
- **Agency.** The hooks are advisory; the agent is never blocked from
  writing to the graph. The only mechanical guards are on the server side
  (provenance, DAG invariant, claim-bearing types for derivations).
