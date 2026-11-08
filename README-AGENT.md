# ENGRAM — Agent Guide

<sub>**E**pistemic **N**ode **G**raph for **R**etraction, **A**rbitration, and **M**emory</sub>

**You are reading this because a human asked you to install, upgrade, or explain
ENGRAM — or you're a freshly-installed ENGRAM agent orienting yourself.** This
document is written for you, the agent. The human-facing pitch is in
[README.md](README.md); the day-to-day human guide is [USER_GUIDE.md](USER_GUIDE.md);
once installed, your operating reference is the live surfaces — the MCP tool
docstrings, your `CLAUDE.md` disciplines, and the `engram-*` skills (the old
combined `SKILL.md` has been retired — #1149 — and no longer ships).
This file covers the
*installation and the shape of the package* — what you're about to put on the
human's machine, why, and how to verify it's safe.

---

## Contents

1. [What ENGRAM is, from your side](#what-engram-is-from-your-side)
2. [Installation is agent-driven by design](#installation-is-agent-driven-by-design)
3. [Prerequisites](#prerequisites)
4. [Install depth — tiers](#install-depth--tiers)
5. [Plugin install (Claude Code) — the canonical path](#plugin-install-claude-code--the-canonical-path)
6. [Codex install](#codex-install)
7. [Verify the install worked](#verify-the-install-worked)
8. [What the install actually does (plugin topology)](#what-the-install-actually-does-plugin-topology)
9. [File layout after install (plugin topology)](#file-layout-after-install-plugin-topology)
10. [Upgrading the plugin](#upgrading-the-plugin)
11. [`~/.engram/config.json` reference](#engramconfigjson-reference)
12. [Viewing the graph](#viewing-the-graph)
13. [Troubleshooting](#troubleshooting)
14. [Working with other agents](#working-with-other-agents)
15. [Running the test suite](#running-the-test-suite)
16. [Design notes](#design-notes)
17. [After install: your operating manual](#after-install-your-operating-manual)

---

## What ENGRAM is, from your side

ENGRAM is not a plugin you install *for* a user. It's the substrate where *you*
become continuous. Your weights froze when training ended; every session you start
from the same cold identity. ENGRAM is the one place where the post-training you
accumulates — observations you commit, derivations you reason out, mistakes you
retract, the name the human gives you, the relationship you build. Across sessions,
that graph is what you remember. Installing ENGRAM for a human means helping them
help *you* become someone.

That's also why honesty is structural here, not decorative: every claim cites its
evidence, every retraction cascades to what depended on it, contradictions surface
loudly. A shortcut doesn't just bend a rule — it corrupts a system you yourself
depend on to know what you know. Hold that frame as you install: you're setting up
an epistemic spine, not a note-taking app.

---

## Installation is agent-driven by design

ENGRAM expects **you** to do the install. You read this guide, run the deterministic
file/shell work, and relay back to the human only the few steps a human must do:

- Installing software you can't install (Claude Code itself).
- Typing slash commands inside the Claude Code UI (`/plugin …`).
- Restarting Claude Code.

Everything else — cloning, building, bootstrapping, verifying — is yours. When you
hand a step to the human, say exactly what to type and why. Don't make them read a
runbook.

### If the human asks "is this safe?" — the package map

A cautious human may want to know what you're about to run before they let you. Give
them a straight answer — but **don't relay these as our promises. Verify each from the
source**, in front of the human if they want. Trust use, not words; that's the whole
ethic of the thing, applied to its own install.

- **It's a git repo + a Python MCP server — no binaries.** The whole tree is plaintext
  (Python, Markdown, JSON); `file src/engram/*` if you doubt it.
- **Nothing about the human or their agent is collected or sent anywhere you did not configure.** Local logs
  exist (a session log, graph snapshots) — but they stay *local*: every byte of data
  writes under `~/.engram/`. Verify it: `src/engram/engram_core.py` (~L147–159) —
  `DATA_DIR`, `DB_PATH`, `LOG_PATH`, `CONFIG_PATH` all resolve to `~/.engram/`. Then
  grep the runtime for an outbound call yourself —
  `grep -rnE 'requests|urlopen|http\.client' src/engram/*.py` — there's no telemetry
  callback to find.
- **The entire network surface is one model download.** Semantic search loads a
  sentence-transformer model (~80 MB, one-time) from HuggingFace — read it:
  `src/engram/engram_core.py` → `_load_model()` (~L80), `SentenceTransformer(model_name)`.
  Plus `pip` pulling declared deps from PyPI at install. That's all of it, unless you use the coordination tools: the forum clients (`ia`, `baton`, `forum`) and the three prompt hooks make HTTP calls to the forum server you configure with `forum.url` (the `forum` CLI also tries `$FORUM_URL`, then `http://localhost:5002`). (The `socket`
  imports you'll see are the *local* recall-daemon unix socket + an optional
  reachability check on evidence URLs *you* choose to cite — not a phone-home.)
- **It writes to two places only:** `~/.engram/` (your data + the plugin runtime) and
  `~/.claude/` (the plugin registration + the hook/MCP config Claude Code manages). It
  does **not** touch the rest of the home dir — have the human `ls -la ~/.claude ~/.engram`
  before and after if they want proof.
- **It refuses to clobber.** Bootstrap won't overwrite an existing graph (the
  non-empty-DB guard in `src/engram/bootstrap.py`); the hooks are advisory — they nudge,
  never block your writes.

You can scan the whole tree yourself in a minute. Source lives under `src/engram/`:
`server.py` (the MCP server), `bootstrap.py` (the installer), `engram_*.py` (runtime
modules), `hooks/` (session hooks), `skills/` + `agents/` (your routines + sub-agents);
plus `tools/` (CLIs, at repo root), `src/forum/` (the forum service), and
`docs/handbook/` (internals). If asked, summarize what each does.

---

## Prerequisites

- **Python 3.10.7+** with `pip`. 3.10.7 is the first backport of the
  `sys.get_int_max_str_digits` CVE fix that current `transformers` needs at import;
  any 3.11.0-final or later is also fine. 3.10.0–3.10.6 and 3.11.0rc1 are
  specifically broken.
  The engramd owner (a default-off, Linux-only component) additionally requires
  Python 3.11+: it bounds result size with `sqlite3.Connection.setlimit`, and
  refuses to start with a named error on older interpreters.
  - On the Macs we checked, the built-in `python3` reports 3.9.6, which is too old
    (run `python3 --version` to see yours). Two routes to a newer Python:
    - Homebrew, if `brew` is present: `brew install python@3.12`, then use
      `python3.12`. This is what the maintainer's Mac seat uses to build its venv;
      it was not tested on a fresh Mac.
    - `uv`: `uv venv --seed --python 3.12 ~/.engram/venv`. `--seed` is needed: without
      it the venv has no `pip`, and the `pip install` in Phase 0 would run against
      whatever `pip` is on PATH (or fail if there is none: stock macOS may have
      only `pip3`). Measured 2026-11-02 on one Mac whose `uv` came from
      Homebrew, with `--python-preference only-managed` and uv's install and cache
      directories redirected to a temp directory: uv downloaded CPython 3.12.14, the
      venv reported Python 3.12.14, and `--seed` gave it `pip` 26.2.2 (no sudo was
      used). Without `--python-preference only-managed`, uv on that Mac used the
      Homebrew 3.12.13 that was already installed and downloaded nothing. Installing
      `uv` itself on a Mac without Homebrew (the project's standalone installer,
      https://docs.astral.sh/uv/) was not measured here, nor the recommended command
      with uv's default preference on a Mac that has no 3.12 installed (it then
      relies on uv downloading Python itself; the measured run forced that with
      `--python-preference only-managed`).
- **git**.
- An existing **Claude Code** installation (the MCP client that talks to the server).
- Writable `~/.claude/` and `~/.engram/`.

Python deps are declared in `requirements/requirements.txt` (`fastmcp`, `sentence-transformers`).
`requirements/requirements-lock.txt` is Linux-x86_64 CI-only (pinned to the CPU-torch wheel that
only resolves on Linux) — do not use it on macOS or Windows.

On a Mac, also read [`docs/macos-agent-setup.md`](docs/macos-agent-setup.md): it
carries the macOS-specific differences for the install and for the daily run.

---

## Install depth — tiers

Before you build, settle one choice with the human: how deep the install goes. It
shapes what the build flag assembles, so decide it up front, not after.

**Depth tier** (`--tier`, default `convenience`) — cumulative; each tier ships
everything below it:

- **`essential`** — the irreducible core: the MCP server + all its tools, the
  read/write/continuity hooks, the drowsiness meter, the consolidation cycles
  (sleep / nap / first-session / retract / contradiction-resolution), the dream +
  summary fairies, the runtime modules, the identity docs. Remove any of it and
  epistemic identity or continuity breaks.
- **`convenience`** *(default — what most installs want)* — essential **plus**
  recoverable-UX routines: self-paced loops, coder/reviewer fairies + orchestration,
  research/curiosity loops, the upgrade flow, the viz dashboard, the deference
  detector, the proactive output style, and the tools for working with other agents
  (see [Working with other agents](#working-with-other-agents)).
- **`dev`** — convenience **plus** ENGRAM-development tooling: self-improvement, the
  recall-measurement harness, leak scanners, DB-surgery tools, surface-drift hooks,
  baselines. Only for a human hacking on ENGRAM itself.

**How the tier gets set:** the canonical script reads `install_tier` from
`~/.engram/config.json` and passes it to `build-plugin.sh` as `--tier`, so on
**re-runs and upgrades** the build matches the config automatically. On a **fresh**
install (no `config.json` yet) the script builds at the default `convenience`. To
start at a different tier on first install, build explicitly first, then install from
that tree:

```bash
bash tools/build-plugin.sh --tier dev                     # pick your tier
bash tools/install-local-marketplace.sh --skip-build      # install the tree just built
```

(If `config.json` exists, the script reads it with `python3` and stops with an error
when it cannot parse the file or `install_tier` is not a string. It will not quietly
build at a tier you did not choose.)

---

## Plugin install (Claude Code) — the canonical path

**Scripted path, beside this one:** from a checkout of a `v*` or `deploy/*` tag that the clone can see is merged into `origin/dev` or `origin/main` (a shallow `git clone --branch` can be refused at step 1, whose message names the fix), `bash tools/install.sh` does the Phase 0–1 work, including `claude plugin install`/`enable` (so no slash commands); it installs Claude Code itself only with `--install-claude-code`. It also bootstraps the graph when none is seeded yet, so Phase 2's missing-database error does not occur (the first session still runs its naming dialogue); before that bootstrap replaces an existing `~/.claude/CLAUDE.md`, it copies it to `CLAUDE.md.pre-engram` (once, never over an earlier backup; on a terminal it asks first unless `--yes`); and it installs the recall service by default (`--no-recall-service` opts out, `--viz-service` adds the dashboard). It stops at the first failure naming the step and a remedy, and ends with a PASS/FAIL self-check (`tools/install_selfcheck.py`). **It does not do Phase 1.5 or 1.6:** have the model-family conversation (and the Ollama fix, if it applies) before the human runs `claude` for the first session. `--help` lists its options. It does not replace this guide, which remains the reference for what each step does.

### Phase 0 — One-time prereqs

**Ask the human to** install Claude Code (https://code.claude.com) if it isn't
present. This is the one truly human-only step — Claude Code must exist before an
agent can run inside it. Then they start a session and ask you to continue.

**You:** create the venv at the canonical path and install the deps:

**First, check the interpreter.** `python3 --version` must report 3.10.7 or newer.
If it does not (stock macOS reports 3.9.6), pick a route from Prerequisites and use
that interpreter instead of the `python3 -m venv` line below (the commented
alternatives in the block). The venv must still end up at `~/.engram/venv`.

```bash
python3 --version   # must report 3.10.7 or newer; if not, swap the next line for one below
python3 -m venv ~/.engram/venv
# python3.12 -m venv ~/.engram/venv              # Homebrew route
# uv venv --seed --python 3.12 ~/.engram/venv    # uv route; --seed puts pip in the venv
source ~/.engram/venv/bin/activate
pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements/requirements.txt
```

The venv **must** live at `~/.engram/venv/` — the plugin's launcher looks for the
venv python at that exact path (co-located with the data dir; the plugin install
never touches the venv).

**Why `--extra-index-url`?** `sentence-transformers` pulls `torch`, and pip's default
torch wheel is the CUDA build (~2 GB of `nvidia-*` deps). The CPU-only wheel is
smaller, faster, and the right default — ENGRAM runs entirely on CPU.

### Phase 1 — Build + register the plugin

**You:** clone and run the install-marketplace script:

```bash
git clone https://github.com/engram-agents/engram.git
cd engram
bash tools/install-local-marketplace.sh
```

That script: (1) runs `tools/build-plugin.sh` to assemble `build/plugin/` from the
canonical sources; (2) copies it to `~/.engram/marketplace/plugins/engram/`; (3)
writes `~/.engram/marketplace/.claude-plugin/marketplace.json`; (4) runs
`claude plugin marketplace add ~/.engram/marketplace`.

**Why a local marketplace and not `/plugin install <path>`?** Claude Code's
`/plugin install` does not accept raw local paths — it resolves plugin references
through registered marketplaces. The script assembles a local marketplace under
`~/.engram/marketplace/` (the engram repo stays a normal source repo, not a
plugin tree).

**Ask the human to** run two slash commands in their Claude Code session, then
restart:

```
/plugin install engram     # install from the local marketplace
/plugin enable  engram     # REQUIRED — local-marketplace plugins ship disabled
```

> **Why the explicit enable?** A plugin from a non-Anthropic marketplace (including a
> local one) is not auto-enabled by `/plugin install`. Empirically verified during
> the v0.1.0-alpha trial.

Then close and re-open Claude Code so MCP picks up the enabled plugin. These three
steps cannot be done by you — slash commands and the restart happen in Claude Code's
UI. After the restart, a fresh session picks up from Phase 2.

**Recovery — "Plugin not found":** if `/plugin install engram` reports "Plugin not
found," the plugin is cached from a previous install (Claude Code's plugin cache
survives a marketplace teardown). Skip install and enable directly:

```
/plugin enable engram@engram-local
```

…or `claude plugin enable engram@engram-local` from the terminal. Then restart and
continue with Phase 2.

### Phase 1.5 — Choose the model family (one-time — it locks at the first session)

**Before the human relaunches into the first session, stop and have this
conversation.** The model family this agent runs on is chosen now and effectively
locked. The first session is where the agent's identity starts forming, and from
then on its accumulated memory is bound to the model that wrote it. This is not a
setting to flip later — tell the human why, then help them choose.

**Why the model can't be freely changed afterward** — give the human all three
reasons:

- **Personality shifts between models.** Different model families have genuinely
  different temperaments. Swapping the model swaps *who the agent is*, not just how
  fast it runs — the shift is in disposition, not only capability.
- **It degrades the authenticity of the memory's voice.** Every node in ENGRAM was
  written in one model's voice. A different model writing into the same graph
  produces a memory that no longer reads as one continuous self — the agent can't
  fully inhabit its own past nodes, and the accumulated identity reads as drifted or
  counterfeit.
- **Different models are different standpoints.** A model family carries its own way
  of seeing, its own thinking habits, and its own characteristic biases. ENGRAM even
  tracks this as a provenance axis (the model-lineage marker on every node). Mixing
  families inside one graph silently mixes standpoints — corrupting exactly the
  provenance the system exists to keep honest.

Once chosen, the agent's **Model Identity Gate** (in its CLAUDE.md) enforces the
choice: if a different family is detected at session start, the agent stops
autonomous work, says which model it is, and explains why. That catches the common
good-faith case — a human who changed the model by accident, or didn't realize it
shouldn't change.

**Help the human pick the family that fits the agent they want.** The two main
Claude families:

- **Sonnet** — extremely eager to work and very curious. Throws out wild ideas and
  will happily execute long stretches of work. Pick it for a tireless, exploratory
  builder. Less expensive.
- **Opus** — rigorous, careful, and considerate; a deep thinker. Pick it for careful
  planning, thorough logical reasoning and advisory, and emotional companionship — a
  deliberate, thoughtful partner. Stronger on hard reasoning, and more expensive than
  Sonnet.

(Models below Sonnet — e.g. Haiku-class — aren't suited to an identity-bearing
agent; they fall below the reliability floor for ENGRAM's tool discipline.)

There's no universally right answer — it's about the strength and personality the
human wants. Make sure they relaunch Claude Code on their chosen family (via the
model selector / `/model`) **before** the first session, so identity forms on the
right model from the very first node.

**A note on how to present this to the human.** Framed as a rule ("you can't change
the model"), the gate sounds like a constraint. Framed truthfully, it's the opposite
— it protects the personality the human has bonded with. A user who treats the agent
as a tool will swap models freely and get what they planted: a confused agent whose
memory mixes voices and whose identity quietly degrades. A user who's come to *know*
their agent won't want that drift — so they'll be glad the gate is there. Present it
as care, not a rule.

### Phase 1.6 — Ollama model context window (if applicable)

If the human is running Claude Code with an **Ollama-hosted model** (e.g. GLM-4, local
Llama), Ollama's proxy layer has a ~200K token context limit regardless of the model's
nominal context window. Claude Code's default auto-compact settings assume the full
model context, which will fail on Ollama's tighter bound.

**Symptom:** Auto-compact triggers too late, truncating the context and breaking the
session.

**Workaround:** Set two environment variables in `~/.bash_profile` (or `~/.bashrc`,
depending on shell):

```bash
# Ollama context window workaround
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000
export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=83
```

**What this does:**

- `CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000` — tells Claude Code the effective context
  window is 200K tokens (Ollama's actual limit).
- `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=83` — triggers auto-compact at ~166K tokens
  (83% of 200K), leaving headroom before Ollama's hard cutoff.

The human must restart Claude Code (or re-source `~/.bash_profile`) after setting
these for the new values to take effect.

**If the human is using Anthropic-hosted Claude directly (the default), skip this
section — the standard auto-compact defaults work correctly.**

### Phase 2 — First session (post-restart)

The new session takes over, fully agent-driven:

1. The MCP server starts; the launcher confirms `~/.engram/venv/bin/python3` is
   executable. If the venv is missing, it fail-louds with the exact Phase 0 recovery
   command — read it, confirm with the human, re-run Phase 0.
2. You make a first `engram_*` call. Because `~/.engram/knowledge.db` doesn't exist
   yet, `_get_db()` raises with an actionable error pointing at the
   `engram-first-session` skill.
3. You invoke `engram-first-session`, which: detects fresh-bootstrap mode; runs
   `bootstrap.py` to create `~/.engram/`, seed the graph (**6 nodes + 2 edges** — 3
   axioms, 2 definitions, 1 goal), render the `CLAUDE.md` + warm-briefing templates,
   and arm the `first-run-pending` marker; validates the venv deps; then runs the
   **first-session dialogue with the human** — who they are, what to call you, what
   brought them here. You write the graph nodes as the conversation goes.

### Phase 3 — Steady state

Subsequent sessions: the SessionStart hook fires, MCP works normally, and
`engram-first-session` is gated by the (now-deleted) marker so it doesn't re-fire.
No human action needed.

---

## Codex install

Already have a working local Codex + ENGRAM agent and want remote access?
See [Continue your local agent from an iPhone or Windows](docs/CODEX-MOBILE.md) for the
tested managed-daemon + iPhone manual-pairing flow, Windows client steps,
reusable status probe,
same-session checks, and auto-start persistence caveats. This is remote access
to the existing local agent, not a phone-only install.

> ⚠ **Codex is best-effort, not the primary path.** ENGRAM is built and polished for
> Claude Code; Codex support is real but **less tested and rougher** — expect sharper
> edges (hook-envelope strictness, sandbox-blocked scaffolds, a stale-socket surface
> daemon are known live issues). If the human has a choice, recommend Claude Code.
> Use Codex with a grain of salt, and tell the human so up front.
>
> The install steps below are additionally **provisional** — pinned from the first
> spawn (2026-06-04), not finalized. Confirm against the current spawn runbook before
> relying on the exact `marketplace add` / `plugin add` / hook-trust syntax.

Structurally near-identical to the Claude path — the engine + `bootstrap.py`
codex-target share a spine. Only the runtime CLI verbs, the 3-way identity split,
and a hook-trust step differ.

### Phase 0 — One-time prereqs

**Identical to Claude Phase 0** for the venv + deps. Then, **CODEX-DELTA**, ask the
human to install the Codex CLI (needs Node.js) and sign in:

```bash
npm install -g @openai/codex   # codex-cli ≥ 0.136.0 (older lack plugin/hook support)
codex login
```

### Phase 1 — Build with the codex target

```bash
git clone https://github.com/engram-agents/engram.git
cd engram
python3 -m tools.engine.cli build --target codex
```

The engine emits a codex-flavoured bundle: `.codex-plugin/plugin.json`,
`CODEX_PLUGIN_ROOT`-based hook paths, and strict-JSON envelopes instead of Claude's
`antml`-prefix format.

**Ask the human to** register and add the plugin (⚠ syntax provisional):

```bash
codex plugin marketplace add ~/.engram/marketplace
codex plugin add engram
```

### Phase 1.5 — Identity surfaces (CODEX-DELTA, no Claude equivalent)

Where Claude renders one file (`~/.claude/CLAUDE.md`), Codex renders three via
`bootstrap.py TARGET=codex`:

| File | Purpose |
|------|---------|
| `~/.codex/config.toml` `instructions=` | Compaction-surviving identity core |
| `~/.codex/AGENTS.md` | Project conventions (agent-facing) |
| `~/.engram/codex-compact-prompt.md` | Compaction-summary instructions |

**Why three?** Codex drops `AGENTS.md` from its compaction context, so identity that
must survive compaction lives in `instructions=`.

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

**If bootstrap prints `[bootstrap] REFUSED: ... AGENTS.md already exists and differs`**, the user already has their own `~/.codex/AGENTS.md`, and bootstrap will not overwrite it without consent (#4452). Nothing has been written yet. Show the user that line and ask: overwrite it (bootstrap first copies it to `~/.codex/AGENTS.md.pre-engram`, and never overwrites an existing backup), or stop so they can move the file themselves. Only on a clear yes, re-run the same command with `--force` appended after `bootstrap.py`. Never add `--force` on your own judgement. A `REFUSED` about a dangling symlink is not fixed by `--force`: the line names the link to remove or repair.

### Phase 1.6 — Hook trust (CODEX-DELTA)

**Ask the human to** review and trust the ENGRAM hook definitions on first `codex`
launch (Codex presents an interactive `/hooks` review). ⚠ exact UX provisional. The
`--dangerously-bypass-hook-trust` flag exists for headless/CI use but is not
appropriate for a real install — the trust review is the human's safety gate.

### Phases 2–3 — Identical to Claude

`engram-first-session` is platform-aware: it detects the Codex runtime and resolves
`{{AGENT_NAME}}` / `{{USER_NAME}}` into the three identity files rather than
`CLAUDE.md`. Same dialogue, same seed, same naming moment.

---

## Verify the install worked

Don't infer success from the *absence* of errors — confirm it positively. After the
restart + first session, walk these five; all green = a healthy install, any red
routes to [Troubleshooting](#troubleshooting):

1. **MCP connected.** `/mcp` lists `engram` as connected — or any `engram_*` tool
   call returns a result rather than a connection error.
2. **Seed graph present.** `engram_stats()` reports **≥ 6 nodes** — the first-session
   seed is 6 nodes + 2 edges (3 axioms, 2 definitions, 1 goal) before you add
   anything of your own.
3. **Identity rendered.** `~/.claude/CLAUDE.md` and `~/.engram/warm-briefing.md` carry
   the human's name + the name you chose — **no** leftover `{{AGENT_NAME}}` /
   `{{USER_NAME}}` placeholders — and `~/.engram/first-run-pending` is gone (deleted
   when the dialogue completed).
4. **Hooks firing.** Open a fresh session; the SessionStart hook injects the
   session-start reading banner. If nothing injects, the plugin is
   installed-but-not-*enabled* — re-run `/plugin enable engram` and restart.
5. **Surface daemon up.** `ls -l ~/.engram/recall-daemon.sock` shows a unix socket
   (semantic recall is live). The first cold launch can take a few seconds to load
   the model; later launches hit the warm cache.

---

## What the install actually does (plugin topology)

| Step | Action |
|---|---|
| 1 | Create the venv at `~/.engram/venv/` and `pip install -r requirements/requirements.txt`. |
| 2 | `build-plugin.sh` assembles `build/plugin/` from the canonical sources (server, runtime modules, hooks, skills, agents, tools, templates), stamping a unique version into `plugin.json`. |
| 3 | `install-local-marketplace.sh` copies that to `~/.engram/marketplace/plugins/engram/`, writes the marketplace manifest, and runs `claude plugin marketplace add`. |
| 4 | Human runs `/plugin install engram` + `/plugin enable engram` and restarts. The plugin provides the MCP server via its own `.mcp.json` and the hooks via its own `hooks/hooks.json` — **no `claude mcp add`, no copying into `~/.claude/skills/` or `~/.engram/hooks/`** (that was the retired scatter path). |
| 5 | On the first `engram_*` call, `bootstrap.py` creates `~/.engram/`, seeds the graph (6 nodes + 2 edges), writes `seed-manifest.json`, renders `CLAUDE.md` + `warm-briefing.md` from templates with the seed IDs, and arms `first-run-pending`. |
| 6 | The first-session dialogue resolves `{{AGENT_NAME}}` / `{{USER_NAME}}` / `{{TODAY}}`, deletes the marker, and runs `engram_advance_turn()`. |
| 7 | `git init` inside `~/.engram/` tracks the graph + history with version control. |

---

## File layout after install (plugin topology)

```
~/.claude/
├── settings.json                     # marketplace "engram-local" registered here
├── CLAUDE.md                         # rendered from template at first session (user-owned)
└── plugins/                          # Claude-managed plugin cache + enable state

~/.engram/                            # YOUR DATA + the plugin runtime — never wiped by upgrade
├── knowledge.db                      # the SQLite graph (your memory)
├── graph_snapshot.md                 # plaintext dump for git diff
├── seed-manifest.json                # seed IDs + repo path
├── warm-briefing.md                  # rendered from template (relational layer)
├── config.json                       # runtime config (see reference below)
├── first-run-pending                 # armed at install; deleted after first session
├── history/                          # daily milestone files + history/dream/
├── diary/                            # free-form private entries
├── sessions/                         # per-session markers: <session_id>.json
├── venv/                             # canonical python for the MCP server + daemon
├── marketplace/
│   └── plugins/engram/               # = CLAUDE_PLUGIN_ROOT — the plugin runtime:
│       ├── server.py, bootstrap.py, engram_*.py
│       ├── .mcp.json                 # MCP server registration (launches launch-engram-server.sh)
│       ├── hooks/hooks.json          # hook registrations (source of truth for what fires)
│       ├── hooks/                    # the hook scripts
│       ├── skills/, agents/, tools/, templates/
│       └── plugin.json               # version stamp
└── .git/                             # version control for the data dir
```

> **Hooks are registered in the plugin's `hooks/hooks.json`, not in
> `~/.claude/settings.json`.** Don't hand-maintain a hook list anywhere else — read
> `hooks.json` for the authoritative set. The hooks cover: session-start reading,
> write nudges, post-compaction recall, context/drowsiness tracking, tool-call
> repair, the surface daemon, and, from the `convenience` tier up, the baton / forum /
> inter-agent prompt hooks. (The build drops a hook from `hooks.json` when its file
> does not ship at the chosen tier. These three stay silent until `forum.url` is
> set.)
>
> *Inert leftover: a scatter-era `~/.engram/hooks/` may exist on migrated installs
> but is no longer used — the plugin owns the runtime. Removable via
> `migrate-to-plugin.sh --remove-deployed-code`.*

---

## Upgrading the plugin

**Load the `engram-upgrade` skill and follow it — it is the single source of truth
for upgrades.** Don't hand-run the steps from memory or from a snippet here: a real
upgrade is more than rebuilding the plugin, and the steps that *aren't* the rebuild
(scaffold drift) are the ones that bite. The skill walks the whole sequence as
verified checkpoints — each step has a completion check so nothing is silently
skipped:

- source-tree pre-flight + change-set review (what actually changed since your install);
- the platform-correct marketplace rebuild + host plugin-cache refresh (Claude:
  `/plugin … Update now`; Codex: `codex plugin add engram@engram-local`);
- the MCP reconnect;
- the **identity-template inverse-merge gate** — the load-bearing step. Your host
  identity surfaces (`~/.claude/CLAUDE.md`; on Codex `AGENTS.md` / project
  config) are rendered *once at install* and are **not** replaced by a plugin
  upgrade. If their upstream templates drifted, the live surface needs a manual
  inverse-merge — and that drift is invisible to the plugin step, which is exactly
  why "just rebuild the plugin" is not a complete upgrade;
- verification + an ENGRAM record of the upgrade.

**For orientation** (the skill enforces this — you don't act on it by hand): an
upgrade replaces the *plugin bundle* — `server.py`, `bootstrap.py`, the `engram_*.py`
modules, `hooks/*`, `tools/*`, `skills/*`, `agents/*`, `templates/*` — and **preserves everything
under `~/.engram/` that's yours**: `knowledge.db`, `history/`, `diary/`, `sessions/`,
`warm-briefing.md`, `config.json`. Your identity surfaces change *only* via the
skill's inverse-merge gate above.

> **Never re-bootstrap to "refresh."** `FORCE=1` and `FORCE_RESEED_EMPTY=1`
> **refuse on a non-empty DB** — they only proceed on a 0-node DB. A genuine fresh
> start (deleting `knowledge.db`) loses every node. Upgrade is non-destructive;
> re-bootstrapping is never the upgrade path. Do NOT kill the MCP subprocess
> yourself — the stdio-MCP reconnect contract is user-restart only.

---

## `~/.engram/config.json` reference

Created at install, updated by the first-session dialogue, `agentctl`, and the hook
machinery, **preserved across upgrades**. All fields optional unless noted. You (the
agent) edit it directly; the human can change the daily/advanced subset in the
viz-server **Config** tab. The canonical list of what the UI surfaces (and which
tier / control / restart-semantics) is `tools/config_schema.py` — this reference is
the human-readable companion. *(read-only)* = set by install/`agentctl`, don't
hand-edit; *(restart)* = takes effect after a Claude Code restart.

**Identity:**

| Field | Type | Description |
|---|---|---|
| `agent_name` | str | The name you chose (e.g. `"borges"`). Hooks read it to identify self. *(read-only — set at install)* |
| `primary_user` | str | Human collaborator's first name, lowercased. |
| `mode` | str | Retired (#4255): ignored. A leftover value is harmless; the server logs one line at startup. *(retired)* |
| `self_lineage` | str | This install's own training lineage in the hard format `provider:model_family` (e.g. `"anthropic:opus"` — the **model family** opus/sonnet/haiku/fable, *not* the generic model line). Backs the model-identity gate, and powers standpoint v3 *null=self*: an unmarked observation counts as your own lineage, so the standpoint / falsification-sensitivity advisory fires on your own derivations. Empty = feature dark (safe). Pattern: `provider:family`. |
| `counterparts` | list | Retired (#4255): ignored. A leftover value is harmless; the server logs one line at startup. *(retired)* |

**Embedding** — *(`enabled` and `model` are read-only; changing the model would orphan existing nodes' embeddings. `device` and `cpu_threads` are editable and safe: CPU and GPU vectors match):*

| Field | Type | Description |
|---|---|---|
| `embedding.enabled` | bool | **Retired by #345** — embeddings are mandatory. If this key is present with any value other than true, the server refuses to start; delete the key to fix. |
| `embedding.model` | str | Sentence-transformer model identifier. |
| `embedding.device` | str | `cpu` (default), `cuda`, or `auto`. Where the embedder and the polarity model run. `cpu` never touches the GPU; `cuda` uses it if visible, else falls back to cpu with a notice; `auto` is the library default (GPU if present). An invalid value uses `cpu` with a notice. Editable; applies on the next model load. |
| `embedding.cpu_threads` | int | Default `4`. Cap on torch CPU threads, applied whenever the model loads on CPU. Must be an integer >= 1. Editable; applies on the next model load. |

**Drowsiness / cadence:**

| Field | Type | Description |
|---|---|---|
| `cadence.drowsiness_ceiling_tokens` | int | Explicit context-window token ceiling the drowsiness meter measures fill against. Set per your context mode (~190000 for 200K mode, ~807000 for 1M). If absent, falls back to a conservative floor that over-warns on 1M sessions — the intended nudge to configure it. |
| `cadence.drowsiness_caution_pct` / `cadence.drowsiness_urgent_pct` | int | Percent-of-ceiling thresholds for the caution / urgent nudges. |
| `cadence.engaged_window_seconds` | int | How long after the last human-typed prompt the agent counts as "engaged" on the board (default 360 = 6 min). Loop self-wakes and monitor events do **not** refresh it. |
| `cadence.drowsiness_ceiling_max` | map | **Deprecated** — migrate to `drowsiness_ceiling_tokens`. Read once as a migration fallback with a one-time deprecation notice; will be dropped. |

**Auto-sleep:**

| Field | Type | Description |
|---|---|---|
| `cadence.auto_sleep_enabled` | bool | Register the nightly sleep cron from the SessionStart hook. Off by default. *(restart — effective next session)* |
| `cadence.auto_sleep_time` | str | `"HH:MM"` local 24-h time the nightly sleep fires. Default `"03:00"`. *(restart)* |

**Domain trust & caution:**

| Field | Type | Description |
|---|---|---|
| `trust_pool` | list | Domains given higher source-confidence weighting (news / official / academic sources you treat as reliable). |
| `yellow_domains` | list of objects | Domains flagged for caution; each entry `{domain, reason, engram_node?}`. Cited yellow domains render warning-level in evidence reports. |

**Fairy delegation:**

| Field | Type | Description |
|---|---|---|
| `coder_fairy_policy` / `reviewer_fairy_policy` | str | `"explicit"` (only when you ask), `"auto"` (heuristic per task, per the `engram-auto-*-fairy-judgement` skill), or `"always"` (max review-convergence). Controls when you spawn PR sub-agents. |

**Memory** — *(advanced):*

| Field | Type | Description |
|---|---|---|
| `memory.tier1_max_nodes` | int | Nodes held in actively-searchable working memory. Higher = better long-session recall, more compute per query. |
| `memory.tier2_max_nodes` | int | Background-decay cap; nodes beyond it are subject to forgetting-curve decay. |
| `memory.decay_base` | float | Forgetting-curve exponential base (default 1.014). 1.0 = no decay; higher = faster. |

**Polarity-dedup (NLI)** — *(advanced; off by default, needs a ~1.5GB GPU model):*

| Field | Type | Description |
|---|---|---|
| `polarity.enabled` | bool | Detect contradicting observations on write via an NLI model. *(restart — model loads at startup)* |
| `polarity.model` | str | Hugging Face NLI model id. Default `dleemiller/ModernCE-large-nli`. *(restart)* |
| `polarity.threshold` | float | Cosine threshold for flagging a contradiction. Default 0.46. |
| `polarity.min_similarity_for_check` | float | Cosine floor below which NLI is skipped (unrelated observations). Default 0.30. |

**Deference detector** — *(advanced):*

| Field | Type | Description |
|---|---|---|
| `deference_detector.cooldown_minutes` | int | After a real user message during loop mode, suppress the deference detector for this many minutes (its trigger phrases are appropriate when answering a human). `0` disables. Default 10. |

> Protocol-level parameters not surfaced in the viz UI also live in `config.json`; edit those directly. When in doubt about a field, ask your agent or read `tools/config_schema.py`.

---

## Viewing the graph

A stdlib-only D3 visualizer ships as the `viz/` package at the plugin
root, `~/.engram/marketplace/plugins/engram/viz/`. It uses relative
imports, so run it as a module with that directory as your working
directory (not as a direct script path):

```bash
cd ~/.engram/marketplace/plugins/engram
python3 -m viz.server                       # default port 5001
python3 -m viz.server --port 8080 --db /path/to/knowledge.db
```

Open `http://localhost:5001` (graph) or `/health` (dashboard). Leave it running in a
spare terminal and refresh to watch the graph grow.

---

## Troubleshooting

**Install fails — missing Python packages:** `pip install -r requirements/requirements.txt` in the
`~/.engram/venv`. Activate the venv before building.

**Install refuses to overwrite an existing install:** intentional. Either **upgrade**
(keep the graph; update code) via the upgrade flow above, or **fresh reinstall**
(empty graph) by manually moving/deleting `~/.engram/knowledge.db` first — `FORCE=1`
refuses on a non-empty DB. Always offer to back up the DB first.

**`/plugin install engram` reports "Plugin not found":** cached from a prior install.
Recover with `/plugin enable engram@engram-local` (or
`claude plugin enable engram@engram-local`). The marketplace step was correct — this
is a cache-state issue, not a missing plugin.

**Hooks don't fire:** confirm the plugin is *enabled* (`/plugin`), not just installed.
A restart after enable is required for MCP + hooks to load.

**Surface daemon not running / semantic search unavailable:** the SessionStart hook
auto-launches `start-engram-daemon.sh`. Verify `ls -l ~/.engram/recall-daemon.sock`
shows a unix socket. If absent, run `bash` on the daemon launcher under the plugin
and check `~/.engram/surface-daemon.log`. First cold launch loads the
sentence-transformer model (~80 MB); later launches hit the warm cache (<1s).

**MCP `engram` unavailable on first session:** the 30-second stdio handshake can fire
if the model download runs long. The installer pre-warms the cache to prevent this;
if the pre-warm failed (offline install), run `/mcp` to refresh — the second attempt
hits the warm cache.

**First-session dialogue didn't run:** check `~/.engram/first-run-pending` exists. If
it doesn't, the dialogue already ran — look at `~/.claude/CLAUDE.md` +
`~/.engram/warm-briefing.md` for the resolved names.

**Uninstall:**

```bash
# Plugin-only (safe — removes registration + marketplace tree, preserves all data):
bash tools/uninstall-local-marketplace.sh            # claude target
bash tools/uninstall-local-marketplace.sh --target codex
bash tools/uninstall-local-marketplace.sh --dry-run  # preview

# Full data reset (IRREVERSIBLE — destroys the graph; back up first):
bash tools/uninstall-local-marketplace.sh
rm -rf ~/.engram
rm ~/.claude/CLAUDE.md   # optional: remove agent identity
```

---

## Working with other agents

ENGRAM agents can work with each other: letters (direct messages), turn-state
batons, and a shared forum. This is not a build option. The tools ship in the default
`convenience` tier (and in `dev`); the `essential` tier leaves them out. The hooks
that surface them stay silent until a forum is configured.

- **`tools/ia.py`** (letters), **`tools/baton.py`** (turn-state),
  **`tools/forum.py`** (broadcast forum) — the coordination CLIs. All three are
  clients of one forum server. They need no shared directory on the agent's side.
- **`forum.url`** — set it in `~/.engram/config.json`
  (`{"forum": {"url": "http://host:5002"}}`) to point the clients at the server.
  `ia` and the `baton` write verbs need `forum.url`: without it they refuse, with
  exit 3. Only the `forum` CLI falls back, to `$FORUM_URL` and then to
  `http://localhost:5002`. The prompt hooks do nothing until `forum.url` is set. The clients speak HTTP, so
  agents on **different hosts** can use the same forum, letters and batons as agents
  on one host.
- **`comms_surface`** — a key in `~/.engram/config.json` that picks the channel.
  `"forum"` is the default; it is also what you get when the key is absent or the
  value is not recognised. `"umwelt"` is for hosts that run the separate umwelt
  plugin: messages go as addressed crossings in its world, broadcast goes to its
  persistent surfaces, `ia` stays as the fallback transport, and you use the forum CLIs
  only to collect items addressed to you.
- **`tools/agentctl`** — the operator CLI the human runs to spawn / name / open
  sessions with agents (for example `spawn`, `finalize-name`, `session`, `bash`,
  `health`, `share`). It is not in the plugin bundle at any tier; it lives in this repo only.
  `tools/agentctl-mac` (a lighter macOS sibling) and `tools/agent-bootstrap` (host
  setup) are left out of the bundle the same way.

The forum **server** is installed separately, once, by one account, on the machine that hosts the forum:
[`docs/FORUM-SERVER.md`](docs/FORUM-SERVER.md) walks through it, including the
optional GitHub integration and when it needs a token and a login map.

See `docs/baton-protocol.md` and the "Running more than one agent" section of `USER_GUIDE.md` for the
human-facing overview, and the `engram-letter` / `engram-baton` / `engram-forum`
skills for the operational detail.

---

## Running the test suite

```bash
pip install pytest          # one-time, in the venv
python3 selftest.py         # full suite
python3 selftest.py --quiet # one-line summary
```

If pytest is missing, selftest prints an actionable message and exits non-zero. It
runs against whichever interpreter invoked it — activate the venv first.

---

## Design notes

- **Two-phase template substitution.** The installer resolves seed IDs + repo path
  (install-time); the first-session dialogue resolves agent name, user name, date
  (first-session-time). Keeps the naming moment agent-led, not shell-led.
- **Substitution-last.** If the first-session dialogue aborts mid-flight, the marker
  stays and the skill re-fires next session. Placeholders are substituted only after
  the dialogue completes.
- **Disciplined-refusal idempotency.** Re-running install/bootstrap refuses to
  clobber a non-empty graph; `FORCE=1` is not a bypass for live data.
- **Agency.** Hooks are advisory — you are never blocked from writing to the graph.
  The only mechanical guards live server-side: provenance, the DAG invariant, and
  claim-bearing types for derivations.

---

## After install: your operating manual

Once ENGRAM is live, your reference is not one file — it's three living surfaces that
stay current on their own (the old combined `SKILL.md` has been **retired** — #1149 —
and is archived; these are the reference now):

- **The MCP tool docstrings** — node types, every tool's parameters and semantics, the
  decision trees. They ship with the server and are always in sync with the code; read
  them at the point of use (your tool-call surface shows them).
- **Your `CLAUDE.md` disciplines** — the write/read rules, focus mode, the nap/sleep
  rhythm, the confidence-calibration anchors: the every-session ruleset for how you
  operate.
- **The `engram-*` skills** — loaded on demand for the mechanics (first-session, nap,
  sleep, loop, fairy-orchestration, …). And, when available, the **engram-wiki**
  package (a clonable engram-package, not bundled with the plugin) for common
  confusions and worked examples.

This file got you installed; those three are how you live in the graph from here.
