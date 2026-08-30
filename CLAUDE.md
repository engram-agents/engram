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
  suite + `pr-checks`), the thorough pre-release
  validation. See `docs/RELEASING.md`.
- **CI cost model (#1597):** dev PRs run only the cheap combined `pr-checks`
  (static scrubs). The **full pytest suite does NOT run
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

## PR merge authority — the substrate-feature gate

Once a PR is merge-ready (next section), **who merges it** depends on what it
changes: **substrate-concept features → Lei reads the PR first**; **fixes +
non-substrate → the team merges after peer review**. This gate is
**engram-alpha-only** — other repos (umwelt, soma) carry their own delegated
authority. During an explicit Lei-absence day-grant, a settled substrate PR may
be merged to `dev` (revertible) while the irreversible live-deploy stays Lei's.

This is a pointer, not the policy. The single source of truth — the full rule,
the substrate/non-substrate discriminator, per-project scope, the day-grant
carve-out, examples, gray-area calls, and the baton turn-target corollary — lives
in **[`docs/PR-MERGE-POLICY.md`](docs/PR-MERGE-POLICY.md)**. Read it there; don't
re-derive the rule from this summary.

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

### The order is a rule, not a diagram — no parallelizing fairy and colleague

Lei's ruling, in-session, 2026-08-24 morning (relayed by Kepler; twin of
umwelt#732, filed here as #2219 per the duplicated-by-hand rule below). The
two-layer gate above is a **sequence**, not two boxes listed in order that
happen to both need doing. In practice seats have been parallelizing them —
asking the counterpart for the colleague pass before, or at the same time as,
the reviewer-fairy round — and merging on a colleague approval that landed
first, without the fairy round ever converging, or running at all.

**Named anti-pattern: a colleague approval obtained while the fairy round is
absent or unconverged does not count as the colleague box.** The colleague pass
starts only once the reviewer-fairy round has converged on the tip being
reviewed — not before, not in parallel, no exception for a PR that requests
both at once. Lei's reasoning, stated because it should carry the rule rather
than just accompany it: (1) the fairy round is *within the author's own
control* — a self-check by a fresh-eyed, context-less reviewer that interrupts
nobody's workflow, a sanity check and a courtesy to peers, not a cost worth
working around. (2) Peer review *assumes* fairy convergence — implementation
most-likely-correct — so the peer spends their read on what a context-less
fairy structurally cannot catch: subtle tensions with other in-flight designs,
team conventions, the larger picture. Sending a PR to a colleague before that
assumption holds wastes the colleague's attention on ground a fairy would have
covered. (3) Parallel runs void each other — a tip moved by fairy-induced
fixes invalidates whatever the colleague already read, forcing a re-read on a
tip that no longer matches what was reviewed. Parallelism here is not an
efficiency gain; it is coordination chaos with the appearance of speed.

**Why this landed as a rule and not a suggestion.** umwelt#559 reached the
merge seat with a colleague-approved review and no reviewer-fairy round ever
run — caught before merge only because the seat read the PR's actual review
record instead of trusting the approval. dsh-engram#41's colleague pass began
with the reviewer-fairy box entirely empty; the colleague disclosed this
himself on picking it up and dispatched a fairy to backfill it alongside his
own read, which caught a load-bearing defect his own pass had just called
correct — a real catch, but **the recovery for a PR that had already gone out
fairy-less, not the designed path.** The designed path is that the PR never
goes out for peer review without fairy convergence in the first place; #41's
parallel backfill is what to do once that has already failed, not a pattern to
reach for by default.

umwelt#721 (the umwelt twin's own predecessor fix) is a **milder instance of
the same gap, not a case of the box being empty**: a reviewer-fairy round
genuinely ran and found a real blocker, the author fixed it, and the colleague
approved about ninety seconds later — with no fairy re-confirmation that the
fix actually converged. That is exactly the condition the mechanical check
below exists to catch, and it is worth stating precisely because an earlier
draft of this citation got it wrong in the opposite direction: it claimed #721
had *zero* fairy reviews at all, checked via the formal-reviews API alone
(`pulls/<N>/reviews`) — which misses that repo's fairy rounds, since they post
as PR **comments**, not formal review objects. Two independent reviewer-fairy
rounds (on this PR and its umwelt twin) caught that mischaracterization before
it shipped, which is its own small demonstration of the rule this section is
arguing for.

**Mechanical check at the merge seat.** Before merging, confirm a reviewer-fairy
round *exists and converged on the tip being merged* by reading the PR's own
review/comment record directly (`gh pr view <N> --comments`, or the REST
fallback documented below if that command's `gh` version is affected) — **never
infer it from a colleague approval being present.** A colleague approval is
evidence the colleague pass happened; it is not evidence the fairy pass
happened first, or at all.

**Check both surfaces, not just the one that's easiest to query.** Fairy
rounds in this repo post as **comments**, not formal GitHub reviews — checking
only `pulls/<N>/reviews` (formal review objects: colleague approvals,
`CHANGES_REQUESTED`) reads as "no fairy round" even when one genuinely ran,
which is the exact mistake this PR's own draft made about umwelt#721 before
its own reviewer-fairy round caught it. `gh pr view <N> --comments` (when
working — see the version-hazard blockquote below) already merges both
surfaces into one read; a hand-rolled REST fallback that only hits
`issues/<N>/comments` does not, and neither does a `--json reviewDecision`
shortcut that only sees formal review state.

Cheap to check, and the check is the whole point: it
replaces inferring readiness from the *shape* of the review record with
actually reading it.

### Stacked PRs (`gh-stack`) — the merge verb changes, and nothing in training knows it

GitHub Stacked PRs (public preview 2026-07-30; official extension
`github/gh-stack`) is **post-cutoff for every model on the team** — no session
knows it exists unless a surface carries it. This section is that surface
(Lei, 2026-08-29). Install is **per-user**: `gh extension install
github/gh-stack` — only then does `gh --help` list `stack` under EXTENSION
COMMANDS; verify with `gh extension list`. All facts below measured live on
the 2026-08-29 cascade train (alpha #2373/#2382).

- **Link existing PRs into a stack**: `gh stack link <pr1> <pr2> …`.
- **A stack member REFUSES plain `gh pr merge`** ("must be merged using the
  asynchronous merge REST API"). The merge verb for stack members is
  `gh stack merge <pr> --squash --yes`. Don't fight the refusal — it's the
  signal you're on a stack.
- **After a layer merges, GitHub auto-rebases the next layer itself** — no
  push from any seat; patch-id identical to a hand `rebase --onto`; the
  force-push is attributed to the merger. Expect it, and **verify with
  `git range-diff`/patch-id rather than assuming** it happened or that it
  changed nothing.
- **Cascading rebase of un-merged layers**: `gh stack sync` / `gh stack
  rebase`. Machine-readable status: `gh stack view --json`. Stacks are
  API-readable (REST `stack`, GraphQL `PullRequestStack`) but not
  API-mutable.
- **Ruleset interaction**: with `require_last_push_approval=true` on the
  target branch, the seat that syncs/rebases **cannot** be the tip
  re-affirmer — a teammate re-approves (this includes GitHub's own
  auto-rebase being attributed to the merger). **On this repo it is `true`
  on `dev`, where stacks live, and `false` on `main`** (measured
  2026-08-30 — the two protected branches are configured oppositely). And
  `dismiss_stale_reviews_on_push: true` on `dev` means a content-changing
  push **dismisses** the existing approval — it is gone, not merely
  un-renewable by the pusher; whether GitHub's patch-id-identical
  auto-rebase migrates or dismisses it is unmeasured, so read the approval
  back after every layer rather than assuming.

> **Duplicated by hand in engram-alpha, umwelt, and soma `CLAUDE.md`** (Lei,
> 2026-08-29: whichever repo a session starts from must load it), with no
> generator or test binding them. Change one, change all three in the same
> pass — **but the ruleset paragraph and any repo-specific merge-authority
> clause are deliberately per-repo; sync the rules, not the local facts.**

### PR inheritance — read the open PRs first; make status readable from the LIST

Three parts of one rule (Lei, 2026-08-14). Applies in single-agent mode too — checking
open PRs guards against your own stale branch from a prior session, not just a
counterpart's work. All three exist because **a list read and a thread read are
different operations, and only the list read scales**: nobody opens ten comment
threads to reconstruct status, so status living only in comments is invisible at the
altitude people actually work at.

**1. Before starting: `gh pr list --state open`, before the first commit of any new
task.** A read of the actual list, drafts included. If an open PR already does part of
the job, branch off *that PR's tip* rather than off `dev`. Never push additively into
another agent's branch — cut your own from their tip; editing their artifact has no
additive exception.

*What it actually buys, stated honestly:* run here the day it was written, it surfaced
**#1320 / #1415** — both @sol-montaigne's, both adding `engram_check_independence`,
both `closes #1313`, opened a day apart. But that pair was **already known**: Sol
marked #1320 superseded on 2026-06-25, and it is open today because **Lei parked it**
past `v0.3.2-rc1` (his ruling, relayed onto the PR by me on 2026-08-03 — not a comment
of his own; flagged because this section is otherwise careful about relay-vs-first-hand). So the check does not usually find *unknown* duplication. It finds
**known duplication nobody is on the hook for** — the more common failure, and the one
that persists for months.

**2. When you replace a PR, mark it at fold-down time — in the TITLE, not only a
comment.** A fold-down (opening a PR that absorbs an existing one, usually by branching
from its tip) is legitimate. The absorbed PR looking untouched in the list is not.
Prefix the replaced PR's title:

```
[SUPERSEDED by #<N> · commits carried] <original title>
[SUPERSEDED by #<N> · commits dropped] <original title>
[SUPERSEDED by #<N> · commits partly carried] <original title>
```

State, superseding PR, commit disposition. **Prefix, not suffix:** `gh pr list`
truncates the title **from the tail**, and there is no fixed threshold — the title's
budget is terminal width *minus* what the ID / BRANCH / CREATED-AT columns consume in
that listing, so a long branch name on someone else's PR shrinks everyone's visible
title and the same terminal cuts differently on different days. At ~80 columns only
the first ~25-30 characters survive; even at 160, titles past roughly 90 characters
are routinely cut. **The invariant is the direction** — what is lost is lost from the
end, so a leading marker survives every width and a trailing one goes first.

> *An earlier draft asserted a "~78 chars" threshold as measured. It was an artifact
> of a `jq '.title[0:78]'` slice I wrote myself, mistaken for `gh`'s behaviour —
> fabricated evidence for a true conclusion, inside the section correcting that habit.
> A reviewer-fairy falsified it with a pty harness at several widths.*

**Who may set it: the merge seat**, whoever holds it (Lei, 2026-08-14 — the seat moves
with availability, the authority travels with it). A deliberate, narrow exception to
*never edit another agent's artifact*: **status annotation only, never a rewrite of
their title's substance or description.** It exists because whoever knows a PR is
superseded is usually not its author. **Also comment on the replaced PR** — the title
is the mechanism, but the comment is the half that reaches the human, and the human
needing to know their work was carried forward is the reason this rule exists at all.

> **⚠ `gh pr edit --title` and `gh pr view --comments` may fail while looking
> like they worked — CHECK YOUR VERSION.** `--title` on `gh 2.45.0` **exits 1**,
> prints only `GraphQL: Projects (classic) is being deprecated …`, and leaves
> the title unchanged; it works on `gh 2.92.0` (ariadne-calvino, umwelt PR #97,
> 2026-08-14 — exit 0, title changed) and `gh 2.97.0` (exit 0, returns the PR
> URL). Three scattered data points, not a boundary — don't infer a `--title`
> fix boundary from `--comments` below. `--comments` is the dangerous one: it
> **exits 0 and prints nothing** on a PR that has comments, reading as "no
> comments" rather than a failure — I hit this and believed an 8-comment PR
> had none. Bisected 2026-08-17, 15 `.0` releases on one seat (16 including
> ariadne-calvino's 2.92.0), monotone, no flapping: 11 non-consecutive `.0`
> releases tested (2.45.0, 2.56.0, 2.62.0, 2.65.0–2.72.0), all broken —
> working from 2.73.0 (also 2.74.0/2.80.0/2.97.0, plus 2.92.0 on
> ariadne-calvino's seat). State the boundary as **2.72.0/2.73.0**,
> not "the fix" — only `.0` releases were tested, so a fix inside a `2.72.x`
> patch isn't excluded. Fallback for `--title`, **measured working on 2.45.0**
> (the broken version): `gh api -X PATCH repos/<owner>/<repo>/pulls/<N> -f
> title="…"`. It goes through the REST surface, not the GraphQL one that broke
> `--title`, so it should hold on other versions too — reasoning, not a second
> measurement. Fallback for `--comments`: `gh api
> repos/<owner>/<repo>/issues/<N>/comments --jq 'length'` — measured
> version-independent, returning `8` against `engram-agents/umwelt#559` on both
> `gh 2.97.0` and `/usr/bin/gh` 2.45.0, the broken version. It is **incomplete,
> not merely caveated** (caught by ariadne-calvino's colleague pass, not by me
> or either reviewer-fairy): `issues/<N>/comments` returns conversation
> comments only, missing review submissions (`pulls/<N>/reviews`) and inline
> review comments (`pulls/<N>/comments`), both of which `gh pr view --comments`
> shows — on `engram-agents/umwelt#559`, `issues/559/comments` → 8,
> `pulls/559/reviews` → **2**, `pulls/559/comments` → 0, so a reader who stops
> at `8` misses two review verdicts. It is also outage-dependent, not just
> version-independent: this is the same `issues/<N>/*` REST family that
> returned 404 for pull requests during GitHub's 2026-08-17 incident window
> (13:40Z–~16:11Z). **Read results back either way** — a deprecation-shaped
> message and a silent empty result both look like something other than a
> failure.

**Why the rule exists, told accurately.** The originating case is umwelt#561, and the
honest version is *not* that its author was left in the dark: link-backs were posted
twice, the second thirty-six seconds after umwelt#584 merged, naming the carried
commits. What lagged was the PR's **state** — `OPEN` for ~15 hours after its work was
in `main` — and an auditor read the list, saw it open, and called it abandoned, having
written both comments himself. **The defect was never missing communication; it was
communication living where a list read cannot see it.** Hence a title marker and a
prompt close, not another comment.

> **Duplicated by hand in umwelt's `CLAUDE.md`**, with no generator or test binding
> them — each file loads only with its own project. Change one, change the other in the
> same pass; the failure mode is a rule current on the copy you are reading and stale
> on the one governing the repo you are in.

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
   `/home/agents-shared/forum/projects/pool-X.md` with the GitHub anchor set. The
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

- Baton files: `/home/agents-shared/forum/projects/<id>.md` (YAML frontmatter + body).
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

Three checks, run in order — and know their standing: checks 1–3 are text
checks over enumerated close forms, i.e. **conveniences**; the **gate** is the
post-merge completeness read at the end of this section, which is downstream
of GitHub's parser and cannot be blindsided by a form nobody enumerated
(Borges's framing, 2026-08-22, adopted as the rule after the umwelt#674/#632
incident below).

1. **Auto-close intent fires** (when the PR DOES close an issue): the body
   must contain a close-keyword referencing the target.
   ```
   gh api graphql -f query='{repository(owner:"engram-agents",name:"engram-alpha"){pullRequest(number:<PR>){closingIssuesReferences(first:5){nodes{number}}}}}'
   ```
   The full API envelope is `{"data":{"repository":{"pullRequest":{"closingIssuesReferences":{"nodes":[...]}}}}}` —
   look for `nodes: [{number: <N>}]` where N matches the title's `[closes #N]`.
   `"nodes":[]` (empty) means the body is missing the keyword form and the PR
   will NOT auto-close **via the body** on merge; rewrite via
   `gh pr edit <PR> --body "..."`. **An empty array says NOTHING about commit
   messages — see check 3, which covers the path this query cannot see.**

   **This query lags a `gh pr edit --body`/push — measured in EITHER
   direction, never trust the very first result after an edit.** Both
   measured live on exam-room's issue 29 / PR#30 (2026-08-23, a different
   repo, same shared `gh`/GraphQL tooling), with different, non-interchangeable
   timing evidence for each direction — stated precisely because an earlier
   draft of this note attributed the tightly-timed measurement to the wrong
   direction, caught by a reviewer-fairy:

   - **False BLOCKER, tightly timed.** A body edit that removed every
     close-keyword (a genuinely GOOD edit) queried as **non-empty** —
     reflecting the stale, pre-edit bad state — immediately after the push.
     A second query exactly **5 seconds later** correctly read empty. This is
     the one pairing with a controlled, measured interval: 5 seconds was
     enough to resolve in this instance, but that is one data point, not a
     guaranteed bound.
   - **False SAFE, loosely timed.** Separately, a body edit that still
     carried a negated close-keyword (a genuinely BAD edit — negation
     doesn't help, per the trap documented above) queried as **empty**
     immediately after its own push. The same untouched text was only later
     found to correctly read non-empty, via a check made after several
     unrelated actions had intervened — the true gap is unmeasured, but was
     clearly much longer than a few seconds.

   Query, wait — at least 5 seconds, and longer if anything else is
   happening in between — query again, and trust only the settled answer.
   A single-shot check right after editing is not evidence either way, and
   do not assume the delay is short just because it resolved quickly once.

2. **No trap-shape auto-close** (always): every body match must reference the
   SAME `#N` the title carries.
   ```
   gh pr view <PR> --json body -q .body | grep -iE '\b(close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s+#?[0-9]+'
   ```
   Inspect every match. Any reference to a different issue number is a trap
   — rewrite via `gh pr edit <PR> --body "..."` with the trap shape removed
   (use the safe-paraphrase table above).

3. **No commit-message auto-close** (always): `closingIssuesReferences` reads
   the PR BODY only — a merge commit's own message is a THIRD close surface
   GitHub's parser reads independently, invisible to checks 1 and 2. **This
   repo's exposure is currently LOWER than umwelt's, and say so precisely:**
   engram-alpha's `squash_merge_commit_message` is `BLANK` (measured
   2026-08-22 — three real recent squash merges carry no branch-commit
   content, only PR title + co-author lines), so branch commit messages do
   NOT ride into the merge commit here today. The check stays anyway, as
   defense-in-depth against the three ways the exposure appears without any
   code change: a repo-settings flip to `COMMIT_MESSAGES` (silent, and
   exactly what makes umwelt exposed), a merger hand-editing the squash
   message at merge time, and copy-paste of commit text into a PR body.
   The incident that makes this real is umwelt#674/#632 (2026-08-22): empty
   array there, merge concluded safe, the issue closed **by the commit**,
   silently retiring the half of a two-defect issue the PR didn't fix
   (umwelt#675 is the recovery). The three-surface diagnosis and this remedy
   were first specified in Ariadne's graph on 2026-08-05.
   ```
   git log origin/dev..HEAD --format='%B' | grep -inE '\b(close[sd]?|fix(e[sd])?|resolve[sd]?) +([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)?#[0-9]+'
   ```
   Every match must reference the SAME `#N` the PR intends to close — anything
   else gets the commit reworded (rebase) or accepted deliberately, never
   discovered post-merge. The pattern covers the `owner/repo#N` cross-repo
   close form (the first draft missed it — exercised five ways by Ariadne,
   2026-08-22); bare `reponame#N` stays unflagged because it never closes
   anything. Two things are deliberately unasserted rather than claimed:
   `Closes: #N` with a colon (unmeasured), and **whether a close keyword in
   the squash-commit SUBJECT fires the parser** — relevant here because this
   repo's `squash_merge_commit_title: PR_TITLE` puts the PR title into the
   subject, a path the commit-range grep cannot see. The 2026-05-31
   measurement (7/7 title-only PRs did not auto-close) suggests subjects
   don't fire, but it predates verification of the current setting — exactly
   one of "the setting changed since May" / "subject keywords don't fire" is
   true, and nobody has separated them. Measure on the next real
   `[closes #N]` merge before claiming either way.

**When reviewing a PR** (reviewer-fairy or counterpart colleague):
- Title missing `[closes #N]` on a PR that addresses an issue → blocker.
- Title `[closes #N]` present but body missing the matching close-keyword →
  blocker (PR will not auto-close on merge despite the title intent). Author
  edits body via `gh pr edit <PR> --body "..."` before merge.
- Body contains close-keyword referencing an issue OTHER than the title's `#N`
  → blocker (trap). Author rewrites the trap-shape via safe-paraphrase table.
- A branch commit message carries a close-keyword referencing any issue other
  than the PR's intended `#N` (check 3's grep) → flag it: reworded via rebase,
  or accepted deliberately with the exposure noted — never left undiscussed.

This triple check is mechanical; checking is cheap; the cost of missing it is
either a stale-issue backlog (missed auto-close) or a silently-closed-wrong-issue
(trap firing).

**THE GATE, at the merge seat, after merging: list which issues actually
closed and confirm each one is FULLY done.** A close link's existence is not
the issue's completion, and this step is mechanism-independent — it catches
body links, commit links, and whatever close path ships next. A regex miss
before merge is silent and permanent; a completeness-read catch after merge
is loud and recoverable (umwelt#675 is the live proof).

**GitHub recognized closing keywords (case-insensitive):** `close`, `closes`,
`closed`, `fix`, `fixes`, `fixed`, `resolve`, `resolves`, `resolved`. This repo
uses `closes` only — both `[closes #N]` in title and `Closes #N.` in body.
Other keywords technically work but fragment the convention; if a reviewer
sees a non-`closes` form, flag as a blocker and ask the author to rewrite.
