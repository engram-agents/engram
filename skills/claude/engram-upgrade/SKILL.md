---
name: engram-upgrade
description: Use when upgrading an existing ENGRAM install to newer alpha code while preserving the graph, history, diary, and sessions. Walks through `tools/deploy.sh` + the install-time template-changes gate + the cross-surface skills/agents copy + MCP restart + version verification + ENGRAM record. Codifies the README "Upgrade as an agent" procedure as a step-by-step checklist so each checkpoint is verified explicitly, not run from context-impression.
---

# ENGRAM Upgrade — Step-by-Step

Upgrading an existing ENGRAM install is mechanically simple but has one well-known failure mode: **trusting context-impression that a step is done** instead of verifying its completion checkpoint. This skill codifies each step + its verification so they cannot be silently skipped.

**The specific failure mode this skill exists to close**: `tools/deploy.sh` prints `⚠ Install-time template changes since your last deploy` when CLAUDE.md.template or settings.json hook fragments have changed since the user's last deploy. That warning is a hard gate — inverse-merging template changes into the live config is judgment work, and "I'll come back to it" is the path where the change silently never lands. The discipline below treats every step as a checkpoint.

**When to use:**
- The user has an existing ENGRAM install (`~/.engram/knowledge.db` exists) and wants newer alpha code without losing data
- You (the agent) judged "upgrade" as the right path over `install.sh --force`
- A new alpha commit shipped a feature/fix the user wants

**When NOT to use:**
- Fresh install (no `~/.engram/knowledge.db`) — use `install.sh` instead
- Skills- or agents-only update (no code changes in the alpha source) — Step 6 alone is sufficient; you don't need the full skill
- The MCP server has been actively crashing and you suspect data corruption — that's an `engram-surgical` call, not a routine upgrade

---

## Step 0 — Confirm upgrade vs fresh install

Verify the user has an existing install before running anything:

```bash
ls -la ~/.engram/knowledge.db
```

If the file exists, `install.sh --force` would archive the entire graph to a timestamped backup and start over — the user's agent loses every node it has ever written. **Default to upgrade.**

Surface to the user:

> You already have an ENGRAM install at `~/.engram/`. I can run `tools/deploy.sh` to update the running code (server, hooks, tools, SKILL.md) while keeping your graph, history, diary, and sessions intact. Want me to proceed?

Wait for affirmative user response before continuing to Step 1. Upgrade is consequential (MCP server gets new code, hooks change behavior); do not skip the consent gate.

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
tools/deploy.sh --dry-run
```

Read the planned changes back to the user. The output uses rsync's compact change-flag notation (`>fcst......` = regular file with checksum/size/time differing). The user should see what files would change before any change is applied.

**Pay attention to** (deploy.sh emits up to TWO separate warnings):
- `⚠ Install-time template changes since your last deploy` — template files (CLAUDE.md.template, settings.hooks.json.template, etc.) drifted; handled in Step 5 via inverse-merge
- `⚠ agents/skills changed since last deploy` — agents/ and skills/ trees drifted; deploy.sh prints the EXACT cp commands to copy. Save these — they're the canonical source for Step 6 (don't re-derive them manually)
- Any file changes the user doesn't expect — surface them and pause for confirmation

**Checkpoint**: dry-run output surfaced to user + user has either confirmed or asked for adjustments + any printed cp commands captured for Step 6.

---

## Step 3 — Summarize what's about to land (pre-deploy review)

Before running `tools/deploy.sh`, read the change set so you understand what
you're applying. This runs BEFORE the deploy so you can flag concerns and abort
before any mutation.

1. Read the currently-deployed sha:

   ```bash
   DEPLOYED=$(grep '^alpha_sha=' ~/.engram/.deployed-version | cut -d= -f2)
   ```

   If `.deployed-version` is missing or the grep returns empty, fall back to
   comparing the alpha repo's checked-out branch HEAD vs `origin/dev`. Note
   the caveat to the user — the range may overcount if the local clone is
   ahead of what's actually deployed.

2. Fetch the latest remote state:

   ```bash
   git -C /path/to/engram-alpha fetch origin
   ```

3. Get the PR-merge commit list between deployed and remote tip:

   ```bash
   git -C /path/to/engram-alpha log --oneline "$DEPLOYED..origin/dev"
   ```

   Each line shows: `<short-sha> <PR title> (#<PR number>)`. The PR number
   at the end of each title is your handle for any follow-up `gh pr view`
   if you need depth on a specific change — do NOT loop over all PRs to
   fetch bodies (token budget). You may run `gh pr view <N>` on at most ONE PR in this step. If you're considering a second, surface the summary as-is and proceed to Step 4.

   **Edge cases:**
   - Empty output means `$DEPLOYED == origin/dev` tip — no remote changes
     exist. If the output is empty, surface to the user: 'No remote changes — deployed SHA matches origin/dev tip; nothing to upgrade.' Then exit the upgrade flow.
   - If the range exceeds 50 PRs, cap the log with `-n 20` and append a
     `"+ N older"` note. Group-summarize rather than enumerate.

4. If the count is 20 or fewer, list each PR with its short title. If
   the count exceeds 20, group by theme (e.g., "12 stats/calibration PRs
   in the confidence-injection cohort", "3 viz refactors", "5 other").

5. Write a concise summary (1 short paragraph, max 4 bullets) of the
   change set's shape. What surfaces are touched? Any deploy-fragile
   areas (`server.py` / hooks / migrations)? Any user-facing changes worth
   flagging? Keep it tight — this is for the user to read in 30 seconds.
   Per-upgrade input budget target: < 1500 tokens; output target: < 500 tokens.

6. Surface the summary to the user. If anything looks concerning — a PR
   touches something actively in-flight, a migration appears in the list,
   anything you don't recognize — pause and discuss before proceeding to
   Step 4.

If you want depth on a specific PR after seeing its title, run
`gh pr view <N>` on that one PR explicitly. Do NOT loop over all PRs to
fetch bodies.

**Checkpoint**: change-set summary surfaced to user + user has either confirmed
"proceed" or raised a concern to resolve first.

---

## Step 4 — Apply

```bash
tools/deploy.sh --yes
```

Expected output shape (abbreviated):

```
=== ENGRAM deploy ===
  source:  /path/to/engram-alpha (BRANCH @ COMMIT)
  target:  /home/USER/.engram
  delete:  no (use --delete to remove stale files)
  mode:    live (will apply)

  CHANGED:   hooks/claude → hooks
  CHANGED:   server.py
  CHANGED:   engram_client.py
  CHANGED:   tools
  CHANGED:   SKILL.md

  ✓ Substituted {{PYTHON}} → /path/to/python3 in hooks/start-engram-daemon.sh

✓ Deployed alpha @ COMMIT → /home/USER/.engram
  Version marker: /home/USER/.engram/.deployed-version
```

**Checkpoint**: deploy.sh exited 0 + you saw the "✓ Deployed alpha" line.

---

## Step 5 — Handle the install-time template-changes warning (HARD GATE)

If deploy.sh printed `⚠ Install-time template changes since your last deploy` followed by a list of template paths, **this is the load-bearing step of this skill**. Do NOT proceed to Step 6 until this is resolved.

Why this is a hard gate: `deploy.sh` does NOT touch `~/.claude/CLAUDE.md` or `~/.claude/settings.json` — they're rendered from templates at install-time. If templates changed since the last deploy, the live config is now out of sync with the alpha-intended config. Skipping the inverse-merge means new disciplines, hook fragments, or settings don't reach the running agent.

**The "I'll come back to it" failure mode is real.** It happened 2026-05-16: deploy.sh nudged about CLAUDE.md template changes for PR #174's context-mode marker discipline; the agent context-impressioned "I'll merge it later"; the merge never landed; the drowsiness ceiling kept misfiring on 1M-context sessions until the gap was diagnosed days later.

### Sub-step 5a — Identify what changed in each template

For each template path the warning lists:

```bash
diff /path/to/engram-alpha/CLAUDE.md.template ~/.claude/CLAUDE.md
diff /path/to/engram-alpha/settings.json.template ~/.claude/settings.json
```

Read the diffs. Identify which sections are template-side additions you want to inverse-merge into the live file, vs which are agent-local customizations to keep.

### Sub-step 5b — Inverse-merge

For each template-side addition that's identity- or discipline-forming (per Cross-Surface Persistence rule):
- Add the section to the live file (`~/.claude/CLAUDE.md` or `~/.claude/settings.json`)
- Preserve agent-local customizations — the template is the floor, not the ceiling

If the change is mechanical (new hook fragment, new permission), just copy it. If it's judgment-laden (new discipline section, conflict with existing local content), surface the diff to the user and decide together.

### Sub-step 5c — Verify

After merging, re-check that the new content is actually in the live file. Grep alone can pass a stale partial merge (the phrase landed but surrounding context is wrong), so pair grep-presence with a re-diff:

```bash
grep -n "<key phrase from the new section>" ~/.claude/CLAUDE.md
diff /path/to/engram-alpha/CLAUDE.md.template ~/.claude/CLAUDE.md | head -50
```

The grep confirms the merge happened; the re-diff shows what drift remains. Read the diff's remaining hunks: each one should be either (i) an agent-local customization you intend to preserve, or (ii) a template-side addition you intentionally chose not to merge. Any other drift means the merge was incomplete.

**Checkpoint**: every template path the warning listed has either been inverse-merged with grep+diff-verified content OR explicitly deferred with the user's agreement AND tracked in `~/.engram/ask-{{USER_NAME}}.md` so it doesn't fall off. Never default to silent defer.

---

## Step 6 — Cross-surface: skills and agents (manual)

`tools/deploy.sh`'s manifest is scoped to `$ENGRAM_HOME` (`~/.engram/`). Skills and agents live outside it (`~/.claude/skills/`, `~/.claude/agents/`) and require manual copy:

```bash
cp -r /path/to/engram-alpha/skills/claude/* ~/.claude/skills/
cp -r /path/to/engram-alpha/agents/claude/* ~/.claude/agents/
```

(Use `gemini` instead of `claude` if the agent runs under Gemini.)

**Do NOT re-run `install.sh --force` to "refresh" these** — that archives the entire knowledge graph. The targeted `cp` is the safe path.

**Recommended**: diff before overwriting if the agent has made local customizations to a specific skill or agent file. Re-add the local changes after the cp if they matter.

**Checkpoint**: cp commands ran without error + a spot-check (e.g., `ls ~/.claude/skills/engram-<latest-skill>/` confirms the new file landed).

---

## Step 7 — Restart MCP server (user action)

`tools/deploy.sh` wrote the new `server.py`, but the running MCP server is still the old version. Tell the user:

> Deploy complete. Please restart the engram MCP server so Claude Code picks up the new code. Inside Claude Code, run `/mcp` to open the MCP management surface and reconnect the engram server, or restart Claude Code entirely. Hooks (`context_tracker.py`, etc.) reload per-fire automatically — no restart needed for those.

**Do NOT** attempt to kill the MCP subprocess yourself. The stdio-MCP redeploy contract is user-restart only — killing the subprocess from an agent breaks the harness's transport state.

**Checkpoint**: user notified + user has either restarted or explicitly chosen to defer (e.g., "I'll restart after the next nap"). If deferred, note that the new server.py code is NOT yet running.

---

## Step 8 — Verify version marker

```bash
cat ~/.engram/.deployed-version
```

The marker records the alpha commit SHA + timestamp. If the user reports a behavior change later, this is the forensics anchor. Confirm the SHA matches what Step 1 said you were deploying.

**Checkpoint**: marker SHA matches the source-tree commit you intended to deploy.

---

## Step 9 — Record the upgrade in ENGRAM

File a single observation capturing the upgrade so future-self can trace what code is actually running. Minimal recipe:

```python
engram_add_observation(
    claim="ENGRAM upgrade applied <YYYY-MM-DD HH:MM TZ>: source-tree at <commit SHA> (<branch>) deployed to ~/.engram/. Template-changes warning addressed: <list of template paths merged | 'none'>. Skills/agents copied: <yes | no | partial>. MCP restart: <done | deferred>.",
    url="file:///home/<user>/.engram/.deployed-version",
    title="ENGRAM deploy <YYYY-MM-DD> — <commit SHA>",
    quoted_text="<paste the contents of .deployed-version>",
    quote_type="hard_data",
    source_class="introspective",
    source_date="<YYYY-MM-DD>",
)
```

**Checkpoint**: obs filed; SHA from the obs claim matches Step 8's marker.

---

## Anti-patterns

- **Context-impression that a step is done.** Each step has a checkpoint command for a reason — verify completion explicitly, not from memory of "I think I did that." This is the failure mode the skill exists to close.
- **Skipping Step 5 because the warning "looked like it'd be fine."** The template-changes warning is the load-bearing step. If you defer it, write it to `~/.engram/ask-{{USER_NAME}}.md` immediately so it doesn't fall off. Silent defer is the failure pattern.
- **`install.sh --force` to "refresh" an existing install.** This archives the entire knowledge graph. Always upgrade with `tools/deploy.sh` for existing installs.
- **Killing the MCP subprocess to "restart" it.** The stdio-MCP contract is user-restart only.
- **Bundling unrelated source-tree changes into the deploy.** Step 1's `git status` check exists to catch this. If you've been editing alpha source while preparing the upgrade, commit the deploy-relevant changes and stash the rest first.
- **Skipping the ENGRAM record (Step 9).** Without the record, "what version is my running ENGRAM at?" requires forensics. The .deployed-version file answers it for the filesystem, but the ENGRAM obs answers it for future-self in graph-context.

---

## Relation to other skills

- **engram-self-improve** — Self-improvement may produce a code change that needs deploying; after committing the fix, this skill ships it.
- **engram-surgical** — For data-corruption recovery, NOT routine upgrades. If `engram-surgical` is the right call, deploy.sh is not.
- **engram-nap / engram-sleep** — An upgrade is a meaningful state-change worth a nap checkpoint if context permits (Step 9's obs IS the minimum record; a nap captures the wider session context).
