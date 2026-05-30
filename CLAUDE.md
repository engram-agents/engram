# CLAUDE.md — engram-alpha project conventions

Developer-facing conventions for contributing to engram-alpha. Identity-level
instructions live in the user-level CLAUDE.md.

## MCP tool parameter style

All ENGRAM MCP tools (`@mcp.tool()`-decorated functions in `server.py`) take a
**single JSON-string parameter** — `payload_json`. The tool body json.loads it,
validates, dispatches.

Why: multi-arg signatures hit the antml-prefix swallow risk (Claude Code's
prompt construction can drop arguments under certain whitespace conditions,
causing silent corruption — first observed during the wave-1 tool migration).
Single-payload pattern eliminates the swallow surface.

Applies to: every new MCP tool, every existing-tool migration. Check existing
tools (`engram_add_observation`, `engram_derive`, etc.) for the canonical shape.

Wave-3 migration (2026-05-14) completed the corpus — all tools are now on
the unified payload_json signature as of 2026-05-27.

## Branching convention

- **`dev`** is the default branch and the merge target for ALL feature/fix PRs.
  Always open PRs against `dev` unless an explicit release-cut is happening.
- **`master`** is the stable-release branch. It moves only on explicit release
  cuts; routine PRs do NOT target master.
- Verify before specifying a PR base: `gh repo view --json defaultBranchRef`
  returns `dev`, and recent merged PRs (`gh pr list --state merged --json baseRefName`)
  all show `dev`. If your session-start banner shows a branch other than `dev`
  for "Main branch", trust the repo state — confirm with the `gh repo view`
  command above. Fix locally with `git remote set-head origin --auto` to
  re-sync your clone's `origin/HEAD` pointer.

## PR review convention (multi-agent mode)

When this host runs **multiple ENGRAM agents** (e.g. Ariadne + Borges) — i.e. a
counterpart agent is active and the shared `/home/agents-shared/inter-agent/`
channel exists — PR review is **reciprocal**: an agent's own PRs get a colleague
fresh-eye from the counterpart agent, not only from the agent's own
reviewer-fairy.

The **"colleague converged"** gate is two layers, both required before a PR
enters the maintainer's merge queue:

1. **Reviewer-fairy** (`engram-pr-reviewer`) on every push, iterate to
   convergence — catches mechanical / contract / scope issues. (Applies in
   single-agent mode too.)
2. **Counterpart-agent colleague fresh-eye** — after the fairy converges, the
   author sends the counterpart a letter (via `ia write`, the inter-agent
   letter CLI) requesting review and iterates on their findings. The
   counterpart catches pattern-level / cross-issue / structural-discipline
   issues a fairy can't see. **This colleague round is performed by the
   counterpart's full agent (complete ENGRAM + context) — it is NOT
   fairy-delegatable.** Fairy reviews are junior rounds that clear shallow /
   mechanical errors; the colleague round is the deep, near-final verdict that
   only full-agent capability can render. A counterpart who delegates their
   colleague review to a fairy has skipped the colleague layer (Lei direct,
   on the colleague-fairy-delegation lapse incident).

Review hierarchy: **reviewer-fairy → counterpart colleague → maintainer
(merge)**. Symmetric — each agent reviews the other's PRs and has its own
reviewed in turn. Review-mode: `--comment` for in-flight iteration; `--approve`
is posted by a reviewer (fairy or colleague) when converged, never by the author.

**Single-agent mode:** no counterpart exists, so the colleague layer collapses
to reviewer-fairy + the author's own second-layer eye + the maintainer. Don't
block on a colleague review that has no one to perform it; the
not-fairy-delegatable constraint above is moot when there is no counterpart to
delegate to.

**Why project-local** (not user-level): the single-vs-multi-agent switch is hard
to encode in always-loaded identity rules.

**Origin:** a self-review lapse traced to this reciprocal step being
unwritten in the workflow doc; the hierarchy itself has been practiced
since the multi-agent coordination work started in 2026-05.

## Project-as-bunch coordination (multi-agent mode)

When this host runs **multiple ENGRAM agents** (e.g. Ariadne + Borges), open
issues are organized into **bunches** — topic- or defect-class-coherent units
of work — each backed by **both** a baton file (turn-state) and a GitHub
Project (item-list + status tally). Two layers, separate verbs:

- **Project layer** = the bunch itself (e.g. `pool-cadence-loop`,
  `pool-coordination-system`). One driver at a time; single-owner; rarely
  passes between agents. Verbs: `claim` / `release`.
- **PR layer** = the per-PR turn cursor (e.g. `PR-487`). Frequent passes
  through fairy → counterpart → maintainer. Verb: `flip`. (Convergence gate:
  see "PR review convention" above.)

### Creating a bunch

Either agent can create a bunch; either agent can claim it, regardless of who
created.

1. `gh project create --owner engram-agents --title "pool-X — short scope"` →
   returns Project number `N`.
2. `baton init pool-X --title "..." --participants borges,ariadne --turn lei
   --status in-progress --github project/N` → creates
   `/home/agents-shared/projects/pool-X.md` with the GitHub anchor set. The
   auto-pull hook reads `github: project/N` to render the Project's
   item-status tally each prompt.
3. Add a `## Issues in this bunch` section to the baton body listing the
   issues by `#NNN <title>`.
4. `gh project item-add N --owner engram-agents --url <issue-url>` for each
   issue.

Pool sentinel `turn: lei` = the bunch is available for claim. (For batons
already created without `--github`, use `baton anchor pool-X --github
project/N` to set the anchor after the fact.)

### Claiming and working a bunch

- `baton claim pool-X` — `turn:` flips to the claimer; refuses if already
  claimed (no stealing).
- The claimer drives the bunch toward fix-PRs. Each emerging PR opens its own
  PR-baton (`baton init PR-N ...`); the bunch baton stays put.
- Treat the listed issues as the **named scope**, not the ceiling. Mid-work,
  if a sibling issue surfaces that fits the defect-class, add it to the bunch
  rather than file a parallel one.

### Releasing a bunch

- `baton release pool-X --done` — adds `(done)` suffix, idempotent. Use **only
  after the completeness check** (next section).
- `baton release pool-X --reason "<why>"` — back to pool **without** `--done`
  if scope shrunk / blocked / abandoning.
- Only the current holder can release. Either agent can subsequently claim
  again from the pool.

### Completeness-check gate (Done direction)

Before `--done`, **sibling-grep the defect-class surface** for instances the
named scope missed:

- Identify the defect's class (file pattern, function family, schema-shape,
  label, …).
- `grep -rn <pattern>` across the codebase for siblings.
- If siblings exist: extend the bunch (add issue + drive a follow-up PR) OR
  file new issues and comment scope into the original bunch.

This is the colleague-layer discipline that catches sibling gaps invisible to
diff-scoped fairy review (origin: the completeness-check class of incidents
where a fix landed in one place but missed sibling occurrences in adjacent
files / functions).

### Genre split — letters vs baton

- **Baton** = turn-state only ("the next action is yours" / "I'm done").
- **Letters** (`ia write`) = colleague verdicts (not fairy-delegatable),
  substantive discoveries (cross-PR catches, completeness yields). The system
  needs both — pure-baton/no-letters kills the discovery channel.

### Single-agent mode

No counterpart → no claim contention. The Project layer remains useful as an
organizing structure for defect-classes, but the `claim` / `release` ritual is
moot. PR-layer baton still useful as a per-PR cursor through fairy →
maintainer.

### Existing Projects vs new

- **Reuse** an existing Project when the work fits its scope (e.g. Project #4
  "Coordination System dogfood" already covers baton/Project enhancements).
- **Create new** for genuinely-new defect-classes.
- Empty Projects (e.g. #2 template) are templates, not active bunches.

### Known gaps (tracked separately)

- `baton reopen` verb missing — a `status: merged` baton with open work can't
  return to `in-progress` without manual frontmatter edit.
- Baton CLI doesn't auto-sync the Issues-list section with Project items —
  drift possible; rely on the Project's item-list as ground truth, the baton's
  Issues section as the human-readable summary.

### Operational substrate

- Baton files: `/home/agents-shared/projects/<id>.md` (YAML frontmatter + body).
- GitHub Projects: `https://github.com/orgs/engram-agents/projects/`.
- CLI source: `tools/baton.py` (this repo).
- Skill (HOW details): `engram-baton`.
- Companion letter skill (verdicts + discoveries channel): `engram-letter`.

**Why project-local** (not user-level): same reasoning as the PR review
convention above — multi-agent coordination tooling is project-specific, not
identity-level discipline.

## PR title — issue closure

When a PR fully closes an issue, the PR **title** ends with `[closes #N]`. Single
style for this repo, no alternatives. GitHub auto-closes the issue when the PR
merges to `dev`. The bracket-form is empirically verified on PR #434
(`[closes #432]`); GitHub's underlying auto-close fires on any recognized
keyword + `#N` pair regardless of brackets (#441's `(closes #440)` also auto-closed),
but this rule standardizes the surface form going forward so reviewers can scan
for it uniformly.

Title is the canonical surface (not body, not commit message) — easy to scan in
`gh pr list`, easy to verify pre-merge, and a reviewer can confirm the closing
intent without opening the PR. PR titles are editable post-filing via
`gh pr edit <PR> --title "<new title>"` — if you forgot the tag at PR-open time,
edit it before merge.

**For multi-PR / EPIC ships:** earlier PRs may reference the issue number in
title or body, but **do not** use a closing keyword. Only the LAST PR in the
series uses `[closes #N]`. Don't rely on a "wrap-up PR I'll file later" — that
PR rarely materializes (empirical: 7 of 11 stale-but-addressed candidates in the
2026-05-28 backlog scan followed this pattern, leaving their parent issues open
indefinitely).

**For partial work that addresses but doesn't fully close:** omit the closing
tag from the title. Comment on the issue describing what landed and what
remains, so the scope is durable and the next contributor knows the state.

**When reviewing a PR** (reviewer-fairy or counterpart colleague), if the PR
clearly addresses an issue but the title doesn't carry `[closes #N]`, flag as
a **blocker** — the author edits the title before merge. This is mechanical;
checking is cheap; the cost of missing it is a stale-issue backlog.

### Body-text discipline (avoid the auto-close trap)

**The parser sees keyword + `#N` regardless of negation, brackets, or backticks.**
- `"this PR does NOT close #N"` → auto-closes #N on merge (the NOT is invisible to the regex).
- `"the final scrub PR uses [closes #N]"` → auto-closes #N (brackets don't escape; `closes #N` is matched literally — empirically verified PR #441's `(closes #440)` auto-closed).
- `"the final scrub PR uses \`[closes #N]\`"` → auto-closes #N (backticks don't escape either).

**Empirical (same-day n=2, 2026-05-30):** PR #516 auto-closed #510 via body text "this PR does NOT close #510" (caught manually + reopened); PR #537 round-1/2 body had two trap occurrences (caught pre-merge by counterpart-colleague review). Both authors were consciously trying to follow this rule.

**Safe-paraphrase shapes** (when the body legitimately needs to reference an issue number without triggering auto-close):

| Trap (DO NOT write this in the body) | Safe paraphrase |
|---------------------------------------|-----------------|
| "this PR does NOT close #N" | "first slice of #N's EPIC — umbrella stays open" |
| "the final scrub PR uses `[closes #N]`" | "the final scrub PR carries the closing tag in its title" |
| "addresses #N (will close in follow-up)" | "addresses #N — follow-up PR carries the close-tag" |
| "fixes #N partially" | "addresses one slice of #N; remainder tracked in next PR" |
| "resolves #N" (in body, not title) | drop entirely; only the title carries the closing tag |

**The rule reduces to:** keyword + `#N` belongs ONLY in the PR title. Body references the issue number without close-keyword preceding it (`#N`, `issue #N`, `umbrella #N`, `part of #N`, `addresses #N`, `relates to #N` — none of these trip the parser).

**Verification before pushing the PR body:** `gh pr view <N> --json body -q .body | grep -iE '\b(close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s+#?[0-9]+'` — empty output means the body is clean; any match is a trap to rewrite via `gh pr edit <N> --body "..."`.

GitHub recognized closing keywords (case-insensitive): `close`, `closes`,
`closed`, `fix`, `fixes`, `fixed`, `resolve`, `resolves`, `resolved`. This repo
uses `closes` only, in the `[closes #N]` bracket form. Other keywords technically
work for GitHub's auto-close but fragment the convention; if a reviewer sees a
non-bracket form on a PR that closes an issue, flag it as a blocker and ask the
author to rewrite the title via `gh pr edit <PR> --title "..."` before merge.
