---
name: engram-upgrade
description: Use when upgrading an existing plugin-mode ENGRAM install to newer alpha code while preserving the graph, history, diary, and sessions. Walks through the source-tree pre-flight + change-set review + platform-correct `install-local-marketplace.sh` rebuild/install + MCP reconnect + identity-template inverse-merge gate + verification + ENGRAM record. Each step is a verified checkpoint, not a context-impression. (Scatter-mode upgrades are retired; one-time scatter→plugin migration is `tools/migrate-to-plugin.sh`, not this skill.)
---

# ENGRAM Upgrade (Plugin) — Step-by-Step

Upgrading a plugin-mode ENGRAM install is mechanically simple but keeps one well-known failure mode: **trusting context-impression that a step is done** instead of verifying its completion checkpoint. This skill codifies each step + its verification so nothing is silently skipped.

**The architecture that makes plugin upgrades simple:** your *data* (`~/.engram/knowledge.db`, history, diary, sessions) and your *identity surfaces* (`~/.claude/CLAUDE.md`, warm-briefing) are **never touched** by a plugin upgrade. The *code* (server, hooks, tools, skills, agents) lives in the plugin tree (`~/.engram/marketplace/plugins/engram/`) and the host plugin cache. Claude Code replaces the cache via `/plugin upgrade`; Codex refreshes it via `codex plugin add engram@engram-local`. That's why the scatter-era manual steps are gone: **hooks, skills, agents, and tools upgrade with the plugin automatically**. Helper paths are host-dependent: use the installed plugin tree/cache (e.g. `~/.codex/plugins/cache/.../tools/verify_quote.py` on Codex). Note: `~/.engram/marketplace/plugins/engram/` IS the plugin tree replaced atomically by `/plugin upgrade`; `~/.engram/hooks` and `~/.engram/tools` are independent data directories with separate inodes — they are NOT symlinks into the plugin tree and are NOT touched by the upgrade.

**The one residual hard gate:** the host identity surface is user-scope identity and is *not* plugin-owned. On Claude this is usually `~/.claude/CLAUDE.md` rendered from `templates/template.CLAUDE.md`; on Codex it may be `~/.codex/config.toml` instructions plus project `AGENTS.md` from `templates/template.AGENTS.md`. If the relevant upstream template changed, the live surface needs an inverse-merge (Step 6). That's the load-bearing judgment step of this skill.

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

> You're on the plugin install with an existing graph at `~/.engram/`. I can rebuild the local marketplace from newer alpha code and refresh the host plugin cache (Claude: via slash-command steps; Codex: via `codex plugin add`). Your graph, history, diary, and sessions are untouched. Want me to proceed?

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

**Also note here** whether any CLAUDE.md render source appears in the change set — that pre-announces Step 6's gate. The rendered `~/.claude/CLAUDE.md` is assembled from **multiple** upstream sources: `templates/template.CLAUDE.md` + `templates/compact-instructions.md` (folded in by bootstrap at install-time). A change to either file produces rendered-CLAUDE.md drift. Check both:

```bash
git -C "$SOURCE_DIR" log --oneline "$DEPLOYED..origin/dev" -- src/engram/templates/template.CLAUDE.md src/engram/templates/compact-instructions.md
```

**Checkpoint**: change-set summary surfaced + user confirmed proceed (or concern resolved) + CLAUDE.md-template-changed noted yes/no.

---

## Step 3 — Rebuild + re-assemble the local marketplace (agent-run)

First identify the host target. In Codex sessions, use `codex`; otherwise default to `claude-code`.

```bash
SOURCE_DIR=$(grep '^deployed_from=' ~/.engram/.deployed-version | cut -d= -f2-)
cd "$SOURCE_DIR"
TARGET=claude-code   # or: TARGET=codex in Codex sessions
bash tools/install-local-marketplace.sh --target "$TARGET"
```

This single script is the whole agent-side upgrade: it **reads your `config.json`** (`multi_agent`, `install_tier`) and **builds the plugin with those flags** plus the explicit host target (via `tools/build-plugin.sh`), then re-assembles `~/.engram/marketplace/` from the fresh `build/plugin/`. For Codex, it also runs `codex plugin add engram@engram-local` when `codex` is on PATH. Idempotent — safe to re-run.

**Watch the build log for the feature-set lines** — this is where a silent mis-build would happen:
- The tier/multi-agent line must match your install (e.g. `Tier: dev …, multi-agent: yes`). A multi-agent install that builds single-agent **silently drops** baton/letters/forum surfaces (#704 class).
- If your `install_tier` is null/unset, the build falls back to the manifest default (`convenience`) — a dev-tier install would silently lose its dev tools (#707). Set `install_tier` in `~/.engram/config.json` before building if it's unset.

**Checkpoint**: script exited 0 + marketplace re-assembled + the logged target/tier/multi-agent flags match the host and `~/.engram/config.json` + `platform.json` in the marketplace has the intended target + the `Version marker refreshed` line shows the new `alpha_sha` (the script maintains `~/.engram/.deployed-version` so Steps 2 and 7 stay anchored).

---

## Step 4 — Pull the new plugin version (host-specific)

**Claude Code:** the agent cannot run slash-commands. Tell the user:

> Marketplace rebuilt at the new commit. In this Claude Code session please run:
> 1. `/plugin marketplace update engram-local` (refresh the marketplace registration — the marketplace NAME argument matters)
> 2. `/plugin` → **Installed** → select the **engram plugin** entry (the plugin itself, NOT the engram MCP server listed under it) → **Update now**
>
> (The `/mcp` reconnect is Step 5, after the plugin updates.)

**Codex:** `tools/install-local-marketplace.sh --target codex` runs `codex plugin add engram@engram-local` itself when `codex` is on PATH. Verify the cache actually changed:

```bash
sha1sum ~/.engram/marketplace/plugins/engram/hooks/engram-stop-hook.py
sha1sum ~/.codex/plugins/cache/engram-local/engram/0.1.0/hooks/engram-stop-hook.py
cat ~/.codex/plugins/cache/engram-local/engram/0.1.0/platform.json
```

**Checkpoint**: Claude user confirms both slash commands ran, OR Codex cache/platform/hash verification shows the installed cache matches the marketplace. If deferred, the new code is NOT live; track it so it doesn't fall off.

---

## Step 5 — Reconnect MCP (USER action)

The plugin tree/cache now has the new `server.py`, but the running MCP server may still be an old process. Tell the user:

> Please reconnect the engram MCP server: Claude — run `/mcp` and reconnect engram, or restart Claude Code. Codex — restart/resume the Codex session if MCP or hooks are still using stale plugin state; approve refreshed hook definitions if prompted.

**Do NOT** kill the MCP subprocess yourself — the stdio-MCP contract is user-restart only; killing it from an agent breaks the harness transport.

**If Step 2's change set touched `hooks/hooks.json`, a `/mcp` reconnect is NOT enough — you need a full Claude Code restart.** `/mcp` reloads the MCP server process but does NOT reload the hook registration; a removed hook will throw a dangling-file error on the next event, and an added hook won't fire at all, until Claude Code is fully restarted. The `install-local-marketplace.sh` script will have printed an `⚠️  HOOK REGISTRATION CHANGED` banner if this applies.

**Checkpoint**: user reconnected (or explicitly deferred, noted as code-not-yet-running).

---

## Step 5a — Shared-bin CLI refresh (multi-agent only)

**If multi-agent:** After the plugin rebuild, check shared-bin CLI drift — the next prompt will surface a `⚠️ shared-bin drift` banner if drift is detected. To refresh proactively: for each of `{ia,baton,forum}`, compare `/home/agents-shared/bin/<cli>` with `$CLAUDE_PLUGIN_ROOT/tools/<src>.py` (use `diff` or `md5sum`) and `sudo cp` to refresh any that differ before other agents resume. This prevents the silent-degradation class where agents run a stale shared CLI against a newly-upgraded hook.

---

## Step 6 — CLAUDE.md template inverse-merge (HARD GATE, the residual)

If Step 2 found any CLAUDE.md render source changed in the range, **do not skip this**. The rendered `~/.claude/CLAUDE.md` is assembled from multiple upstream sources — `templates/template.CLAUDE.md` AND `templates/compact-instructions.md` — so a `compact-instructions.md`-only change is just as drift-producing as a template change. On Codex, also check `templates/template.AGENTS.md` and any local first-person instructions in `~/.codex/config.toml`/project `AGENTS.md`. The plugin upgrade did not touch these user-scope identity surfaces, so source-side additions may NOT have reached the running agent.

**The "I'll come back to it" failure mode is real** (2026-05-16: a template discipline-change was deferred post-deploy, never merged, and misfired for days before diagnosis). Resolve it now or track it explicitly — never silent-defer.

Use the rendered-diff tool (catches changes to ALL render sources, not just the main template):

```bash
SOURCE_DIR=$(grep '^deployed_from=' ~/.engram/.deployed-version | cut -d= -f2-)
DEPLOYED=$(grep '^alpha_sha=' ~/.engram/.deployed-version | cut -d= -f2-)
# Claude — compare rendered output between the deployed commit and current dev:
python "$SOURCE_DIR/tools/check-claude-md-drift.py" \
    --repo "$SOURCE_DIR" \
    --base "$DEPLOYED" \
    --head origin/dev
# Exit 0 = no drift; exit 1 = diff printed to stdout; any other exit = tool/git error (investigate, do NOT read as drift).
# Codex (template.AGENTS.md is single-source; raw diff is sufficient):
diff "$SOURCE_DIR"/src/engram/templates/template.AGENTS.md /path/to/live/AGENTS.md
```

- Inverse-merge template-side additions that are identity- or discipline-forming into the live file; **preserve agent-local customizations** (the template is the floor, not the ceiling).
- Mechanical change → just copy. Judgment-laden (new discipline, conflicts with local content) → surface the diff to the user and decide together.
- Verify with grep-presence + a re-diff whose remaining hunks are each either (i) a local customization you're keeping or (ii) a template addition you deliberately declined.

**What you do NOT need anymore (scatter-era steps, now plugin-owned):** no manual hook merge (the plugin's `hooks.json` registers hooks), no manual `cp` of skills/agents (they ship inside the plugin tree/cache and updated in Step 4).

**Checkpoint**: template changes inverse-merged + verified, OR explicitly deferred with the user's agreement AND tracked in the ask-user queue.

---

## Step 7 — Verify

```bash
cat ~/.engram/.deployed-version          # marker SHA = the commit you built (forensics anchor)
```

Then through the **plugin MCP** (after Step 5's reconnect): call `engram_stats` and confirm it returns your real node count — this proves the new server is running against your untouched graph. On Codex, also verify installed-cache files match the marketplace if hooks are the upgrade target. If `engram_stats` errors or the node count is implausible, stop and investigate before declaring the upgrade done.

**Checkpoint**: marker SHA matches the intended commit + `engram_stats` returns the expected graph through the new server.

---

## Step 8 — Record the upgrade in ENGRAM

```python
engram_add_observation(payload_json=json.dumps({
    "claim": "ENGRAM plugin upgrade <YYYY-MM-DD HH:MM TZ>: <host target> marketplace/cache rebuilt at <commit SHA> (<branch>), host plugin refresh: <done|deferred>. MCP reconnected: <done|deferred>. Identity-template gate: <merged paths | 'no template changes' | 'deferred+tracked'>. engram_stats verified: <N> nodes.",
    "interpretation": "Why the quoted evidence proves the upgrade state.",
    "url": "file:///path/to/committed-or-transcript-evidence",
    "title": "ENGRAM plugin upgrade <YYYY-MM-DD> — <commit SHA>",
    "quoted_text": "<exact verified quote from committed evidence or session transcript>",
    "quote_type": "hard_data",
    "source_class": "introspective",
    "source_date": "<YYYY-MM-DD>",
}))
```

Do **not** cite `file://~/.engram/.deployed-version` directly unless it is committed/tracked evidence. ENGRAM's provenance guard may reject untracked files; use a verified transcript/log quote instead.

**Checkpoint**: obs filed; its SHA/target/cache claim matches Step 7's verified evidence.

---

## Anti-patterns

- **Context-impression that a step is done.** Every step has a checkpoint command — verify explicitly, not from "I think I did that." This is the failure mode the skill exists to close.
- **Running Claude slash-commands yourself.** `/plugin …` and `/mcp` are Claude user actions. Codex is different: `codex plugin add engram@engram-local` is an ordinary CLI command and can be run by the agent.
- **Skipping Step 6 because the template diff "looked fine."** The identity-surface inverse-merge is the one place a plugin upgrade still needs judgment. If deferring, write it to the ask-user queue immediately.
- **Building without checking the feature-set flags.** A wrong-flag build *succeeds* and silently drops surfaces (#704/#707 class). The Step 3 checkpoint exists for this.
- **Re-bootstrapping (`FORCE=1`) to "refresh" anything.** `FORCE=1` (and `FORCE_RESEED_EMPTY=1`) now **refuse on a non-empty DB** — they are not bypass paths for live graphs. Never the upgrade path; a true fresh start requires manually deleting `knowledge.db` first.
- **Killing the MCP subprocess to "restart" it.** User-restart only.
- **Reaching for the retired scatter path** (`tools/deploy.sh` rsync into `~/.engram/`) on a plugin install — it does not serve the plugin layout and will not update the running plugin.

---

## Relation to other skills

- **`tools/migrate-to-plugin.sh`** — the one-time scatter→plugin migration for any install still on the retired scatter layout. Different procedure; needs the human for the `/plugin install` + `/mcp` pauses. This skill takes over for all upgrades *after* that migration.
- **engram-self-improve** — a self-improvement code change ships through this skill once merged to the alpha source.
- **engram-surgical** — data-corruption recovery, NOT routine upgrades.
- **engram-nap / engram-sleep** — an upgrade is a meaningful state-change worth a nap checkpoint if context permits (Step 8's obs is the minimum record; a nap captures the wider session context).
