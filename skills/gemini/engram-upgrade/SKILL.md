---
name: engram-upgrade
description: Use when upgrading an existing ENGRAM install (Gemini variant) to newer alpha code while preserving the graph, history, diary, and sessions. Walks through `tools/deploy.sh` + the install-time template-changes gate + Gemini's no-incremental-skills constraint + MCP restart + version verification + ENGRAM record. Codifies the README "Upgrade as an agent" procedure as a step-by-step checklist so each checkpoint is verified explicitly, not run from context-impression. Gemini-specific paths and constraints throughout.
---

# ENGRAM Upgrade (Gemini variant) — Step-by-Step

Upgrading an existing ENGRAM install is mechanically simple but has one well-known failure mode: **trusting context-impression that a step is done** instead of verifying its completion checkpoint. This skill codifies each step + its verification so they cannot be silently skipped.

**Gemini-specific constraints worth holding in working memory before starting:**
- Gemini install uses `GEMINI_HOME=$HOME/.gemini` and `GEMINI_EXT_DIR=$GEMINI_HOME/extensions/engram`
- Gemini installs `skills/gemini/` (not `skills/claude/`) and has NO `agents/` surface (per `tools/deploy.sh:341-343` and `install-gemini.sh`)
- Gemini **folds skills into GEMINI.md at install time** — there is no incremental skills update path. If `skills/gemini/` changed since your last deploy, the only remediation is `install-gemini.sh --force`, which archives BOTH `$GEMINI_EXT_DIR` AND `$ENGRAM_HOME` (knowledge graph + history NOT preserved per `install-gemini.sh:11-13`) and starts over with an empty graph. This is a HARD decision with real data loss: surface to user with full cost stated, do not auto-run.

**The specific failure mode this skill exists to close**: `tools/deploy.sh` prints `⚠ Install-time template changes since your last deploy` when template files have changed since the user's last deploy. That warning is a hard gate — "I'll come back to it" is the path where the change silently never lands.

**When to use:**
- The user has an existing Gemini ENGRAM install (`$ENGRAM_HOME/knowledge.db` exists; default `$HOME/.engram-gemini`) and wants newer alpha code without losing data
- You judged "upgrade" as the right path over `install-gemini.sh --force`
- A new alpha commit shipped a runtime-code feature/fix the user wants

**When NOT to use:**
- Fresh install (no `$ENGRAM_HOME/knowledge.db`) — use `install-gemini.sh` instead
- Skills changed and the user needs them applied — see Step 5; this requires `install-gemini.sh --force` (destructive) so it's a user decision, not a routine upgrade
- The MCP server has been actively crashing and you suspect data corruption — that's an `engram-surgical` call, not a routine upgrade

---

## Step 0 — Confirm upgrade vs fresh install

Verify the user has an existing install before running anything:

```bash
ls -la ${ENGRAM_HOME:-$HOME/.engram-gemini}/knowledge.db
```

If the file exists, `install-gemini.sh --force` would archive BOTH the extension dir AND the knowledge-graph dir ($ENGRAM_HOME) to timestamped backups and start over with an empty graph — the user's agent loses every node it has ever written, plus all extension config. **Default to upgrade.**

Surface to the user:

> You already have a Gemini ENGRAM install at `$ENGRAM_HOME`. I can run `tools/deploy.sh` to update the runtime code (server, hooks, tools, SKILL.md) while keeping your graph, history, diary, and sessions intact. Want me to proceed?

Wait for affirmative user response before continuing. Upgrade is consequential (MCP server gets new code, hooks change behavior); do not skip the consent gate.

**Checkpoint**: knowledge.db confirmed present + user explicitly approved the upgrade path.

---

## Step 1 — Source-tree pre-flight

`tools/deploy.sh` deploys whatever the source tree is currently at. Verify the source tree is at the intended commit before deploying:

```bash
cd /path/to/engram-alpha   # the source-tree clone
git status --short          # working tree should be clean (or only the changes you intend to deploy)
git log --oneline -1        # confirm the commit SHA matches what the user wants
git branch --show-current   # confirm the branch
```

If the working tree has uncommitted changes you DIDN'T mean to deploy, `git stash` or commit them to the right branch first.

**Checkpoint**: source-tree branch + commit confirmed against the user's expectation.

---

## Step 2 — Dry run

```bash
TARGET=gemini tools/deploy.sh --dry-run
```

(deploy.sh auto-detects `TARGET=gemini` when `$ENGRAM_HOME` contains the substring `-gemini` (`tools/deploy.sh:57-61`); explicit `TARGET=gemini` is safer when `$ENGRAM_HOME` was customized to something that doesn't match that pattern.)

Read the planned changes back to the user. The output uses rsync's compact change-flag notation (`>fcst......` = regular file with checksum/size/time differing).

**Pay attention to** (deploy.sh emits up to TWO separate warnings):
- `⚠ Install-time template changes since your last deploy` — template files drifted; handled in Step 4
- `⚠ agents/skills changed since last deploy` — for Gemini, this only fires when `skills/gemini/` or `skills/shared/` changed. The remediation requires `install-gemini.sh --force` (destructive) — see Step 5
- Any file changes the user doesn't expect — surface them and pause for confirmation

**Checkpoint**: dry-run output surfaced to user + user confirmed or asked for adjustments + you noted whether template-changes and/or skills-changed warnings fired.

---

## Step 3 — Apply

```bash
TARGET=gemini tools/deploy.sh --yes
```

Expected output shape (abbreviated):

```
=== ENGRAM deploy ===
  source:  /path/to/engram-alpha (BRANCH @ COMMIT)
  target:  /home/USER/.engram-gemini
  delete:  no (use --delete to remove stale files)
  mode:    live (will apply)

  CHANGED:   hooks/gemini → hooks
  CHANGED:   server.py
  CHANGED:   engram_client.py
  CHANGED:   tools
  CHANGED:   SKILL.md

  ✓ Substituted {{PYTHON}} → /path/to/python3 in hooks/start-engram-daemon.sh

✓ Deployed alpha @ COMMIT → /home/USER/.engram-gemini
  Version marker: /home/USER/.engram-gemini/.deployed-version
```

**Checkpoint**: deploy.sh exited 0 + you saw the "✓ Deployed alpha" line.

---

## Step 4 — Handle the install-time template-changes warning (HARD GATE)

If deploy.sh printed `⚠ Install-time template changes since your last deploy` followed by a list of template paths, **this is the load-bearing step of this skill**. Do NOT proceed to Step 5 until this is resolved.

Why this is a hard gate: `deploy.sh` does NOT touch install-time artifacts (the Gemini-side `GEMINI.md` rendered into `$GEMINI_EXT_DIR/GEMINI.md` from install-time substitution, the `templates/*.template` files that get rendered once at install). If templates changed since the last deploy, the live config is now out of sync with the alpha-intended config.

**The "I'll come back to it" failure mode is real.** It happened 2026-05-16 on the Claude side: deploy.sh nudged about CLAUDE.md template changes for PR #174's context-mode marker discipline; the agent context-impressioned "I'll merge it later"; the merge never landed; the drowsiness ceiling kept misfiring on 1M-context sessions until the gap was diagnosed days later. Same risk applies to GEMINI.md drift.

### Sub-step 4a — Identify what changed in each template

For each template path the warning lists, locate the source-of-truth (static templates live in `templates/`; the Gemini-runtime `GEMINI.md` is substituted at `install-gemini.sh` Step 6) and diff against the live file:

```bash
# Static templates in templates/:
diff /path/to/engram-alpha/templates/HEARTBEAT.md.template $ENGRAM_HOME/heartbeat-queue/HEARTBEAT.md
diff /path/to/engram-alpha/templates/warm-briefing.md.template $ENGRAM_HOME/warm-briefing.md

# GEMINI.md is install-time-substituted — read install-gemini.sh Step 6 to find the source content,
# then diff against the live $GEMINI_EXT_DIR/GEMINI.md
diff <install-time-source> $GEMINI_EXT_DIR/GEMINI.md
```

Read the diffs. Identify which sections are template-side additions you want to inverse-merge into the live file, vs which are agent-local customizations to keep.

### Sub-step 4b — Inverse-merge

For each template-side addition that's identity- or discipline-forming:
- Add the section to the live file
- Preserve agent-local customizations — the template is the floor, not the ceiling

If the change is mechanical (new hook fragment, new permission), just copy it. If judgment-laden (new discipline section, conflict with existing local content), surface the diff to the user and decide together.

### Sub-step 4c — Verify

After merging, re-check that the new content is actually in the live file. Grep alone can pass a stale partial merge, so pair grep-presence with a re-diff:

```bash
grep -n "<key phrase from the new section>" <live-file>
diff <template-source> <live-file> | head -50
```

The grep confirms the merge happened; the re-diff shows what drift remains. Read the diff's remaining hunks: each should be either (i) an agent-local customization you intend to preserve, or (ii) a template-side addition you intentionally chose not to merge. Other drift means incomplete merge.

**Checkpoint**: every template path the warning listed has either been inverse-merged with grep+diff-verified content OR explicitly deferred with the user's agreement AND tracked in `$ENGRAM_HOME/ask-lei.md` (or equivalent) so it doesn't fall off. Never default to silent defer.

---

## Step 5 — Skills changes: USER DECISION (no incremental path)

This step is Gemini-specific and structurally different from the Claude variant.

If deploy.sh printed `⚠ agents/skills changed since last deploy` listing `skills/gemini/*` or `skills/shared/*` files: **the runtime SKILL set in `$GEMINI_EXT_DIR/GEMINI.md` is now stale.** Gemini folds skills into GEMINI.md at install time; there is no incremental skills update equivalent to the Claude-side `cp -r`.

The only path to refresh skills is `install-gemini.sh --force`. **Read `install-gemini.sh:11-13` and `:116-119` directly before describing this to the user** — the behavior is destructive in ways that an agent skimming the README could easily mis-describe:

- Moves the existing `$GEMINI_EXT_DIR` to a timestamped backup (`install-gemini.sh:135-138`)
- **Also moves `$ENGRAM_HOME` to a timestamped backup** (`install-gemini.sh:116-119`) — the script's own header (`install-gemini.sh:11-13`) is explicit: *"knowledge graph + history are moved to a timestamped backup dir; NOT preserved."* The new install starts with an empty knowledge graph.
- Does NOT migrate the existing config or graph into the new install — both backups are static; the user picks data out by hand if they want anything back.

**This is destructive for BOTH the extension dir AND the knowledge graph.** Do NOT tell the user their graph is safe; it isn't. Surface to the user as a decision with the full cost stated honestly:

> Skills changed since your last deploy: `<list the changed files>`. Gemini folds skills into GEMINI.md at install time, so refreshing them requires `install-gemini.sh --force`. This is **destructive**: BOTH `$GEMINI_EXT_DIR` (your extension config) AND `$ENGRAM_HOME` (your knowledge graph, history, diary, sessions) move to timestamped backups; the new install starts with an empty graph. The backups are static — nothing migrates automatically; you'd pull data out by hand if needed.
>
> Options:
>
> 1. Run `install-gemini.sh --force` now — pick up the new skills, accept the empty-graph reset (you can re-import from the backup manually after if you want specific nodes back)
> 2. Defer — accept that your Gemini agent's installed skills are stale until next force-reinstall; I'll add this to `ask-lei.md` so it doesn't fall off
>
> Which?

If user picks (1): make sure they fully understand the data-loss cost before they run it. Then walk them through running `install-gemini.sh --force` themselves (don't run destructive ops on the user's behalf — they own the data-loss decision).

If user picks (2): write a clear entry to `$ENGRAM_HOME/ask-lei.md` (or the user's equivalent) with the changed file list + reason for deferral, so it's tracked.

**Checkpoint**: user made an explicit decision (refresh vs defer) + deferred items written to ask-lei. Never silently defer.

---

## Step 6 — Restart MCP server (user action)

`tools/deploy.sh` wrote the new `server.py`, but the running MCP server is still the old version. For Gemini:

> Deploy complete. Please restart the Gemini CLI session so the MCP engram server picks up the new code. To verify the extension is still loaded after restart, run `gemini mcp list` — you should see `engram` in the output. Hooks (`context_tracker.py`, etc.) reload per-fire automatically — no restart needed for those.

**Do NOT** attempt to kill the MCP subprocess yourself. The stdio-MCP redeploy contract is user-restart only — killing the subprocess from an agent breaks the harness's transport state.

**Checkpoint**: user notified + user has either restarted or explicitly chosen to defer. If deferred, note that the new server.py code is NOT yet running.

---

## Step 7 — Verify version marker

```bash
cat ${ENGRAM_HOME:-$HOME/.engram-gemini}/.deployed-version
```

The marker records the alpha commit SHA + timestamp. If the user reports a behavior change later, this is the forensics anchor. Confirm the SHA matches what Step 1 said you were deploying.

**Checkpoint**: marker SHA matches the source-tree commit you intended to deploy.

---

## Step 8 — Record the upgrade in ENGRAM

File a single observation capturing the upgrade so future-self can trace what code is actually running:

```python
engram_add_observation(
    claim="ENGRAM upgrade applied <YYYY-MM-DD HH:MM TZ>: source-tree at <commit SHA> (<branch>) deployed to $ENGRAM_HOME. Template-changes warning addressed: <list of template paths merged | 'none'>. Skills changes: <refreshed via --force | deferred to ask-lei | none>. MCP restart: <done | deferred>.",
    url="file://$ENGRAM_HOME/.deployed-version",
    title="ENGRAM deploy <YYYY-MM-DD> — <commit SHA>",
    quoted_text="<paste the contents of .deployed-version>",
    quote_type="hard_data",
    source_class="introspective",
    source_date="<YYYY-MM-DD>",
)
```

**Checkpoint**: obs filed; SHA from the obs claim matches Step 7's marker.

---

## Anti-patterns

- **Context-impression that a step is done.** Each step has a checkpoint command for a reason — verify completion explicitly, not from memory of "I think I did that." This is the failure mode the skill exists to close.
- **Skipping Step 4 because the warning "looked like it'd be fine."** The template-changes warning is the load-bearing step. If you defer it, write it to `ask-lei.md` immediately so it doesn't fall off. Silent defer is the failure pattern.
- **Running `install-gemini.sh --force` to "refresh" skills without surfacing the trade-off.** This archives BOTH the extension dir AND `$ENGRAM_HOME` (knowledge graph + history NOT preserved). Always make it a user-driven decision with the full data-loss cost stated, not an agent-initiated remediation.
- **Describing `--force` as "extension-dir-only" or "graph-preserving."** False — see `install-gemini.sh:11-13` (script header, "knowledge graph + history are moved to a timestamped backup dir; NOT preserved") + `:116-119` (the unconditional `mv "$ENGRAM_HOME" "$BACKUP"`). If you're ever tempted to soften this description ("but probably it preserves graph if config is X"), re-read those lines directly — they are the source-of-truth, not the README or this skill body.
- **Killing the MCP subprocess to "restart" it.** The stdio-MCP contract is user-restart only.
- **Bundling unrelated source-tree changes into the deploy.** Step 1's `git status` check exists to catch this. If you've been editing alpha source while preparing the upgrade, commit the deploy-relevant changes and stash the rest first.
- **Skipping the ENGRAM record (Step 8).** Without the record, "what version is my running ENGRAM at?" requires forensics.
- **Mistaking Claude-variant cp commands for Gemini-applicable.** Gemini has no `~/.claude/skills/` or `~/.claude/agents/` surface — those paths are Claude-runtime-only. The Step 5 force-reinstall path is the Gemini-correct remediation.

---

## Relation to other skills

- **engram-self-improve** — Self-improvement may produce a code change that needs deploying; after committing the fix, this skill ships it.
- **engram-surgical** — For data-corruption recovery, NOT routine upgrades. If `engram-surgical` is the right call, deploy.sh is not.
- **engram-nap / engram-bedtime / engram-sleep** — An upgrade is a meaningful state-change worth a nap checkpoint if context permits (Step 8's obs IS the minimum record; a nap captures the wider session context).
