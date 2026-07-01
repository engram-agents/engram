---
name: engram-upgrade
description: Use when upgrading an existing plugin-mode ENGRAM install to newer alpha code while preserving the graph, history, diary, and sessions. Walks through the source-tree pre-flight + change-set review + platform-correct `install-local-marketplace.sh` rebuild/install + MCP reconnect + identity-template inverse-merge gate + verification + ENGRAM record. Each step is a verified checkpoint, not a context-impression. (Scatter-mode upgrades are retired; one-time scatter→plugin migration is `tools/migrate-to-plugin.sh`, not this skill.)
---

# ENGRAM Upgrade (Plugin) — Step-by-Step

Upgrading a plugin-mode ENGRAM install is mechanically simple but keeps one well-known failure mode: **trusting context-impression that a step is done** instead of verifying its completion checkpoint. This skill codifies each step + its verification so nothing is silently skipped.

**The architecture that makes plugin upgrades simple:** your *data* (`~/.engram/knowledge.db`, history, diary, sessions) and your *identity surfaces* (`~/.claude/CLAUDE.md`, warm-briefing) are **never touched** by a plugin upgrade. The *code* (server, hooks, tools, skills, agents) lives in the plugin tree at `~/.engram/marketplace/plugins/engram/`. **For a Claude `directory`-source local marketplace — the standard plugin install — that source tree IS the live code**: `CLAUDE_PLUGIN_ROOT` resolves to it and Claude Code invokes hooks/skills/tools directly from it (verified 2026-06-28 by live hook-fire instrumentation). So once Step 3's `install-local-marketplace.sh` rebuilds the source tree, **new hook / tool / skill code is already live** (re-read per invocation) — there is no separate "pull into cache" step for them. The host plugin *cache* (`~/.claude/plugins/cache/...`) + `installed_plugins.json` are version **bookkeeping, NOT the live load path** for a directory source. Only two code kinds need an explicit post-rebuild action (Step 5): a changed **`server.py`** (the running MCP process is from session start → `/mcp` reconnect) and a changed **`hooks.json` registration** (read at session start → full restart). **Codex differs:** `codex plugin add` copies the build into Codex's own cache (`~/.codex/plugins/cache/...`) — so the Codex flow is cache-refresh-based, and you use the installed cache for helper paths (e.g. `~/.codex/plugins/cache/.../tools/verify_quote.py`). **NB:** that Codex's *cache* is its live load path is **inferred** from the explicit copy step (the copy makes the cache the install location, unlike Claude's in-place source) — it has **not** been live-fire-verified the way Claude's directory-source model was. Re-verify on Codex, especially after a Codex version bump (same instrument-and-fire method). Note: `~/.engram/hooks` and `~/.engram/tools` are independent data directories with separate inodes — NOT symlinks into the plugin tree, NOT touched by the upgrade.

**The one residual hard gate:** the host identity surface is user-scope identity and is *not* plugin-owned. On Claude this is usually `~/.claude/CLAUDE.md` rendered from `templates/template.CLAUDE.md`; on Codex it may be `~/.codex/config.toml` instructions plus project `AGENTS.md` from `templates/template.AGENTS.md`. If the relevant upstream template changed, the live surface needs an inverse-merge (Step 7). That's the load-bearing judgment step of this skill.

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

> You're on the plugin install with an existing graph at `~/.engram/`. I can rebuild the local marketplace from newer alpha code — on Claude that source rebuild makes the new hook/skill/tool code live immediately (a `/mcp` reconnect or restart is only needed if `server.py` or the hook registration changed); on Codex it refreshes the Codex cache via `codex plugin add`. Your graph, history, diary, and sessions are untouched. Want me to proceed?

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
UPSTREAM_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
# If UPSTREAM_BRANCH is "HEAD" you are in detached state — checkout your branch first (e.g. git checkout main or git checkout dev).
git pull origin "$UPSTREAM_BRANCH"
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
# Save the pre-upgrade SHA before Step 3 overwrites .deployed-version.
# Step 3's install-local-marketplace.sh refreshes the marker to the new SHA,
# so a fresh read in Step 7 yields base==head → false "no drift" (#1194).
echo "$DEPLOYED" > ~/.engram/.upgrade-pre-sha
UPSTREAM_BRANCH="$(git -C "$SOURCE_DIR" rev-parse --abbrev-ref HEAD)"
git -C "$SOURCE_DIR" log --oneline "$DEPLOYED..origin/$UPSTREAM_BRANCH"
```

- Empty output → nothing to upgrade; tell the user and exit the flow.
- ≤ 20 PRs → list each short title. More → group by theme ("5 backup PRs", "3 forum", …); cap the log with `-n 20` + a "+ N older" note.
- You may `gh pr view <N>` on at most ONE PR for depth. Do NOT loop over all PRs fetching bodies (token budget).
- Write a ≤4-bullet summary: what surfaces are touched, anything deploy-fragile (`server.py` / hooks / migrations), anything user-facing. Surface it; pause on anything concerning.

**Also note here** whether any CLAUDE.md render source appears in the change set — that pre-announces Step 7's gate. The rendered `~/.claude/CLAUDE.md` is assembled from **multiple** upstream sources: `templates/template.CLAUDE.md` + `templates/compact-instructions.md` (folded in by bootstrap at install-time). A change to either file produces rendered-CLAUDE.md drift. Check both:

```bash
UPSTREAM_BRANCH="$(git -C "$SOURCE_DIR" rev-parse --abbrev-ref HEAD)"
git -C "$SOURCE_DIR" log --oneline "$DEPLOYED..origin/$UPSTREAM_BRANCH" -- src/engram/templates/template.CLAUDE.md src/engram/templates/compact-instructions.md
```

**Checkpoint**: change-set summary surfaced + user confirmed proceed (or concern resolved) + CLAUDE.md-template-changed noted yes/no + pre-upgrade SHA saved to `~/.engram/.upgrade-pre-sha`.

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

## Step 4 — Activate the new code (host-specific)

**Claude Code (directory-source install):** there is **no "pull into cache" step** for hooks/tools/skills — Step 3's source rebuild already made them live, because Claude loads them directly from the marketplace source tree (`CLAUDE_PLUGIN_ROOT`) per invocation. The only remaining activation is code-kind-specific, and it lives in Step 5:

- **`server.py` changed** → `/mcp` reconnect (Step 5).
- **`hooks.json` registration changed** (a hook added/removed) → full Claude Code restart (Step 5).
- **Neither changed** → nothing to do; the rebuild is already live for the next prompt.

Refreshing the host plugin *cache* via `/plugin marketplace update engram-local` + `/plugin` → **Update now** is **optional bookkeeping** — it re-snapshots the cache + `installed_plugins.json` to the new version string, but it is **NOT** what makes hook/skill/tool code live on a directory install (that is the source rebuild in Step 3). Run it only if you want the cache's reported version to match; the code is already live either way.

> **Why this differs from the official-marketplace model:** for a `git`/`github`-source marketplace Claude Code loads from a *cache copy*, so `/plugin update` IS the activation. Our local marketplace is `directory`-source, whose `installLocation` is the source dir itself — so the source is live and the cache is bookkeeping. (This directory-vs-cache distinction is the root of the v0.1.4 four-day silent-hook outage; see the project `CLAUDE.md` "Plugin deploy" section.)

**Codex:** `tools/install-local-marketplace.sh --target codex` runs `codex plugin add engram@engram-local` itself when `codex` is on PATH, copying the build into Codex's cache (inferred to be Codex's live load path — not live-fire-verified; see the architecture note above). Verify the cache actually changed:

```bash
sha1sum ~/.engram/marketplace/plugins/engram/hooks/engram-stop-hook.py
sha1sum ~/.codex/plugins/cache/engram-local/engram/0.1.0/hooks/engram-stop-hook.py
cat ~/.codex/plugins/cache/engram-local/engram/0.1.0/platform.json
```

**Checkpoint**: on Claude, the Step 3 rebuild succeeded (hooks/tools/skills are already live) and any needed `server.py`/`hooks.json` activation is deferred to Step 5; on Codex, cache/platform/hash verification shows the installed cache matches the marketplace. If a needed `/mcp` reconnect or restart (Claude) or `codex plugin add` (Codex) is deferred, the affected code is NOT yet live; track it so it doesn't fall off.

---

## Step 5 — Reconnect MCP (USER action)

The plugin tree/cache now has the new `server.py`, but the running MCP server may still be an old process. Tell the user:

> Please reconnect the engram MCP server: Claude — run `/mcp` and reconnect engram, or restart Claude Code. Codex — restart/resume the Codex session if MCP or hooks are still using stale plugin state; approve refreshed hook definitions if prompted.

**Do NOT** kill the MCP subprocess yourself — the stdio-MCP contract is user-restart only; killing it from an agent breaks the harness transport.

**If Step 2's change set touched `hooks/hooks.json`, a `/mcp` reconnect is NOT enough — you need a full Claude Code restart.** `/mcp` reloads the MCP server process but does NOT reload the hook registration; a removed hook will throw a dangling-file error on the next event, and an added hook won't fire at all, until Claude Code is fully restarted. The `install-local-marketplace.sh` script will have printed an `⚠️  HOOK REGISTRATION CHANGED` banner if this applies.

**Checkpoint**: user reconnected (or explicitly deferred, noted as code-not-yet-running).

---

## Step 6 — Restart viz server (if installed as systemd service)

Skip if you don't use the viz dashboard (T2 feature, may not be installed).

The plugin rebuild (Step 3) replaced `viz_server.py` in the plugin tree, but any running viz service still executes the **old code**. Without a restart the health dashboard silently serves stale content — it appears healthy but reports on the pre-upgrade version.

**macOS note:** systemd is Linux-only. On macOS, skip to the manual-start path below. launchd integration is a known gap (see #402); it is not handled here.

```bash
# Check if a systemd service is registered (Linux only):
systemctl --user status engram-viz 2>/dev/null || echo "no systemd service"

# If registered, restart it:
systemctl --user restart engram-viz && echo "viz restarted" || echo "viz restart failed — check journalctl --user -u engram-viz"
```

**If not a systemd service** (manual-start install, or macOS): the old process is still running. Tell the user:
> The viz server is serving pre-upgrade code. Kill the old process and restart:
> `pkill -f viz_server.py; python3 ${CLAUDE_PLUGIN_ROOT:-~/.engram/marketplace/plugins/engram}/viz_server.py &`

Verify the server is live with new code after the restart:
```bash
curl -s http://localhost:5001/api/health | python3 -m json.tool
```
A `health_score` field in the response confirms the viz server is up. Step 8 also checks this.

**Checkpoint**: viz responds to `http://localhost:5001/api/health` (or the service is confirmed not installed).

---

## Step 7 — CLAUDE.md template inverse-merge (HARD GATE, the residual)

If Step 2 found any CLAUDE.md render source changed in the range, **do not skip this**. The rendered `~/.claude/CLAUDE.md` is assembled from multiple upstream sources — `templates/template.CLAUDE.md` AND `templates/compact-instructions.md` — so a `compact-instructions.md`-only change is just as drift-producing as a template change. On Codex, also check `templates/template.AGENTS.md` and any local first-person instructions in `~/.codex/config.toml`/project `AGENTS.md`. The plugin upgrade did not touch these user-scope identity surfaces, so source-side additions may NOT have reached the running agent.

**The "I'll come back to it" failure mode is real** (2026-05-16: a template discipline-change was deferred post-deploy, never merged, and misfired for days before diagnosis). Resolve it now or track it explicitly — never silent-defer.

Use the rendered-diff tool (catches changes to ALL render sources, not just the main template):

```bash
SOURCE_DIR=$(grep '^deployed_from=' ~/.engram/.deployed-version | cut -d= -f2-)
# Read the pre-upgrade SHA saved in Step 2.  Do NOT re-read .deployed-version here —
# Step 3 already overwrote it with the new SHA, so base==head → false "no drift" (#1194).
DEPLOYED=$(cat ~/.engram/.upgrade-pre-sha 2>/dev/null) || {
  echo "ERROR: ~/.engram/.upgrade-pre-sha not found — re-run Step 2 to save the pre-upgrade SHA before proceeding."
  exit 1
}
[[ -z "$DEPLOYED" ]] && { echo "ERROR: ~/.engram/.upgrade-pre-sha is empty — re-run Step 2 (check that .deployed-version has a valid alpha_sha= line)."; exit 1; }
UPSTREAM_BRANCH="$(git -C "$SOURCE_DIR" rev-parse --abbrev-ref HEAD)"
# Claude — compare rendered output between the deployed commit and current upstream branch:
python "$SOURCE_DIR/tools/check-claude-md-drift.py" \
    --repo "$SOURCE_DIR" \
    --base "$DEPLOYED" \
    --head "origin/$UPSTREAM_BRANCH"
# Exit 0 = no drift; exit 1 = diff printed to stdout; any other exit = tool/git error (investigate, do NOT read as drift).
# Codex (template.AGENTS.md is single-source; raw diff is sufficient):
diff "$SOURCE_DIR"/src/engram/templates/template.AGENTS.md /path/to/live/AGENTS.md
```

- Inverse-merge template-side additions that are identity- or discipline-forming into the live file; **preserve agent-local customizations** (the template is the floor, not the ceiling).
- Mechanical change → just copy. Judgment-laden (new discipline, conflicts with local content) → surface the diff to the user and decide together.
- Verify with grep-presence + a re-diff whose remaining hunks are each either (i) a local customization you're keeping or (ii) a template addition you deliberately declined.

**What you do NOT need anymore (scatter-era steps, now plugin-owned):** no manual hook merge (the plugin's `hooks.json` registers hooks), no manual `cp` of skills/agents (they live in the plugin source tree and go live with the Step 3 rebuild — Claude — or the Codex cache refresh).

**Checkpoint**: template changes inverse-merged + verified, OR explicitly deferred with the user's agreement AND tracked in the ask-user queue.

---

## Step 8 — Verify

```bash
cat ~/.engram/.deployed-version          # marker SHA = the commit you built (forensics anchor)
rm -f ~/.engram/.upgrade-pre-sha         # pre-upgrade SHA scratch file no longer needed
```

Then through the **plugin MCP** (after Step 5's reconnect): call `engram_stats` and confirm it returns your real node count — this proves the new server is running against your untouched graph. On Codex, also verify installed-cache files match the marketplace if hooks are the upgrade target. If `engram_stats` errors or the node count is implausible, stop and investigate before declaring the upgrade done.

**For a thorough operational health check** (recommended after any upgrade): load and run the `engram-health-exam` skill. It covers the full runtime layer — hook delivery, surface daemon, viz server, config sanity, schema/migration — beyond the single `engram_stats` probe above. The `engram_stats` call here is the minimum gate; `engram-health-exam` is the full exam.

**Checkpoint**: marker SHA matches the intended commit + `engram_stats` returns the expected graph through the new server + `.upgrade-pre-sha` scratch file removed.

---

## Step 9 — Record the upgrade in ENGRAM

```python
engram_add_observation(payload_json=json.dumps({
    "claim": "ENGRAM plugin upgrade <YYYY-MM-DD HH:MM TZ>: <host target> marketplace source rebuilt at <commit SHA> (<branch>) — hooks/skills/tools live from source (Claude) / cache refreshed (Codex). MCP reconnected: <done|deferred|n/a>. Hooks.json restart: <done|deferred|n/a>. Identity-template gate: <merged paths | 'no template changes' | 'deferred+tracked'>. engram_stats verified: <N> nodes.",
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

**Checkpoint**: obs filed; its SHA/target/cache claim matches Step 8's verified evidence.

---

## Anti-patterns

- **Context-impression that a step is done.** Every step has a checkpoint command — verify explicitly, not from "I think I did that." This is the failure mode the skill exists to close.
- **Running Claude slash-commands yourself.** `/plugin …` and `/mcp` are Claude user actions. Codex is different: `codex plugin add engram@engram-local` is an ordinary CLI command and can be run by the agent.
- **Skipping Step 7 because the template diff "looked fine."** The identity-surface inverse-merge is the one place a plugin upgrade still needs judgment. If deferring, write it to the ask-user queue immediately.
- **Building without checking the feature-set flags.** A wrong-flag build *succeeds* and silently drops surfaces (#704/#707 class). The Step 3 checkpoint exists for this.
- **Re-bootstrapping (`FORCE=1`) to "refresh" anything.** `FORCE=1` (and `FORCE_RESEED_EMPTY=1`) now **refuse on a non-empty DB** — they are not bypass paths for live graphs. Never the upgrade path; a true fresh start requires manually deleting `knowledge.db` first.
- **Killing the MCP subprocess to "restart" it.** User-restart only.
- **Reaching for the retired scatter path** (`tools/deploy.sh` rsync into `~/.engram/`) on a plugin install — it does not serve the plugin layout and will not update the running plugin.

---

## Relation to other skills

- **`tools/migrate-to-plugin.sh`** — the one-time scatter→plugin migration for any install still on the retired scatter layout. Different procedure; needs the human for the `/plugin install` + `/mcp` pauses. This skill takes over for all upgrades *after* that migration.
- **engram-self-improve** — a self-improvement code change ships through this skill once merged to the alpha source.
- **engram-surgical** — data-corruption recovery, NOT routine upgrades.
- **engram-nap / engram-sleep** — an upgrade is a meaningful state-change worth a nap checkpoint if context permits (Step 9's obs is the minimum record; a nap captures the wider session context).
