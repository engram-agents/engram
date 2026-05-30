---
name: engram-fairy-orchestration
description: Use when dispatching one or more fairies (sub-agents via the Agent tool) for PR coding work, paper research, ENGRAM consolidation, or any task delegated to a focused sub-agent. Holds the operational protocol — briefing pattern, cohort orchestration for 3+ stacked/parallel PRs, fairy worktree lifecycle (cleanup, branch-ref verification, recovery procedures), and the gotchas surfaced from real cohorts. Load before a single-fairy dispatch OR a multi-PR cohort. CLAUDE.md holds the WHEN-to-delegate decision rules + tripwires; this skill holds the HOW.
---

> **Note for the agent:** The ENGRAM node IDs referenced in this skill (e.g., `ob_1755`, `ls_0047`, `ob_1970`, `ob_1978`, `ls_0030`) are leftover references to the alpha developer's graph — they don't exist in your install. The skill is yours now; the disciplines stand on their own. File your own observations to replace these anchors as you accumulate cohort experience.

# Fairy orchestration — operational protocol

Use this skill when about to dispatch sub-agents (fairies) for focused work. CLAUDE.md tells you when to delegate; this skill tells you how to do it well, and how to scale to cohorts of 3+ stacked/parallel PRs without losing track.

The empirical record this skill rests on: ob_1755 (fairy contractor principle), ob_1970 (worktree-lifecycle pain points + 39 hanging worktrees incident), ls_0047 (always-delegate-PR-coding), ob_1978 (8-PR cohort 2026-05-21 manager-of-fairies validation), ls_0030 (sub-agent .md disciplines).

---

## §1 — Briefing pattern (every dispatch)

Per ob_1755 — fairies are contractors, not colleagues. The spec is the contract. A fairy has no access to your mind, conversation history, ENGRAM, or accumulated context. Anything load-bearing must be in the spec.

- **Pre-spec coordination check** (BEFORE drafting the spec). If the spec names an issue number, verify no open PR already addresses it. Two checks, both cheap (substitute the actual issue / PR number for `NN` in each):
    - `gh pr list --search 'in:title "#NN"' --state open` — catches a counterpart's open PR with the issue number in its title.
    - `ls /home/agents-shared/projects/ 2>/dev/null | grep -E '#NN|PR-NN'` — multi-agent mode only; surfaces a counterpart's in-flight baton for that issue or PR.
  - Skipping this is the failure mode that produced PR-517 (issue #486) on 2026-05-30 — a duplicate of the counterpart's PR-502, which had already converged. Same lines, guaranteed merge conflict. The pre-check would have caught it in 5 seconds.
  - Single-agent mode: only the `gh pr list` check applies. Shared-projects path doesn't exist.
- **Self-contained prompt.** Include repo path, branch baseline, template commit/PR to read, scope, validation step. Inline (don't link to) any node IDs or context-laden references the fairy needs.
- **Hard scope bounds.** Explicit "DO NOT modify X" for adjacent surfaces. Anti-patterns enumerated. Stop conditions named.
- **Escape valve.** Always include some form of: *"if you hit anything that doesn't match the template, STOP and report back."* Silent improvisation is the failure mode.
- **Always run in background** (`run_in_background: true`). Never wait synchronously. Completion notifications arrive automatically and re-invoke you.
- **Suggest branch name.** Spec should name the branch. Anticipate collisions — if your suggested name might exist remote, note that the fairy can use a `-v2` fallback.
- **Output contract.** Reference the fairy's `# Output contract` section. Demand verification (test results, syntax check, `git diff --stat` scope check) + ambiguous-decision log. Without verification claims, the report can't be trusted.
- **Post-creation handoff** (AFTER `gh pr create` returns the PR number — the **dispatching agent** runs this, not the fairy; fairies don't have shared-projects access). Open a baton for the PR — this is the channel by which the counterpart and the user see your PR is in flight; skipping it means the auto-pull hook can't surface the work.
    - `baton init PR-N --title "<short>" --participants <list> --turn <next-actor> --status in-progress --github pr/N`
    - See the `engram-baton` skill (§"PR layer — `flip`") for the full PR-cursor lifecycle.
    - Single-agent mode: the baton is still useful as a per-PR cursor through fairy → maintainer, but the multi-agent coord surface is absent.

---

## §2 — Cohort orchestration (3+ stacked / parallel PRs)

When work spans 3+ PRs with dependencies, treat as a cohort. The empirical pattern from 2026-05-21 (8 PRs in ~3.5 hours):

### Three-layer task tracking

- **TaskCreate** (in-session dispatch graph) — one task per PR, wire dependencies via `addBlockedBy`/`addBlocks`. The graph IS the dispatch decision surface — at any moment, the unblocked-in-progress-pending split tells you what to dispatch next.
- **ENGRAM `engram_add_task` node** (cross-session backbone) — file a parent task for the cohort with `scope=milestone` serving the relevant goal. Cohort survives compaction here.
- **`ask-<user-name>`.md (the user-facing live queue, where `<user-name>` is the agent's primary collaborator)** — concise per-PR status, prune as items resolve, surface ready-for-merge state.

Don't conflate roles. TaskCreate is the manager's view; ENGRAM is the historian's view; ask-<user-name>.md is the collaborator's view.

### Bottleneck-first

Identify the single critical-path piece. Spec it carefully — its quality cascades to every downstream PR. Other work fans out around it. Most cohorts have ONE foundation PR; the rest stack on or run independent.

### Spec-vs-implementation separability

Spec-writing is text. It has no conflict surface. Draft ALL specs upfront (even ones whose dependencies aren't ready yet) so they're queued for dispatch the moment deps clear. Save specs to `~/.engram/cohort-specs-<date>/` for durability across compaction.

The failure mode this prevents: holding specs because their *implementation* would conflict with a running fairy. That conflates two different surfaces. Specs are text; only implementation has conflict.

### Stacked PRs over waiting

When PR-B consumes PR-A's API, branch PR-B off PR-A's tip (not `dev`). Rebase after PR-A merges; GitHub auto-retargets stacked PRs when the base merges. Don't gate throughput on the user's merge cadence.

**The deeper insight**: this is how git is meant to be used. The branch graph mirrors the dependency map. Clean dependency thinking produces clean branch graph naturally; mess in the branch graph is evidence of unclear dependency thinking.

### Spec quality is the lever

ls_0047 (always-delegate) + bottleneck-first + spec-vs-impl-separability compose into the structural finding: **concurrent throughput unlocks by spec quality, not by code-writing speed.** The bottleneck shifts from coding to specifying. Optimize there.

---

## §3 — Fairy worktree lifecycle

Every fairy that touches a repo gets dispatched with `isolation: "worktree"`. Cleanup is the parent's responsibility — Claude Code's worktree-cleanup contract preserves any-commits worktrees so the parent can extract artifacts before discarding.

### Cleanup discipline

After fairy returns + commits/artifacts extracted:
```
git worktree remove -f -f <path>
```

Prefer **double-force** (`-f -f`) over single `--force`. Single-force errors on stale-pid locks ("cannot remove a locked working tree, lock reason: claude agent agent-XXX (pid Y)") even when the fairy has completed. Double-force overrides the lock.

### Reflex checks

**Before dispatching:** `git worktree list | grep <branch>`. If the target branch is currently checked out in another worktree, git refuses a second checkout. Either clean the existing worktree first, or branch the new fairy off a different name.

**After fairy returns:** verify branch ref. Fairies sometimes commit to `worktree-agent-XXXXX` (the harness-assigned worktree branch name) instead of the named branch the spec requested. Run:
```
git rev-parse <expected-branch>
```
If mismatched, recover with:
```
git branch -f <expected-branch> <fairy-commit-sha>
```

### Hygiene gotchas

- **Nested worktrees**: when a fairy is dispatched to fix issues on a branch already checked out by a previous fairy's worktree, the new fairy's worktree nests inside the previous one (e.g., `.claude/worktrees/agent-AAA/.claude/worktrees/agent-BBB`). Functionally OK but hard to clean. Best avoided by cleaning round-N's worktree before round-N+1 dispatches.
- **Branch-name suggestion collisions**: spec's suggested name collides with existing remote branch from unrelated prior work. Fairy creates `-v2`. Anticipate in spec template.
- **Weekly bulk-prune**: worktrees on merged or origin-absent branches. Without periodic pruning, hanging worktrees accumulate (39+ in one incident, ob_1970).

### Recovery procedures (when things go wrong)

| Symptom | Diagnosis | Fix |
|---|---|---|
| `cannot remove a locked working tree` | Stale-pid lock from dead fairy | `git worktree remove -f -f <path>` |
| `fatal: '<path>' is not a working tree` | Already removed but dir lingers | `rm -rf <path>` |
| Fairy commit on wrong branch | Fairy committed to `worktree-agent-XXX` | `git branch -f <expected> <sha>` |
| GitHub 502 on `gh pr create` | Transient API failure | Wait 3s, retry once |
| `branch is already checked out` | Another worktree holds it | Remove the other worktree first, OR branch from a new name |
| Fairy report claims branch X at commit Y but `git rev-parse X` differs | Branch-ref divergence | Force-update: `git branch -f X Y` (verify Y is reachable from the worktree first) |

---

## §4 — Tool transient failures

- `gh pr create` can return HTTP 502. Retry once after 3s.
- `gh pr list` and similar can timeout. Retry once.
- Worktree commands can race with the harness's worktree-tracking metadata. If a remove fails for non-obvious reasons, check `git worktree list` to see if git's view matches your expectation.

---

## §5 — Convergence cycle

Per CLAUDE.md PR Workflow Standards: re-dispatch reviewer after every round-N commit until fairy returns no blockers AND no actionable suggestions worth shipping. Nits can defer.

The discipline empirically: most coder-fairy work converges in round 1 (no changes needed) or round 2 (1-2 small fixes). Round 3+ is rare and usually means the spec was incomplete on a load-bearing dimension — investigate spec quality before assuming the fairy is the problem.

---

## §6 — When to load this skill

You're orchestrating fairy work AND any of these apply:
- About to dispatch a coder-fairy, reviewer-fairy, or other Agent-tool sub-agent
- Cleaning up worktrees from a prior session
- Running a cohort (3+ PRs)
- Recovering from a worktree / branch / dispatch failure

You do NOT need to load this skill for:
- Deciding *whether* to delegate (that's the CLAUDE.md tripwires — they fire without this skill)
- General code-writing you're keeping (the always-delegate rule fires first; if it doesn't trigger, you're not in fairy territory)

---

## §7 — References (durable empirical anchors)

- **ob_1755** — Fairy contractor principle (spec IS the contract, judgment retained, start loose / tighten on real drift)
- **ob_1970** — Worktree lifecycle pain (39 hanging worktrees, cleanup discipline, branch collisions)
- **ls_0047** — PR-coding-always-fairy rule (no size exception)
- **ob_1978** — Manager-of-fairies validation cohort (8 PRs in 3.5h, three-layer task tracking, failure modes catalog)
- **ls_0030** — Sub-agent .md disciplines (NOT-parent disclaimer + tools whitelist + frontmatter strictness)
- **engram-coder-fairy.md** § Character — what we value in a fairy (make the call locally, surface transparently, stay in scope, validate not gesture, escape valve over silent improvisation)
