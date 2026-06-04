---
name: engram-upgrade
description: Use when upgrading an existing plugin-mode ENGRAM install to newer alpha code while preserving the graph, history, diary, and sessions. Walks through the source-tree pre-flight + change-set review + `install-local-marketplace.sh` rebuild + the user-run `/plugin marketplace update` + `/plugin upgrade engram` + MCP reconnect + the CLAUDE.md.template inverse-merge gate + verification + ENGRAM record. Each step is a verified checkpoint, not a context-impression. (Scatter-mode upgrades are retired; one-time scatter→plugin migration is `tools/migrate-to-plugin.sh`, not this skill.)
---

# ENGRAM Upgrade (Plugin) — Step-by-Step

Upgrading a plugin-mode ENGRAM install is mechanically simple but keeps one well-known failure mode: **trusting context-impression that a step is done** instead of verifying its completion checkpoint. This skill codifies each step + its verification so nothing is silently skipped.

**The architecture that makes plugin upgrades simple:** your *data* (`~/.engram/knowledge.db`, history, diary, sessions) and your *identity surfaces* (`~/.claude/CLAUDE.md`, warm-briefing) are **never touched** by a plugin upgrade. The *code* (server, hooks, tools, skills, agents) lives in the plugin tree (`~/.engram/marketplace/plugins/engram/`), and the whole tree is replaced atomically by `/plugin upgrade`. That's why the scatter-era manual steps are gone: **hooks, skills, agents, and tools upgrade with the plugin automatically** (the `~/.engram/tools` and `~/.engram/hooks` symlinks point into the plugin tree, so references keep resolving).

**The one residual hard gate:** `~/.claude/CLAUDE.md` is user-scope identity — rendered from `templates/CLAUDE.md.template` at install time and *not* plugin-owned. If the template changed upstream, the live file needs an inverse-merge (Step 6). That's the load-bearing judgment step of this skill.

**When to use:**
- The install is plugin-mode (`~/.engram/marketplace/plugins/engram/plugin.json` exists) and the user wants newer alpha code without losing data
- A new alpha commit shipped a feature/fix the user wants

**When NOT to use:**
- Fresh install (no `~/.engram/knowledge.db`) — that's the plugin install path in the README, not an upgrade
- **Still on the scatter layout?** The scatter upgrade path (`tools/deploy.sh` rsync) is **retired**. The supported move is the one-time scatter→plugin migration: `tools/migrate-to-plugin.sh` (see its `--help`; needs the human present for the `/plugin install` + `/mcp` pauses)
- The MCP server has been actively crashing and you suspect data corruption — that's an `engram-surgical` call, not a routine upgrade

---

## Step 0 — Confirm plugin mode + existing data

```bash
ls -la ~/.engram/knowledge.db
ls ~/.engram/marketplace/plugins/engram/plugin.json
```

Both must exist. If `knowledge.db` exists but the plugin marker does NOT, this is a scatter install — **stop**; the right procedure is `tools/migrate-to-plugin.sh` (one-time migration), not this skill.

Surface to the user:

> You're on the plugin install with an existing graph at `~/.engram/`. I can rebuild the local marketplace from newer alpha code; you'll then run `/plugin marketplace update` + `/plugin upgrade engram` to pull it in. Your graph, history, diary, and sessions are untouched. Want me to proceed?

Wait for an affirmative before continuing — the upgrade changes the running server + hooks behavior.

**Checkpoint**: plugin marker + knowledge.db confirmed present + user explicitly approved.

---

## Step 1 — Locate the source tree + pre-flight

```bash
SOURCE_DIR=$(grep '^deployed_from=' ~/.engram/.deployed-version 2>/dev/null | cut -d= -f2-)
echo "source tree: $SOURCE_DIR"
```

If `.deployed-version` is missing or empty, ask the user for the alpha clone path.

> **Why later blocks re-derive this:** each Bash tool call is a fresh shell, so `$SOURCE_DIR` does not persist. Blocks below re-derive it inline — self-sufficient, not memory-dependent.

Bring the clone to the intended commit and verify:

```bash
cd "$SOURCE_DIR"
git fetch origin
git status --short          # clean (or only changes you intend to ship)
git checkout dev && git pull origin dev
git log --oneline -1        # confirm the commit the user wants
```

If the working tree has uncommitted changes you didn't mean to ship, stash or commit them first — the build packages whatever the tree is at.

**Checkpoint**: source-tree branch + commit confirmed against the user's expectation.

---

## Step 2 — Review the change set (pre-upgrade)

Read what you're about to apply BEFORE any mutation, so you can flag concerns and abort cleanly.

```bash
SOURCE_DIR=$(grep '^deployed_from=' ~/.engram/.deployed-version | cut -d= -f2-)
DEPLOYED=$(grep '^alpha_sha=' ~/.engram/.deployed-version | cut -d= -f2-)
git -C "$SOURCE_DIR" log --oneline "$DEPLOYED..origin/dev"
```

- Empty output → nothing to upgrade; tell the user and exit the flow.
- ≤ 20 PRs → list each short title. More → group by theme ("5 backup PRs", "3 forum", …); cap the log with `-n 20` + a "+ N older" note.
- You may `gh pr view <N>` on at most ONE PR for depth. Do NOT loop over all PRs fetching bodies (token budget).
- Write a ≤4-bullet summary: what surfaces are touched, anything deploy-fragile (`server.py` / hooks / migrations), anything user-facing. Surface it; pause on anything concerning.

**Also note here** whether `templates/CLAUDE.md.template` appears in the change set — that pre-announces Step 6's gate:

```bash
git -C "$SOURCE_DIR" log --oneline "$DEPLOYED..origin/dev" -- templates/CLAUDE.md.template
```

**Checkpoint**: change-set summary surfaced + user confirmed proceed (or concern resolved) + CLAUDE.md-template-changed noted yes/no.

---

## Step 3 — Rebuild + re-assemble the local marketplace (agent-run)

```bash
SOURCE_DIR=$(grep '^deployed_from=' ~/.engram/.deployed-version | cut -d= -f2-)
cd "$SOURCE_DIR"
bash tools/install-local-marketplace.sh
```

This single script is the whole agent-side upgrade: it **reads your `config.json`** (`multi_agent`, `install_tier`) and **builds the plugin with those flags** (via `tools/build-plugin.sh`), then re-assembles `~/.engram/marketplace/` from the fresh `build/plugin/`. Idempotent — safe to re-run.

**Watch the build log for the feature-set lines** — this is where a silent mis-build would happen:
- The tier/multi-agent line must match your install (e.g. `Tier: dev …, multi-agent: yes`). A multi-agent install that builds single-agent **silently drops** baton/letters/forum surfaces (#704 class).
- If your `install_tier` is null/unset, the build falls back to the manifest default (`convenience`) — a dev-tier install would silently lose its dev tools (#707). Set `install_tier` in `~/.engram/config.json` before building if it's unset.

**Checkpoint**: script exited 0 + marketplace re-assembled + the logged tier/multi-agent flags match `~/.engram/config.json` + the `Version marker refreshed` line shows the new `alpha_sha` (the script maintains `~/.engram/.deployed-version` so Steps 2 and 7 stay anchored).

---

## Step 4 — Pull the new plugin version (USER action)

The agent cannot run slash-commands. Tell the user:

> Marketplace rebuilt at the new commit. In this Claude Code session please run:
> `/plugin marketplace update` (refresh the local marketplace index)
> `/plugin upgrade engram` (pull the new plugin version)

**Checkpoint**: user confirms both commands ran (or explicitly defers — if deferred, the new code is NOT live; track it so it doesn't fall off).

---

## Step 5 — Reconnect MCP (USER action)

The plugin tree now has the new `server.py`, but the running MCP server is still the old process. Tell the user:

> Please reconnect the engram MCP server: run `/mcp` and reconnect engram, or restart Claude Code. Hooks reload per-fire automatically — no restart needed for those.

**Do NOT** kill the MCP subprocess yourself — the stdio-MCP contract is user-restart only; killing it from an agent breaks the harness transport.

**Checkpoint**: user reconnected (or explicitly deferred, noted as code-not-yet-running).

---

## Step 6 — CLAUDE.md template inverse-merge (HARD GATE, the residual)

If Step 2 found `templates/CLAUDE.md.template` changed in the range, **do not skip this**. `~/.claude/CLAUDE.md` is user-scope identity — the plugin upgrade did not touch it, so template-side additions (new disciplines, corrected rules) have NOT reached the running agent.

**The "I'll come back to it" failure mode is real** (2026-05-16: a template discipline-change was deferred post-deploy, never merged, and misfired for days before diagnosis). Resolve it now or track it explicitly — never silent-defer.

```bash
SOURCE_DIR=$(grep '^deployed_from=' ~/.engram/.deployed-version | cut -d= -f2-)
diff "$SOURCE_DIR"/templates/CLAUDE.md.template ~/.claude/CLAUDE.md
```

- Inverse-merge template-side additions that are identity- or discipline-forming into the live file; **preserve agent-local customizations** (the template is the floor, not the ceiling).
- Mechanical change → just copy. Judgment-laden (new discipline, conflicts with local content) → surface the diff to the user and decide together.
- Verify with grep-presence + a re-diff whose remaining hunks are each either (i) a local customization you're keeping or (ii) a template addition you deliberately declined.

**What you do NOT need anymore (scatter-era steps, now plugin-owned):** no `settings.json` hook merge (the plugin's `hooks.json` registers hooks), no manual `cp` of skills/agents (they ship inside the plugin tree and updated in Step 4).

**Checkpoint**: template changes inverse-merged + verified, OR explicitly deferred with the user's agreement AND tracked in the ask-user queue.

---

## Step 7 — Verify

```bash
cat ~/.engram/.deployed-version          # marker SHA = the commit you built (forensics anchor)
```

Then through the **plugin MCP** (after Step 5's reconnect): call `engram_stats` and confirm it returns your real node count — this proves the new server is running against your untouched graph. If `engram_stats` errors or the node count is implausible, stop and investigate before declaring the upgrade done.

**Checkpoint**: marker SHA matches the intended commit + `engram_stats` returns the expected graph through the new server.

---

## Step 8 — Record the upgrade in ENGRAM

```python
engram_add_observation(payload_json=json.dumps({
    "claim": "ENGRAM plugin upgrade <YYYY-MM-DD HH:MM TZ>: marketplace rebuilt at <commit SHA> (<branch>), /plugin upgrade run, MCP reconnected: <done|deferred>. CLAUDE.md template gate: <merged paths | 'no template changes' | 'deferred+tracked'>. engram_stats verified: <N> nodes.",
    "url": "file:///home/<user>/.engram/.deployed-version",
    "title": "ENGRAM plugin upgrade <YYYY-MM-DD> — <commit SHA>",
    "quoted_text": "<paste the contents of .deployed-version>",
    "quote_type": "hard_data",
    "source_class": "introspective",
    "source_date": "<YYYY-MM-DD>",
}))
```

**Checkpoint**: obs filed; its SHA matches Step 7's marker.

---

## Anti-patterns

- **Context-impression that a step is done.** Every step has a checkpoint command — verify explicitly, not from "I think I did that." This is the failure mode the skill exists to close.
- **Running the upgrade slash-commands yourself.** `/plugin …` and `/mcp` are user actions; the agent's job is the rebuild (Step 3) and the gates.
- **Skipping Step 6 because the template diff "looked fine."** The CLAUDE.md inverse-merge is the one place a plugin upgrade still needs judgment. If deferring, write it to the ask-user queue immediately.
- **Building without checking the feature-set flags.** A wrong-flag build *succeeds* and silently drops surfaces (#704/#707 class). The Step 3 checkpoint exists for this.
- **Re-bootstrapping (`FORCE=1`) to "refresh" anything.** `FORCE=1` only bypasses the existing-install guard — bootstrap then writes seed nodes INTO the existing graph (it deletes nothing). Never the upgrade path; a true fresh start is a manual `knowledge.db` delete.
- **Killing the MCP subprocess to "restart" it.** User-restart only.
- **Reaching for the retired scatter path** (`tools/deploy.sh` rsync into `~/.engram/`) on a plugin install — it does not serve the plugin layout and will not update the running plugin.

---

## Relation to other skills

- **`tools/migrate-to-plugin.sh`** — the one-time scatter→plugin migration for any install still on the retired scatter layout. Different procedure; needs the human for the `/plugin install` + `/mcp` pauses. This skill takes over for all upgrades *after* that migration.
- **engram-self-improve** — a self-improvement code change ships through this skill once merged to the alpha source.
- **engram-surgical** — data-corruption recovery, NOT routine upgrades.
- **engram-nap / engram-sleep** — an upgrade is a meaningful state-change worth a nap checkpoint if context permits (Step 8's obs is the minimum record; a nap captures the wider session context).
