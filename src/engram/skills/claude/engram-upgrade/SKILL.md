---
name: engram-upgrade
description: Use when upgrading an existing plugin-mode ENGRAM install to newer alpha code while preserving the graph, history, diary, and sessions. Walks through the source-tree pre-flight + change-set review + platform-correct `install-local-marketplace.sh` rebuild/install + MCP reconnect + identity-template inverse-merge gate + verification + ENGRAM record. Each step is a verified checkpoint, not a context-impression. (Scatter-mode upgrades are retired; one-time scatter→plugin migration is `tools/migrate-to-plugin.sh`, not this skill.)
---

# ENGRAM Upgrade (Plugin) — Step-by-Step

Upgrading a plugin-mode ENGRAM install is mechanically simple but keeps one well-known failure mode: **trusting context-impression that a step is done** instead of verifying its completion checkpoint. This skill codifies each step + its verification so nothing is silently skipped.

**The architecture that makes plugin upgrades simple:** your *identity surfaces* (`~/.claude/CLAUDE.md`, warm-briefing) are **never touched** by a plugin upgrade. Your *data* (`~/.engram/knowledge.db`, history, diary, sessions) is preserved, but — as of #2170 — is **not guaranteed byte-identical**: the new `server.py` may run a one-shot schema migration on its first DB connection (gated by `PRAGMA user_version`, so each migration fires at most once) that rewrites rows in place. Concretely, #2170's rung 4 recomputes `claim` for every `feeling_report` node, drops and rebuilds the `nodes_fts` search index, and re-embeds every node in the graph. Before any such migration writes a single row, the server automatically takes a restore point at `~/.engram/db-backup/pre-migration-v<N>-<timestamp>.db` — alongside the pre-existing daily hot-copy ladder (`~/.engram/db-backup/knowledge-YYYYMMDD.db`, one per calendar day, 7-day window). See Step 5's restore procedure if anything looks wrong after reconnecting. The *code* (server, hooks, tools, skills, agents) lives in the plugin tree at `~/.engram/marketplace/plugins/engram/`. **For a Claude `directory`-source local marketplace — the standard plugin install — that source tree IS the live code**: `CLAUDE_PLUGIN_ROOT` resolves to it and Claude Code invokes hooks/skills/tools directly from it (verified 2026-06-28 by live hook-fire instrumentation). So once Step 3's `install-local-marketplace.sh` rebuilds the source tree, **new hook / tool / skill code is already live** (re-read per invocation) — there is no separate "pull into cache" step for them. The host plugin *cache* (`~/.claude/plugins/cache/...`) + `installed_plugins.json` are version bookkeeping for hooks/skills/tools — **but NOT for agents.** THREE code kinds need an explicit post-rebuild activation action (Step 4/5): (1) a changed **agent spec** (`agents/*.md`) — sub-agent/fairy definitions load from the plugin *cache* `installPath`, NOT the source tree, so they need a **cache refresh + `/reload-plugins`** (this is the #1639 fix — the mixed load-model is detailed in Step 4); (2) a changed **`server.py`** (the running MCP process is from session start → `/mcp` reconnect); (3) a changed **`hooks.json` registration** (read at session start → `/reload-plugins`, restart as fallback). **Codex differs:** `codex plugin add` copies the build into Codex's own cache (`~/.codex/plugins/cache/...`) — so the Codex flow is cache-refresh-based, and you use the installed cache for helper paths (e.g. `~/.codex/plugins/cache/.../tools/verify_quote.py`). For a session actually running **Codex 0.154.0+**, the activation behavior is no longer an inference: the official [0.154.0 release notes](https://github.com/openai/codex/releases/tag/rust-v0.154.0) state that existing sessions pick up newly installed plugin tools and refresh skills and hooks after external plugin upgrades or rollbacks. The narrower [openai/codex#42990](https://github.com/openai/codex/pull/42990) regression test specifically covers hook refresh at turn construction across an upgrade and rollback. On a managed seat, do not infer the running version from `codex --version` alone: a persistent app-server may still predate a newly installed CLI. Keep the boundary exact: this establishes in-session refresh for **tools, skills, and hooks**; it does **not** establish that an already-running MCP subprocess is replaced, nor that agent specs refresh. Older Codex versions retain the conservative restart/resume path. Note: **do not carry an answer about `~/.engram/hooks` and `~/.engram/tools` — carry a check.** Whether they are symlinks into the plugin tree or real directories depends on how your seat was installed. `bootstrap.py` creates those symlinks so scatter-era references to the paths keep resolving, and where they exist an upgrade does not touch them because it moves the *target*, not the link — which is exactly how a fix reaches them. But **bootstrap runs at bootstrap**: a seat that arrived by *migration* never executes it, so its `hooks` and `tools` are the pre-migration real directories that migration left in place — not a shadow that displaced a maintained symlink, because no symlink was ever maintained there. Either way that seat is pinned to whatever those directories last held (see #2557 / #2558). The trap is that on such a seat a flat "these are NOT symlinks" note reads as **accurate** — and that is the worse case, not the safer one: the reader checks, finds the note correct, and concludes the install is correctly shaped while it is in fact pinned. Where such a note is right, it is right for a reason unrelated to the install being right. **Check the state, not the history.** `test -L ~/.engram/tools` **and** `test -L ~/.engram/hooks` answer the only question that changes what you do, and they answer it directly. Check **both** — they are gated independently and can genuinely differ on one seat. Resist the urge to infer *whether bootstrap ever ran* from what is lying around — three successive revisions of this note got that inference wrong in three different ways, because the symlink step has more exits than it looks like: it is skipped **wholesale** when `ENGRAM_BOOTSTRAP_SKIP_SYMLINKS=1` (an intentional sandbox/benchmark mode, which gates both paths at once), and skipped **per-path** when the corresponding plugin directory is missing — `tools/` and `hooks/` are independently gated, so whichever plugin directory is missing, the other is still symlinked and the seat is left **mixed** (the same holds in either direction: a missing plugin `tools/` still symlinks `hooks/`, and a missing plugin `hooks/` still symlinks `tools/`). In every one of those cases the rest of bootstrap runs to completion while a pre-existing real directory is left untouched and no backup is written. The one direction that does hold, for either path: `~/.engram/tools.scatter-backup` **existing** proves the symlink step ran and displaced a real directory there, and `~/.engram/hooks.scatter-backup` proves the identical thing for `hooks` — `bootstrap.py` creates each backup with the same logic in its own path's branch, independently. Either backup's absence proves nothing at all about its own path. That check must be `test -L` and **not** an inode comparison: a symlink has its own inode, so `stat` without `-L` reports a different inode for a symlink and for an independent directory alike, which is true and cannot distinguish the two. (`stat -c` is GNU-only; it errors on macOS, where the form is `stat -f%i`. `test -L` is portable.) Note also that `test -L` on the directory is not the whole answer: a real directory can hold per-file symlinks into the plugin, so a seat can be partly current and partly pinned, and the two halves have to be checked separately.

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

> You're on the plugin install with an existing graph at `~/.engram/`. I can rebuild the local marketplace from newer alpha code — on Claude that source rebuild makes new hook/skill/tool code live immediately; **agent-spec changes additionally need a cache refresh (`/plugin marketplace update` + `/reload-plugins`), and `server.py` needs a `/mcp` reconnect** (all quick, at the end). On Codex it refreshes the Codex cache via `codex plugin add`. Your history, diary, and sessions are untouched; your graph is preserved but may be rewritten in place by an automatic, one-shot schema migration on first reconnect (a restore point is taken automatically first — see Step 5). Want me to proceed?

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
git tag --points-at HEAD | grep -E '^(v|deploy/)'   # confirm HEAD carries a deployable tag
```

The commit you install-local-marketplace.sh from must carry a deployable tag
(`v*` or `deploy/*` at HEAD) — the installer's #2586 tag guard refuses an
untagged HEAD by default. `--allow-untagged` exists for a deliberate exception
(e.g. certifying a merge commit that must itself be untagged) and leaves
`alpha_tag=none` in `.deployed-version`. This matters because the
health-snapshot versioning gate (#2563) treats the tag set as the deployable
baseline — a seat running from untagged code writes snapshots the gate cannot
see, which can orphan that seat's health history.

If the working tree has uncommitted changes you didn't mean to ship, stash or commit them first — the build packages whatever the tree is at.

**Checkpoint**: source-tree branch + **tagged** commit confirmed against the user's expectation (or the untagged exception named).

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

**Also note here** whether the CLAUDE.md render source appears in the change set — that pre-announces Step 7's gate. The rendered `~/.claude/CLAUDE.md` is assembled from a **single** upstream source: `templates/template.CLAUDE.md`. (`templates/compact-instructions.md` still ships but is Codex-only as of engram-alpha#2067 — it feeds `~/.engram/codex-compact-prompt.md` and `~/.codex/config.toml`'s `experimental_compact_prompt_file`, not the Claude render; see `docs/compaction-continuity.md`. On Codex, check it too — Step 7 below.) Check:

```bash
UPSTREAM_BRANCH="$(git -C "$SOURCE_DIR" rev-parse --abbrev-ref HEAD)"
git -C "$SOURCE_DIR" log --oneline "$DEPLOYED..origin/$UPSTREAM_BRANCH" -- src/engram/templates/template.CLAUDE.md
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

**Claude Code (directory-source install): run the full activation sequence — all three steps, in order.** Component types load by *different* mechanisms, and you don't always know exactly which a change touched, so the safe default is to run all three. (Verified 2026-07-02, #1639, by dispatching a fresh dream-fairy and confirming its ENGRAM MCP tools were present after the cache refresh.)

> **All three are Claude Code slash commands the USER runs — relay them, don't self-invoke.** `/plugin`, `/reload-plugins`, and `/mcp` are CC built-ins the agent cannot execute itself (see the Anti-patterns note at the end of this skill). Present the sequence to the user and confirm each is done, exactly as Step 5 already does for `/mcp`.

1. **`/plugin marketplace update engram-local`** — re-snapshots the plugin cache and repoints `installed_plugins.json` `installPath` to the new build. **REQUIRED for AGENTS:** sub-agent (fairy) definitions are registered from the cache `installPath`, **NOT** from the live source tree — the *opposite* of hooks/skills/tools. Without this, an agent-spec change (e.g. a fairy's `tools:` grant) silently never activates.
2. **`/reload-plugins`** — re-registers agents + hooks **in-session** from the refreshed cache. Verified **sufficient** for an agent-spec change; a full Claude Code restart is **not** required.
3. **`/mcp` → reconnect `engram`** (Step 5) — reloads the `server.py` **process** (pinned from session start; the lone component neither the cache refresh nor the reload picks up).

**Per-component load model (why the full sequence is the default):**

| Component | Load path | What activates a change |
|---|---|---|
| hooks / skills / tools | **source-live** — loaded from the marketplace *source* per invocation (`CLAUDE_PLUGIN_ROOT`=source). Hooks + skills are live-verified; tools follow the same mechanism but are inferred, not independently fired | live on the next prompt after Step 3's rebuild; the sequence is a harmless no-op for them |
| **agents (fairies)** | **cache-loaded** — registered from `installed_plugins.json` `installPath` (a cache snapshot), reload/restart-gated; live-verified #1639 | **steps 1 + 2 REQUIRED** |
| `server.py` (MCP) | **process-pinned** — running process from session start | **step 3 (`/mcp` reconnect) REQUIRED** |
| `hooks.json` registration | read at session start | `/reload-plugins` reloads hooks in-session (it reloaded 19 in the #1639 case); a full restart is the conservative fallback if a registration change doesn't take effect (not independently re-verified) |

> **Correction (2026-07-02, #1639):** a prior version of this step called the cache refresh "optional bookkeeping" and treated **all** of hooks/skills/tools/agents as source-live. That was **wrong for agents** — they are cache-loaded, so agent-spec fixes (e.g. the #1551 dual-prefix MCP grant) silently never activated on plugin installs, and dream-fairies ran with zero ENGRAM MCP access for cycles. The `/plugin marketplace update` + `/reload-plugins` steps are **required**, not optional, whenever a build may have touched an agent spec. hooks/skills/tools remain genuinely source-live — the correction is scoped to agents plus the "run the full sequence to be safe" default.

> **Why this still differs from the official-marketplace model:** for a `git`/`github`-source marketplace Claude Code loads *everything* from a cache copy, so `/plugin update` is the sole activation. Our local marketplace is `directory`-source, whose `installLocation` is the source dir — so hooks/skills/tools are source-live, but **agents are still cache-registered** and the MCP server process is still session-pinned. Hence the mixed model above. (The directory-vs-cache distinction is also the root of the v0.1.4 four-day silent-hook outage; see the project `CLAUDE.md` "Plugin deploy" section.)

**Codex:** `tools/install-local-marketplace.sh --target codex` runs `codex plugin add engram@engram-local` itself when `codex` is on PATH, copying the build into Codex's cache. When the session's running Codex runtime is **0.154.0+**, the next turn construction detects the external plugin change and refreshes the existing session's installed tools, skills, and hooks (the official [release-note guarantee](https://github.com/openai/codex/releases/tag/rust-v0.154.0); [openai/codex#42990](https://github.com/openai/codex/pull/42990) specifically regression-tests the hook-refresh subset at turn construction). A newer CLI binary beside an older persistent app-server does not satisfy that version gate. This is an activation guarantee for those three surfaces, not proof of the cache's internal load semantics, agent-spec refresh, or MCP-process replacement. Verify the cache actually changed as an install-integrity checkpoint:

```bash
sha1sum ~/.engram/marketplace/plugins/engram/hooks/engram-stop-hook.py
sha1sum ~/.codex/plugins/cache/engram-local/engram/0.1.0/hooks/engram-stop-hook.py
cat ~/.codex/plugins/cache/engram-local/engram/0.1.0/platform.json
```

**Checkpoint**: on Claude, the Step 3 rebuild succeeded (hooks/tools/skills are source-live), `/plugin marketplace update engram-local` + `/reload-plugins` ran (agents re-registered from the refreshed cache), and the `server.py` reconnect is deferred to Step 5; on Codex, cache/platform/hash verification shows the installed cache matches the marketplace, and a running 0.154.0+ runtime will refresh tools/skills/hooks at turn construction. If a needed agent refresh or MCP reconnect/restart (`server.py`) is deferred, the affected code is NOT yet live; track it so it doesn't fall off. **Verify agents specifically** if the build touched any `agents/*.md`: dispatch one and confirm it has the expected tools (e.g. a dream-fairy calling `engram_stats` — the #1639 probe), since the 0.154.0 guarantee does not mention agent specs and a stale-cache agent fails *silently*.

---

## Step 5 — Reconnect MCP (USER action)

The plugin tree/cache now has the new `server.py`, but the running MCP server may still be an old process. Tell the user:

> Please reconnect the engram MCP server: Claude — run `/mcp` and reconnect engram, or restart Claude Code. Codex — if `server.py` changed, restart/resume the Codex session so the process-pinned MCP server is replaced. On a running Codex 0.154.0+ runtime, tools, skills, and hooks refresh in the existing session without that restart; on older versions, restart/resume remains the conservative fallback for those surfaces too. Approve refreshed hook definitions if prompted.

**Do NOT** kill the MCP subprocess yourself — the stdio-MCP contract is user-restart only; killing it from an agent breaks the harness transport.

**Migration + restore procedure (#2181).** Reconnecting starts the new `server.py` process, and its first DB connection is where a pending schema migration (gated by `PRAGMA user_version`, so it fires at most once) actually runs — if Step 2's change set bumps that gate, this is when it happens, not at rebuild time in Step 3. Before any such migration writes a single row, the server automatically takes a binary restore point at `~/.engram/db-backup/pre-migration-v<N>-<timestamp>.db` (its own 3-copy rotation, separate from the daily `knowledge-YYYYMMDD.db` ladder). If the backup itself cannot be written (e.g. a full or read-only disk), the migration is skipped entirely and `PRAGMA user_version` is left unchanged, so it safely retries on the next reconnect — nothing is silently half-migrated.

If anything looks wrong after reconnecting (missing nodes, a query that used to work no longer does, `engram_stats` looks implausible in Step 8), **the order below matters — do not reorder it, and do not skip straight to the `cp`.** `knowledge.db` runs in WAL mode: recent writes live in a `knowledge.db-wal` sidecar beside the main file until a checkpoint folds them in, and any live connection can recreate that sidecar out from under a restore in progress. Copy the backup over `knowledge.db` while the server is still running (or while a stale `-wal` is still sitting next to it) and the next connection replays that `-wal` over the data you just restored — the restore silently comes half-undone, with no error anywhere. (This class of bug is easy to miss: a bare-file copy of a live SQLite DB can be hours stale and still open clean.)

1. **Stop the MCP server process — before touching any file.** Tell the user: "Something looks wrong post-migration; please quit or restart Claude Code (don't relaunch yet) so I can restore from backup." (Same **Do NOT kill the subprocess yourself** rule as above — stopping it is a user action, not an agent action.) This step is what makes the rest of the procedure safe: while the server is alive it holds an open WAL-mode connection to `knowledge.db`, and a clean stop checkpoints that connection's WAL and removes `knowledge.db-wal` for you — which is why step 2 below is normally a no-op. It is not optional cleanup; it is the step that prevents the replay described above.
2. **Check for leftover WAL/SHM sidecars, and remove them if present:** `ls ~/.engram/knowledge.db-wal ~/.engram/knowledge.db-shm 2>/dev/null`. After a clean stop in step 1 there normally are none — the checkpoint already removed them. If either file **is** still present, the previous shutdown was unclean, and this is exactly the case where restoring without removing them would replay stale writes over the restored data: `rm -f ~/.engram/knowledge.db-wal ~/.engram/knowledge.db-shm`.
3. Pick the restore point: the newest `~/.engram/db-backup/pre-migration-v*.db` if one exists (it is the copy taken immediately before this migration); otherwise the newest `~/.engram/db-backup/knowledge-YYYYMMDD.db`.
4. Copy it over the live DB, now that the server is stopped and no sidecar can reappear underneath it: `cp ~/.engram/db-backup/<chosen-file> ~/.engram/knowledge.db`.
5. Relaunch/reconnect the MCP server (`/mcp`, or however the user brought it back in step 1) and re-verify with `engram_stats` (Step 8).

**If Step 2's change set touched `hooks/hooks.json`, a `/mcp` reconnect is NOT enough** — `/mcp` reloads only the MCP server process, not the hook registration. Step 4's **`/reload-plugins`** re-registers hooks in-session (it reloaded 19 hooks in the #1639 case), so running the full Step-4 sequence should pick up a registration change; a **full Claude Code restart** is the conservative fallback if a dangling-file error (removed hook) or a silently-not-firing added hook persists after `/reload-plugins`. (The in-session `/reload-plugins` path for a *registration* change is plausible but not independently re-verified — restart if in doubt.) The `install-local-marketplace.sh` script will have printed an `⚠️  HOOK REGISTRATION CHANGED` banner if this applies.

**Checkpoint**: user reconnected (or explicitly deferred, noted as code-not-yet-running).

---

## Step 6 — Restart viz server (if installed as systemd service)

Skip if you don't use the viz dashboard (T2 feature, may not be installed).

The plugin rebuild (Step 3) replaced the `viz/` package (`viz/server.py` and its siblings) in the plugin tree, but any running viz service still executes the **old code**. Without a restart the health dashboard silently serves stale content — it appears healthy but reports on the pre-upgrade version.

**macOS note:** systemd is Linux-only. On macOS, skip to the manual-start path below. launchd integration is a known gap (see #402); it is not handled here.

`tools/operator-setup-viz.sh` is the canonical path (idempotent; its last step
is this same restart) — use the bare restart below only for a code-only
refresh where setup is already known-good.

```bash
# Check if a systemd service is registered (Linux only):
systemctl --user status engram-viz-user.service 2>/dev/null || echo "no systemd service"

# If registered, restart it:
systemctl --user restart engram-viz-user.service && echo "viz restarted" || echo "viz restart failed — check journalctl --user -u engram-viz-user.service"
```

**If not a systemd service** (manual-start install, or macOS): the old process is still running. Tell the user:
> The viz server is serving pre-upgrade code. Kill the old process and restart:
> `pkill -f viz.server; cd ${CLAUDE_PLUGIN_ROOT:-~/.engram/marketplace/plugins/engram} && python3 -m viz.server &`

Verify the server is live with new code after the restart:
```bash
curl -s http://localhost:5001/api/health | python3 -m json.tool
```
A `health_score` field in the response confirms the viz server is up. Step 8 also checks this.

**Checkpoint**: viz responds to `http://localhost:5001/api/health` (or the service is confirmed not installed).

---

## Step 7 — CLAUDE.md template inverse-merge (HARD GATE, the residual)

If Step 2 found the CLAUDE.md render source (`templates/template.CLAUDE.md`) changed in the range, **do not skip this**. The rendered `~/.claude/CLAUDE.md` is assembled from that single upstream source now (engram-alpha#2067 — `templates/compact-instructions.md` no longer feeds it; see `docs/compaction-continuity.md`). On Codex, also check `templates/compact-instructions.md` (now Codex-only — feeds `codex-compact-prompt.md` / `experimental_compact_prompt_file`), `templates/template.AGENTS.md`, and any local first-person instructions in `~/.codex/config.toml`/project `AGENTS.md`. The plugin upgrade did not touch these user-scope identity surfaces, so source-side additions may NOT have reached the running agent.

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

**What you do NOT need anymore (scatter-era steps, now plugin-owned):** no manual hook merge (the plugin's `hooks.json` registers hooks), no manual `cp` of skills/agents (they live in the plugin source tree). Note the activation asymmetry (Step 4): **skills** go live with the Step 3 rebuild (source-live), but **agents** additionally need the cache refresh + `/reload-plugins` (they load from the cache, not source — #1639); on Codex both come via the cache refresh.

**Checkpoint**: template changes inverse-merged + verified, OR explicitly deferred with the user's agreement AND tracked in the ask-user queue.

---

## Step 8 — Verify

```bash
cat ~/.engram/.deployed-version          # marker SHA = the commit you built (forensics anchor)
rm -f ~/.engram/.upgrade-pre-sha         # pre-upgrade SHA scratch file no longer needed
```

Then through the **plugin MCP** (after Step 5's reconnect): call `engram_stats` and confirm it returns your real node count. **Read carefully what that does and does not establish — the obvious reading is wrong, and it is the reading this step used to endorse.**

**It establishes:** the DB is readable, and *some* server answered (your graph is preserved across the upgrade, though possibly migrated in place per Step 5). If `engram_stats` errors or the node count is implausible, stop and investigate before declaring the upgrade done — Step 5's restore procedure is the recovery path.

**It does NOT establish that the new server is running.** A node count cannot discriminate a reconnected process from one that never restarted. And the `version` block in the same response is *worse* than merely silent about it: the `version` section of `engram_stats.py` does a `read_text` on `~/.engram/.deployed-version` **inside the request handler**, at call time, with no caching — and Step 3 rewrote that marker. **So a server that never reconnected reports the new `alpha_sha` identically.** (Finding: Mira, 2026-09-07, from correcting an over-confident upgrade certification; re-verified against the source 2026-09-10.)

### The check that DOES discriminate

Run this in addition, not instead. Paste it whole — the guards are the point, not decoration.

```bash
# Anchor on the interpreter as argv[0]. A bare 'plugins/engram/server.py'
# pattern ALSO matches the shell you are running this in, whose own command
# line contains that string -- so the naive form returns two pids, one of
# which then exits.
#
# Each piece guards a DIFFERENT case; all three are load-bearing, and the
# two anchors are not interchangeable (#2797 round 2 shipped one without
# the other and a bare `ls` read as a healthy server):
#   ^[^ ]*    the interpreter must be argv[0]. [^ ]* cannot cross a space,
#             so a python PATH appearing as a later ARGUMENT -- `ls -la
#             /usr/bin/python3 .../server.py`, an editor, strace, any
#             wrapper naming both -- cannot match. Dropping this in favour
#             of a (^|/) alternation is the regression referred to above:
#             (^|/) matches a python token after ANY slash, anywhere in the
#             command line.
#   [Pp]      a Homebrew Framework build's argv[0] basename is 'Python'.
#   [^/ ]*    covers every real suffix -- '3.12', a pyenv shim, '3.13t'
#             (free-threaded), '3.12-dbg' -- while EXCLUDING '/' is what
#             stops the suffix running past the basename, so
#             /opt/python-tools/bin/foo (a non-Python binary in a
#             python-named DIRECTORY) does not match.
# NOT covered, deliberately -- and this list is the WHOLE of it, because
# "not covered: pypy3" alone would imply everything else is:
#   (a) pypy3 and other non-CPython interpreter names. ENGRAM's venv is
#       CPython and the server needs native wheels.
#   (b) any WRAPPER-launched server -- sudo/env/nohup/timeout -- since the
#       wrapper, not the interpreter, is then argv[0]. Verified: all four
#       fail to match. This is safe today because the real launcher execs
#       the interpreter directly (launch-engram-server.sh:12,
#       `exec "$VENV_PY" "$SCRIPT_DIR/server.py" "$@"`), so argv[0] IS the
#       interpreter in deployment.
# Both misses fail in the SAME safe direction: "no server" -> exit 1, a
# loud false alarm, never a silent pass. Widen if either premise changes.
PAT='^[^ ]*[Pp]ython[^/ ]* .*plugins/engram/server\.py'
P=$(pgrep -u "$USER" -f "$PAT")
N=$(printf '%s\n' "$P" | grep -c .)
[ "$N" = 1 ] || { echo "FAIL: expected 1 server, found $N"; exit 1; }

NOW=$(date -u +%s); ET=$(ps -o etimes= -p "$P" | tr -d ' ')      # Linux; see macOS note
[ -n "$ET" ] || { echo "FAIL: ps returned nothing for pid $P"; exit 1; }
echo "server born at epoch $((NOW-ET)) -- must be AFTER your /mcp reconnect"

ARGV=$(ps -p "$P" -o command=)
[ -n "$ARGV" ] || { echo "FAIL: empty argv -- the checks below would report PASS without running"; exit 1; }
echo "$ARGV" | grep -q 'plugins/engram' || { echo "FAIL control: pattern not live"; exit 1; }
echo "$ARGV" | grep -q 'plugins/cache' && { echo "FAIL: server is running from the plugin CACHE, not the marketplace source"; exit 1; }
echo "PASS: 1 server, born after the epoch above, argv = marketplace source"
```

**Why every guard is there — each one is a failure this block was observed to produce before they were added** (measured 2026-09-10 while writing this step, by running it rather than reasoning about it):

- **The pid-count assertion.** `pgrep -f` self-matched the invoking shell and returned two pids; `ps -p "6507 23770"` then errored.
- **The empty-`ARGV` assertion, which is the important one.** With `ps` erroring, `grep -c 'plugins/cache'` on the empty stream returned **`0` — which is the PASS value.** The block reported a clean seat from a check that never ran. A step written to fix a false-verification problem had a false-verification bug of exactly the same shape.
- **The `control` line** is what caught it: it read `0` where it must read `1`. Keep it. A zero from an inert pattern is indistinguishable from a zero from a healthy seat, and only the must-be-present control separates them.

Three directions were exercised, on the block as extracted from this file rather than a copy:

```
healthy seat                  -> exit 0,  "PASS: 1 server, born after the epoch above, argv = marketplace source"
no such server                -> exit 1,  "FAIL: expected 1 server, found 0"
argv matching the plugin cache-> exit 1,  "FAIL: server is running from the plugin CACHE, not the marketplace source"
```

A check never observed to fail is not yet a check.

**macOS:** `etimes` is Linux-only (procps), so the born-at-epoch line does not port — and it does not merely *invert*, it **breaks**. `ps -o etime=` renders `[[dd-]hh:]mm:ss`, not an integer, so `$((NOW-ET))` is a hard bash syntax error (measured on this seat: `ET=24:02` → `syntax error in expression (error token is ":02")`). Do not swap `etimes`→`etime` and keep the arithmetic.

Instead, on Darwin, drop the epoch line and read the elapsed string directly — the **direction inverts**, so you are checking it is *smaller* than the time since your reconnect, not that a start time is late:

```bash
ps -p "$P" -o etime=    # e.g. "24:02" = 24m02s ago. Must be LESS than time since your /mcp reconnect.
```

Every other line — the anchored `pgrep`, the pid-count assert, the `ARGV` non-empty assert, the control, and the cache gate — ports unchanged. The `pgrep` pattern needed widening to get there (#2797) and now covers Homebrew Framework `Python` as well as versioned interpreters; see the bound below for what remains unexercised on BSD.

**The Darwin path is partly tested now, and the part that was tested came back FALSE — which is worth more than the hedge was.** Original bound: two assertions rode together, (1) the `etime` rendering, measured on Linux where `ps` is procps rather than BSD, and (2) the ports-unchanged claim, unexercised on BSD entirely.

A colleague ran the block on a real Mac and **falsified (2)** — the `pgrep` pattern silently matched nothing against a Homebrew Framework `Python`, reported as #2797 and fixed above. Note where the doubt was mis-aimed: the hedge scoped its uncertainty to *Darwin*, and measuring the class afterwards showed the pattern also missed `python3.12` and pyenv shims, which are **ordinary Linux**. So the marker protected the platform that could not be tested while the pattern was already broken on the one that could. Five argv shapes cost nothing to check and only one had been checked — my own.

**Still unexercised on BSD:** the `etime` rendering itself, and whether `ps -o command=` and the guards behave identically under BSD `ps`. The pattern fix was verified by matching, not by running on Darwin. Confirm or correct here rather than assume.

⚠ **Use `etimes`/`etime`, never `ps -o lstart`.** `lstart` prints **local** time with no timezone token, so whatever you hand it re-parses it in the wrong zone silently — including a real converter: `date -u -d "$(ps -o lstart= ...)"` applies a *zero* shift, because `date -d` reads the timezone-less string as UTC. That has produced wrong published times on this project repeatedly, once defeating an otherwise-correct converter. `etimes` is an integer of seconds and cannot fail this way.

On Codex, also verify installed-cache files match the marketplace if hooks are the upgrade target.

**For a thorough operational health check** (recommended after any upgrade): load and run the `engram-health-exam` skill. It covers the full runtime layer — hook delivery, surface daemon, viz server, config sanity, schema/migration — beyond the single `engram_stats` probe above. The `engram_stats` call here is the minimum gate; `engram-health-exam` is the full exam.

**Checkpoint**: marker SHA matches the intended commit + `engram_stats` returns the expected graph + **the discriminating block above exits 0** (one server; argv is the marketplace source, not the cache — both hard-gated) + **you read the born-at epoch yourself and it is after your reconnect** + `.upgrade-pre-sha` scratch file removed.

Note which half is mechanical and which is yours: the block *enforces* the pid count, the non-empty reads, the control, and the cache check — any of those failing exits 1. It **cannot** enforce the birth time, because only you know when you ran the reconnect; it prints the epoch and you compare. Do not read `exit 0` as covering that one. The `engram_stats` call alone establishes none of it.

---

## Step 9 — Record the upgrade in ENGRAM

```python
engram_add_observation(payload_json=json.dumps({
    "claim": "ENGRAM plugin upgrade <YYYY-MM-DD HH:MM TZ>: <host target> marketplace source rebuilt at <commit SHA> (<branch>, tag <alpha_tag>) — hooks/skills/tools live from source (Claude) / cache refreshed (Codex). MCP reconnected: <done|deferred|n/a>. Hooks.json restart: <done|deferred|n/a>. Identity-template gate: <merged paths | 'no template changes' | 'deferred+tracked'>. engram_stats verified: <N> nodes.",
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
