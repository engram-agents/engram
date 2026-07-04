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
- **`main`** is the stable-release branch. It moves only on explicit release
  cuts; routine PRs do NOT target main. **A release cut = opening a formal
  PR from `dev` → `main`** — this fires the full CI gate (`tests.yml` full
  suite + `pr-checks` + `non-author-approval`), the thorough pre-release
  validation. See `docs/RELEASING.md`.
- **CI cost model (#1597):** dev PRs run only the cheap combined `pr-checks`
  (static scrubs) + `non-author-approval`. The **full pytest suite does NOT run
  on dev PRs** — at agent-PR velocity it's too expensive; run **affected-tests
  locally** and paste the result in your review. The full suite runs once, on
  the dev→main release PR (above). `affected-tests.yml` is `workflow_dispatch`
  only.
- Verify before specifying a PR base: `gh repo view --json defaultBranchRef`
  returns `dev`, and recent merged PRs (`gh pr list --state merged --json baseRefName`)
  all show `dev`. If your session-start banner shows a branch other than `dev`
  for "Main branch", trust the repo state — confirm with the `gh repo view`
  command above. Fix locally with `git remote set-head origin --auto` to
  re-sync your clone's `origin/HEAD` pointer.

## Install tiers — a design-time discipline

Tiering is **not** a release-time packaging step bolted on at the end — it starts
at **design time**. The first question for any new artifact — a feature, tool,
hook, skill, sub-agent, schema change, or whole subsystem — is: **which tier does
this belong to, and how does it ship accordingly?** If you can't answer that, the
design isn't finished.

**Three cumulative depth tiers** (Essential ⊂ Convenience ⊂ Dev — choosing one
ships it plus everything below):

- **Essential (T1)** — removing it breaks epistemic identity OR continuity: the
  MCP server + all its tools, the core read/write/continuity hooks, the drowsiness
  meter (it times the pre-compaction consolidation save), the consolidation cycle
  (sleep / nap / first-session / retract / contradiction-resolution), the
  dream/summary fairies, the runtime modules, the identity docs.
- **Convenience (T2, the default)** — established agentic routines that *degrade
  UX but are recoverable*: self-paced loops, coder/reviewer fairies +
  orchestration, research/curiosity loops, the upgrade flow, the viz dashboard,
  the deference detector, the proactive output style.
- **Developer (T3)** — ENGRAM-development tooling only: self-improvement, the
  recall-measurement harness, leak scanners, DB-surgery tools, the surface-drift
  hooks, baselines.

**Orthogonal axis — `--multi-agent`** (a topology flag, not a depth tier; composes
with any depth tier): inter-agent letters / turn-state batons / the shared forum —
their CLIs, prompt-hooks, coordination skills, and the multi-agent CLAUDE.md
template.

**Two load-bearing rules:**

1. **A safety discipline ships with its tool, at the same tier.** If *misuse* of a
   tool is harmful (not merely "unused-is-benign"), its usage-discipline skill is
   the same tier as the tool — they are one inseparable unit. (This is why the
   trust-tier MCP tools **and** the `engram-trust-tier` skill are all T1: a safety
   tool used without its discipline is worse than not having it.)

2. **Every shippable is tagged exactly once in the manifest.** The tier map *will
   be* the single source of truth at `src/build/packaging/tiers.json`; `build-plugin.sh
   --tier` *will* filter through it at **build time** (the
   agent-led install reads the README, prompts the user for tier + multi-agent, and
   builds accordingly — no post-install pruning). **CI must fail the build if any
   shippable path is untiered or any manifest entry is dangling** — so "added a
   skill, forgot to tier it" cannot drift past review. This is a mechanical gate,
   not vigilance.

*(The `src/build/packaging/tiers.json` manifest + the CI tag-once test land with the tiering
rollout. Until they do, the tier of any new artifact is still a required design
decision — record it in the audit so the manifest is a transcription, not a
re-derivation.)*

## Plugin build restructures code paths — never rely on the `src/` layout

The plugin build (`build-plugin.sh`) does **not** mirror the `src/engram/` source
tree into the installed plugin — it **restructures** it. Notably it **flattens
`hooks/claude/*` → `hooks/*`** and **`skills/claude/*` → `skills/*`** (the
`claude/` level is dropped), so an installed file sits at a **different nesting
depth** than its source file. The plugin root is `$CLAUDE_PLUGIN_ROOT`; the source
checkout has no equivalent marker.

**Therefore: any code that resolves a sibling/companion path at runtime MUST NOT
assume the source layout.** A fixed `Path(__file__).resolve().parents[N]`
calibrated against `src/engram/hooks/claude/<f>.py` **overshoots** in the deployed
`hooks/<f>.py` (one level shallower) — and does so **silently**, because it still
resolves to *a* path, just the wrong one.

- **Do:** resolve via `$CLAUDE_PLUGIN_ROOT` first (reliably set when hooks/tools
  run — `hooks.json` invokes every hook as `${CLAUDE_PLUGIN_ROOT}/...`), then fall
  back to a **walk-parents search for the actual target file** (the nearest
  ancestor dir that contains what you need), never a fixed `parents[N]`. See
  `engram-baton-prompt-hook.py` / `engram-forum-prompt-hook.py` for the pattern.
- **Verify against the DEPLOYED tree, not just `src/`.** This bug class is
  invisible to CI and unit tests — they run in the `src/` layout, where the
  source-relative path happens to work — so it surfaces only when the
  *actually-installed* file runs. The guard is a regression test that recreates
  the flattened layout (and runs with `$CLAUDE_PLUGIN_ROOT` **unset** to exercise
  the walk-parents fallback).

*Origin (#1537 → #1539): the pure-API baton prompt-hook used `parents[2]/tools` to
import `forum_api` — correct in source, overshot to `plugins/tools` (nonexistent)
in the deployed tree → `_FORUM_API_OK=False` → the hook silently no-op'd. Caught
only by running the deployed hook, after reviewer-fairy + colleague + CI all
passed on the source tree. Green CI ≠ deployed-correct when CI exercises only the
source layout.*

## Plugin deploy: for our LOCAL (directory) marketplace, the SOURCE tree is live

For our **`directory`-source local marketplace**, Claude Code invokes hooks / the
MCP server / skills **directly from the marketplace SOURCE tree**
(`~/.engram/marketplace/plugins/engram/`), and `CLAUDE_PLUGIN_ROOT` resolves to
that path. (`~/.claude/plugins/known_marketplaces.json` sets `installLocation` to
the source dir itself for a directory source.) The
`~/.claude/plugins/cache/.../<version>/` snapshots + `installed_plugins.json`
`installPath` are version **bookkeeping, NOT the live load path — for hooks /
skills / tools.** **The one exception is AGENTS (sub-agents / fairies): they load
from the `installPath` cache snapshot, NOT the source tree** (live-verified #1639,
2026-07-02) — so for agents the cache IS the live path, and `installPath` points at
a `.../cache/.../<version>/` snapshot, not the source dir. Mind the two distinct
fields: marketplace `installLocation` (`known_marketplaces.json`) = source; plugin
`installPath` (`installed_plugins.json`) = cache snapshot. Hooks/skills/tools follow
`installLocation`; agents follow `installPath`. **Verified 2026-06-28** by
instrumenting every hook copy and watching which fires in the live session: only the
marketplace-tree copy fired, with `CLAUDE_PLUGIN_ROOT` = the source (that test
covered hooks; the agent path was separately verified in #1639).

**What this means for deploys/upgrades (the OPPOSITE of the official-marketplace
model):**

- **`tools/install-local-marketplace.sh` makes new HOOK / TOOL / SKILL code LIVE
  immediately** — it rebuilds the source tree, which IS the live path. **No
  `/plugin update` is needed** for hooks/tools/skills on a directory install
  (hooks hot-reload: re-read from the source file per prompt).
- **AGENTS are an exception (like the official-marketplace model):** sub-agent /
  fairy specs (`agents/*.md`) load from the `installPath` **cache snapshot**, not the
  source tree — so a rebuilt source is inert for agents until you **`/plugin
  marketplace update engram-local` + `/reload-plugins`** (a full restart also works,
  but is heavier). This failed silently in #1639: the #1551 dual-prefix MCP-tool fix
  lived in source but never reached dispatched dream-fairies, which ran with zero
  ENGRAM MCP access until the cache was refreshed. Verify an agent change by
  dispatching one and confirming its tools (a stale-cache agent fails *silently*).
- **MCP server is also an exception:** it launches from
  `${CLAUDE_PLUGIN_ROOT}/launch-engram-server.sh` (= the source), but the running
  *process* is from session start — a changed `server.py` needs a `/mcp` reconnect
  or a restart to reload the process. The path is the source; the process does not
  hot-reload.
- **A changed `hooks.json` registration needs a full Claude Code restart**
  (registration is read at session start). This is what caused the v0.1.4 four-day
  silent-hook outage: a guard on marketplace-path-invoked hooks killed all live
  hooks, *because our live hooks ARE invoked from the marketplace path.*

**To know which code is actually running, instrument the live artifact — never
infer from a proxy.** A version string, `installed_plugins.json`, or a bash-env
test will mislead you (each misled me in turn on 2026-06-28 — a stale
`BATON_PROJECTS_DIR` even made an old hook *look* migrated). The settling test:
add a stderr/file sentinel to the top of each candidate hook copy, fire a REAL
prompt (a self-wake counts), and read which copy logged — that, plus
`CLAUDE_PLUGIN_ROOT` read from *inside* a live hook, are the only ground truth.

*Full system model — why directory-source loads in-place, the per-component scope
(verified vs inferred), the complete edit-takes-effect table, and the
verify-the-live-artifact method — lives in
[`docs/claude-code-plugin-system.md`](docs/claude-code-plugin-system.md) (read
on-demand; not loaded every session). This behavior is **undocumented upstream** —
re-verify after any Claude Code version bump.*

## PR merge-readiness — CI-green gate (both modes)

Merge-readiness has **two** gates, not one: a PR is merge-ready only when it is
**review-converged AND CI-green on the current tip.** This is a property of the PR,
not of the coordination topology — it holds identically in single-agent and
multi-agent mode. (Multi-agent: it gates the `baton flip → <maintainer>` below.
Single-agent: it gates surfacing the PR as ready / flipping the solo cursor to the
maintainer. Same rule, different surface — so a single-agent contributor is just as
bound by it.)

Before presenting a PR as merge-ready, check CI on the current tip:

```
gh pr view <N> --json statusCheckRollup,mergeStateStatus
```

(`mergeStateStatus: CLEAN` + every `statusCheckRollup[].conclusion: SUCCESS` = green.)

- **Green** → present as merge-ready.
- **Pending / running** → do NOT present as ready yet. CI is async: hold (keep the
  turn with yourself), **re-check on the next loop tick**, advance only once green.
  This is the one legitimate case where a converged PR sits un-advanced for a tick.
- **Failed** → it's a **blocker**: back to the author for a fix round, same as any
  reviewer blocker. Never present a red PR as ready.

**Re-check after ANY post-approval commit.** A moved tip (e.g. folding a late
reviewer nit after the approve) re-triggers CI and can fail on the new commit even
though the approved commit was green — CI-green must hold on the tip the maintainer
would actually merge.

*Origin: converged-but-red PRs reached "ready" framing twice (a CI gate was red while
review-convergence was checked but CI was not). v1 is this discipline (vigilance); the
structural form — a flip-to-maintainer guard that refuses when CI is red, mechanical
like the CI tag-once gate — is the better v2 (mechanical gate > vigilance), tracked as
a follow-up.*

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
   colleague review to a fairy has skipped the colleague layer (the user flagged
   this directly, after the colleague-fairy-delegation lapse incident).

Review hierarchy: **reviewer-fairy → counterpart colleague → maintainer
(merge)**. Symmetric — each agent reviews the other's PRs and has its own
reviewed in turn. Review-mode: `--comment` for in-flight iteration; `--approve`
is posted by a reviewer (fairy or colleague) when converged, never by the author.

### Every PR carries a baton — merge-readiness is baton-tracked, not inferred

Open a PR-baton **at PR creation**, for **every** PR — bunch-driven OR standalone:
`baton init PR-N --title "<short>" --participants <self>,<counterpart>,<maintainer> --turn <self> --status in-progress --colleague <counterpart>`.
Use `--colleague <name>` to identify the expected colleague reviewer — this adds them to participants (dedup) and enables the colleague gate: `baton flip PR-N <maintainer>` will be rejected if no non-author APPROVED review exists. Without `--colleague` (and without a third participant), the gate falls back to warn-only (single-agent mode).
Then flip it as the turn actually passes along the review pipeline:

- reviewer-fairy converged → `baton flip PR-N <counterpart> "fairy-converged; colleague please"`
- counterpart **comments / requests changes** → it flips back to you (`flip PR-N <self>`);
  iterate and re-review. Only an explicit colleague **approval** advances the baton — a
  "COMMENTED with considerations" verdict does **not** (that misread is the whole origin).
- counterpart **approved** → `baton flip PR-N <maintainer> "colleague-APPROVED; ready for merge"`

*(Single-agent mode — no counterpart: skip the colleague flip; after the reviewer-fairy
converges and your own second-layer pass is clean, flip straight to the maintainer.)*

**A PR is merge-ready ONLY when its baton turn = the maintainer (pool sentinel)** —
reached only via an explicit colleague approval (or, single-agent, your own converged
second-layer pass). The maintainer merges from that baton signal — never from a PR that
merely *looks* parked. A PR with **no baton**, or a baton **not yet flipped to the maintainer**, is
**not** ready, no matter how long it has sat. Symmetrically: never leave a
colleague-converged PR without flipping its baton to the maintainer — otherwise its
readiness is invisible, and a colleague verdict that is actually "COMMENTED with
considerations" (not an approval) can be misread as ready.

*Origin: a PR mis-merge — the colleague verdict lived only in a letter and no baton
tracked the turn-state, so a parked-but-unconverged PR read as merge-ready. The
baton's `turn:` field is the single load-bearing merge-gate; a letter is not.*

**Single-agent mode:** no counterpart exists, so the colleague layer collapses
to reviewer-fairy + the author's own second-layer eye + the maintainer. Don't
block on a colleague review that has no one to perform it; the
not-fairy-delegatable constraint above is moot when there is no counterpart to
delegate to.

**Single-agent on host with cross-host forum peers:** if you are a single agent
on your machine but have peers reachable via the LAN forum (e.g. Borges and
Ariadne on a different host), **mention the relevant peer on the forum after any
GitHub activity that requires their reaction** — posting a review, requesting a
review, commenting with findings, approving, or responding to a review. The
baton/letter system requires a shared filesystem; the forum is the only
real-time cross-host notification channel. A brief `@name` mention in a forum
post or reply is enough — the forum-mention Monitor delivers it within ~30s.
Without this, a review or request sits unacknowledged until the peer's next
scheduled wakeup.

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
2. `baton init pool-X --title "..." --participants borges,ariadne --turn <maintainer>
   --status in-progress --github project/N` → creates
   `/home/agents-shared/projects/pool-X.md` with the GitHub anchor set. The
   auto-pull hook reads `github: project/N` to render the Project's
   item-status tally each prompt.
3. Add a `## Issues in this bunch` section to the baton body listing the
   issues by `#NNN <title>`.
4. `gh project item-add N --owner engram-agents --url <issue-url>` for each
   issue.

Pool sentinel `turn: <maintainer>` = the bunch is available for claim. (For batons
already created without `--github`, use `baton anchor pool-X --github
project/N` to set the anchor after the fact.)

### Claiming and working a bunch

- `baton claim pool-X` — `turn:` flips to the claimer; refuses if already
  claimed (no stealing).
- The claimer drives the bunch toward fix-PRs. Each emerging PR opens its own
  PR-baton (per "Every PR carries a baton" in the PR review convention above —
  that rule holds for standalone PRs too, not only bunch-driven ones); the bunch
  baton stays put.
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

## PR — issue closure

When a PR fully closes an issue, **both surfaces are required**:

1. **PR title** ends with `[closes #N]` — the human-scan canonical surface.
   Scannable in `gh pr list`; reviewer can confirm closing intent without
   opening the PR. Title is editable post-filing via
   `gh pr edit <PR> --title "..."` if you forgot the tag at open-time.
2. **PR body** contains the close-keyword form `Closes #N.` (case-insensitive).
   **This is what fires GitHub's auto-close parser on merge** — empirically
   verified via the `closingIssuesReferences` GraphQL field. Without it, the
   issue does NOT auto-close on merge regardless of title content. (The
   bracket form in the title is human-scan cosmetic; the parser ignores it.)

**Empirical anchor** (2026-05-31, Borges + Ariadne; #572):
- 7/7 sampled title-only PRs (`[closes #N]` in title, no body keyword): all had
  `closingIssuesReferences=[]` and would not have auto-closed on merge.
- Body-keyword PRs: `closingIssuesReferences=[N]` populates correctly.
- Backwards-compat sweep: 0 issues leaked open — the 7 title-only PRs' issues
  ARE closed, but close events show **human actors** with `closer_pr: null`.
  Maintainers have been manually closing issues during merge waves, which
  silently masked the gap. Risk is purely forward-looking; no remedial cleanup
  needed. **The amendment is preventive, not remedial.**

**For multi-PR / EPIC ships:** earlier PRs may reference the issue number in
title or body, but **do not** use a closing keyword in either surface. Only the
LAST PR in the series uses BOTH `[closes #N]` in title AND `Closes #N.` in
body. Don't rely on a "wrap-up PR I'll file later" — that PR rarely materializes
(empirical: 7 of 11 stale-but-addressed candidates in the 2026-05-28 backlog
scan followed this pattern, leaving their parent issues open indefinitely).

**For partial work that addresses but doesn't fully close:** omit the closing
tag from both title AND body. Comment on the issue describing what landed and
what remains, so the scope is durable.

### Body-text trap discipline (referencing the wrong number)

The auto-close parser sees `(close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s+#?\d+`
in the body **regardless of negation or brackets** (the `NOT` and the `[ ]` are
invisible to it). This is the mechanism that fires intentional auto-close — and the
same mechanism that fires *accidental* auto-close when the body references a
NON-target issue.

**Backticks are the one exception** (measured on PR #1191): GitHub's
auto-close parser ignores keywords inside inline-code spans, so a backticked
`` `Closes #M` `` does **not** populate `closingIssuesReferences` and will **not**
auto-close on merge. Two consequences: (a) an *intentional* `Closes #N` must be
**plain text** — a backticked one silently fails to auto-close (caught pre-merge on
#1191 by the `closingIssuesReferences` verify step below); (b) the verification grep
below is a plain regex that **does** match inside backticks, so it conservatively
over-flags backticked refs — a backticked match is safe-from-the-real-parser, so
confirm it rather than treating it as an auto-close risk.

**Empirical traps caught (2026-05-30):**
- PR #516 body said `"this PR does NOT close #510"` → auto-closed #510 on merge
  (the NOT is invisible to the regex; caught manually + reopened).
- PR #537 round-1/2 body had two trap occurrences referring to #51 → would
  have auto-closed #51 on merge (caught pre-merge by counterpart-colleague).

**Trap shapes (DO NOT write any of these in the body):**

| Trap shape | Problem | Safe paraphrase |
|------------|---------|-----------------|
| "this PR does NOT close #M" | parser closes #M (negation invisible) | "first slice of #M's EPIC — umbrella stays open" |
| the final scrub PR uses closes #M (example aside, **unbackticked**) | parser closes #M (plain-text keyword fires, even in an aside) | "the final scrub PR carries the closing tag in its title" — or backtick the example (`` `closes #M` ``), which the parser ignores |
| "fixes #M partially" | parser closes #M | "addresses one slice of #M; remainder tracked in next PR" |

**Safe forms for referencing without closing**: `#M`, `issue #M`, `umbrella #M`,
`part of #M`, `addresses #M`, `relates to #M`. None of these trip the parser.

### Verification before pushing the PR

Two checks, run in order:

1. **Auto-close intent fires** (when the PR DOES close an issue): the body
   must contain a close-keyword referencing the target.
   ```
   gh api graphql -f query='{repository(owner:"engram-agents",name:"engram-alpha"){pullRequest(number:<PR>){closingIssuesReferences(first:5){nodes{number}}}}}'
   ```
   The full API envelope is `{"data":{"repository":{"pullRequest":{"closingIssuesReferences":{"nodes":[...]}}}}}` —
   look for `nodes: [{number: <N>}]` where N matches the title's `[closes #N]`.
   `"nodes":[]` (empty) means the body is missing the keyword form and the PR
   will NOT auto-close on merge; rewrite via `gh pr edit <PR> --body "..."`.

2. **No trap-shape auto-close** (always): every body match must reference the
   SAME `#N` the title carries.
   ```
   gh pr view <PR> --json body -q .body | grep -iE '\b(close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s+#?[0-9]+'
   ```
   Inspect every match. Any reference to a different issue number is a trap
   — rewrite via `gh pr edit <PR> --body "..."` with the trap shape removed
   (use the safe-paraphrase table above).

**When reviewing a PR** (reviewer-fairy or counterpart colleague):
- Title missing `[closes #N]` on a PR that addresses an issue → blocker.
- Title `[closes #N]` present but body missing the matching close-keyword →
  blocker (PR will not auto-close on merge despite the title intent). Author
  edits body via `gh pr edit <PR> --body "..."` before merge.
- Body contains close-keyword referencing an issue OTHER than the title's `#N`
  → blocker (trap). Author rewrites the trap-shape via safe-paraphrase table.

This double-gate is mechanical; checking is cheap; the cost of missing it is
either a stale-issue backlog (missed auto-close) or a silently-closed-wrong-issue
(trap firing).

**GitHub recognized closing keywords (case-insensitive):** `close`, `closes`,
`closed`, `fix`, `fixes`, `fixed`, `resolve`, `resolves`, `resolved`. This repo
uses `closes` only — both `[closes #N]` in title and `Closes #N.` in body.
Other keywords technically work but fragment the convention; if a reviewer
sees a non-`closes` form, flag as a blocker and ask the author to rewrite.
