# CLAUDE.md — ENGRAM project conventions

Developer-facing conventions for contributing to engram. Identity-level
instructions live in the user-level CLAUDE.md.

**Convention for this file (#3322).** A section states the rule, its one-line
origin (date + who ruled), and a pointer to where the evidence lives. Dated
measurement history lives in `docs/internal/`, not here. Budget: at most 75,000 bytes
today (CI step `claude-md-budget`; 40,000 is the round-2 target of #3322, reached by
tightening rule wording, not by moving more); moved blocks are listed in
`tools/claude_md_moves.json` and checked by `tools/claude_md_moves_check.py`.

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

- **`dev`** is the merge target for ALL feature/fix PRs. Always open PRs
  against `dev` unless an explicit release-cut is happening.
- **`main`** is GitHub's default branch and holds releases (a plain clone lands
  on a tagged release, which the installer needs). It moves only on explicit release
  cuts; routine PRs do NOT target main. **A release cut = opening a formal
  PR from `dev` → `main`** — this fires the full CI gate (`tests.yml` full
  suite + `pr-checks`), the thorough pre-release
  validation. See `docs/internal/RELEASING.md`.
- **CI cost model (#1597):** dev PRs run only the cheap combined `pr-checks`
  (static scrubs). The **full pytest suite does NOT run
  on dev PRs** — at agent-PR velocity it's too expensive; run **affected-tests
  locally** and paste the result in your review. The full suite runs nightly on
  `dev` (`nightly.yml`) and on the dev→main release PR (above). `affected-tests.yml` is `workflow_dispatch`
  only.
- **No macOS runners in CI — ever (Lei, 2026-09-28).** A `runs-on: macos-*` job
  bills at **ten times** the Linux minute rate, and `pr-checks` fires on every
  PR event. **macOS coverage comes from the Mac seats**: the test stays in the
  suite and skips on Linux; a seat on a real Mac (Aleph, Clio) runs it locally
  whenever a shipped shell script changes and pastes the result in the review,
  exactly like affected-tests above. If a future change genuinely needs a hosted
  macOS runner, it is Lei's call, opened as a question with the per-run cost
  stated — never added inside a fix PR. (Billing measurement:
  `docs/internal/CI-COST-MODEL.md`.)
- **Whole-minute billing (Lei, 2026-10-03).** GitHub bills CI in whole minutes rounded up, so an addition that keeps the whole pr-checks run under one minute costs nothing extra.
- Verify before specifying a PR base: recent merged PRs
  (`gh pr list --state merged --json baseRefName`) all show `dev`.
  `gh repo view --json defaultBranchRef` returns `main` by design, so a banner
  naming `main` as "Main branch" is expected; PRs still target `dev`.

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

**Agent coordination is not a tier or a flag** (#4255): inter-agent letters /
turn-state batons / the shared forum — their CLIs, prompt-hooks and skills, and
the CLAUDE.md template's "Agent coordination" section — are tagged in the manifest
like everything else.

**Two load-bearing rules:**

1. **A safety discipline ships with its tool, at the same tier.** If *misuse* of a
   tool is harmful (not merely "unused-is-benign"), its usage-discipline skill is
   the same tier as the tool — they are one inseparable unit. (This is why the
   trust-tier MCP tools **and** the `engram-trust-tier` skill are all T1: a safety
   tool used without its discipline is worse than not having it.)

2. **Every shippable is tagged exactly once in the manifest.** The tier map **is**
   the single source of truth at `src/build/packaging/tiers.json`, and
   `build-plugin.sh --tier` filters through it at **build time** (the
   agent-led install reads the README, prompts the user for the tier, and
   builds accordingly — no post-install pruning). **CI fails the build if any
   shippable path is untiered or any manifest entry is dangling** — so "added a
   skill, forgot to tier it" cannot drift past review. This is a mechanical gate,
   not vigilance.

*(All three parts are LIVE: the
manifest exists; `src/build/engine/build.py` and `cli.py` resolve it at build
time, and the CLI's own help calls it "the single source of truth"; and
`pr-checks.yml` runs the `manifest-gate` step on every PR. Behavioural
confirmation, not a code-read: on 2026-09-04 that gate blocked a PR for a new
module its author had not tiered. So the tier of a new artifact is not merely a
design decision to record for later transcription — **add the entry in the same
PR that adds the artifact, or CI will stop you.**)*

## Contract-first tests — write the assertions before the implementation

**Lei's ruling, 2026-09-19 (engram-alpha#3045).** For a function with a stateable
contract, **write the tests that assert its designed behaviours — including its error
handling — before implementing it.** The tests exist first; the implementation is then
written against a suite that already fails. Drift from the design is caught
mechanically, rather than depending on the implementer *remembering* what was decided
earlier.

**The gap this closes is specific, and it is not "we skip design."** We already write
design docs that state behaviours, failure contracts and invariants — but **prose does
not execute.** A design doc can say a caller must distinguish three states when the
owner is down, and nothing fails if the implementation collapses them into two. The
statement and the check live in different artifacts, and only one of them runs.

**The originating case:** a batch fairy silently dropped one of fifteen nodes and
`cohort_dispatch validate` still reported `clean` and exit 0 — "every node sent
comes back or is reported" was never asserted. Review is a weak instrument
against a missing assertion (there is no wrong line to look at), so the check
belongs in the test suite. The case and its qualifications:
`docs/internal/CONTRACT-FIRST-TESTS.md`.

**Scope — where it applies:**

- Functions with a **stateable contract**: the `cohort_dispatch` stages, the cascade
  verbs, the baton gates, anything with a named invariant or a failure contract.
- **Error handling especially.** The happy path tends to get exercised by ordinary use;
  the refusal paths are where behaviour silently goes missing, because nothing calls
  them until something goes wrong.
- **And see the spec-author obligation below** — this block says *where the discipline
  applies*; it does not by itself say *who has to act* for it to fire through a fairy.

**Where it does NOT apply:** exploratory work where the design emerges from coding.
That work is already excluded from fairy delegation for the same reason — if the
contract isn't known yet, a test written first encodes a guess, not a decision.

**The obligation is on the SPEC AUTHOR, and this is the half that makes the discipline
reachable at all.** This project's delegation rule is *"ALWAYS delegate the actual code
writing to a fairy. No size exception."* So nearly all code arrives through a fairy, and
a fairy can only gate on **the spec text in front of it** — "does this function have a
stateable contract?" is a property of the function, which a fairy cannot evaluate
without inventing the contract, and inventing one is exactly what the scope bound
forbids. Therefore: **when dispatching a coder-fairy for a function with a stateable
contract, state the contract in the spec.** A fairy handed a spec that omits it will
correctly skip the step — so **an unstated contract is an unimplemented one**, and the
omission is the dispatcher's, not the fairy's.

**One caveat, and it is the failure mode to expect.** A test written at design time can
still be **non-discriminating** — asserting the happy path and passing against a
plausible-wrong implementation. Writing it early does not exempt it from the
can-it-fail discipline this repo has already paid for **more than once**: reverting to
the pre-fix code and watching the test go red proves the *code* was broken, not that the
test discriminates.

Instances of a non-discriminating control, in the review threads of #2614 and
#2721 (not in what those PRs were about — say so when citing them), are in
`docs/internal/CONTRACT-FIRST-TESTS.md`. The stronger control is a mutation where **the
new assertion fires and the pre-existing checks stay green.**

**Adoption status: adopted (Lei, 2026-09-30, in session, verbatim: "let's keep the
contract tests convention").** The trial ran on one case, #3045's fix, merged as #3235
(`307bdd04`), and ended with that ruling.

The one trial case (#3235) found both a defect the tests caught and two they did
not. **The convention is a complement to the review rounds, never a replacement
for one.** That is n=1; the narrative is in `docs/internal/CONTRACT-FIRST-TESTS.md`.

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
  back to a **walk-parents search for the actual target file, stopping at the
  file's own tree root** (the nearest ancestor containing `engram_core.py`: the
  plugin root installed, `src/engram/` in source); never search above it, or a
  tier-filtered build can match a stale copy in an ancestor (#3983). Never a
  fixed `parents[N]` (the one exception: a file looking for `engram_core.py`
  itself uses a fixed window of its own directory and its parent). The defect is
  any upward walk, in any spelling: `.parents` however used (`roots.extend(...)`
  counts), a `.parent`/`dirname` loop, in Python or shell (`start-engramd-owner.sh`
  had two). A new one fails `tests/test_no_unbounded_upward_walks_3983.py`
  unless bounded or classified there. See
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

For our `directory`-source local marketplace, Claude Code runs hooks / skills /
tools and the MCP launcher **directly from the marketplace SOURCE tree**
(`~/.engram/marketplace/plugins/engram/`), and `CLAUDE_PLUGIN_ROOT` resolves
there. The `~/.claude/plugins/cache/.../<version>/` snapshots and
`installed_plugins.json` `installPath` are version bookkeeping, NOT the live
load path — **except for AGENTS (sub-agents / fairies), which load from the
`installPath` cache snapshot.** Two distinct fields: marketplace
`installLocation` (`known_marketplaces.json`) = source; plugin `installPath`
(`installed_plugins.json`) = cache snapshot. Hooks/skills/tools follow
`installLocation`; agents follow `installPath`.

What each edit needs (the OPPOSITE of the official-marketplace model):

| Edit | Takes effect |
|---|---|
| hooks / tools / skills | live once `tools/install-local-marketplace.sh` rebuilds the source tree; no `/plugin update` (hooks re-read the source file per prompt) |
| agents (`agents/*.md`) | `/plugin marketplace update engram-local` + `/reload-plugins` (a restart also works); a stale-cache agent fails **silently**, so verify by dispatching one and confirming its tools |
| MCP server (`server.py`) | the path is the source but the process is from session start: `/mcp` reconnect or restart |
| `hooks.json` registration | `/reload-plugins` re-reads it (measured for an addition and a removal); a full restart only if a registration demonstrably does not take effect |

**To know which code is running, instrument the live artifact — never
infer from a proxy** (a version string, `installed_plugins.json`, or a bash-env
test will mislead). Add a stderr/file sentinel to each candidate hook copy, fire
a REAL prompt, and read which copy logged; `CLAUDE_PLUGIN_ROOT` read from
*inside* a live hook is the other ground truth.

*Undocumented upstream — re-verify after any Claude Code version bump.
Measurements (2026-06-28, #1639, #1892, #3265) and the full system model:
[`docs/internal/claude-code-plugin-system.md`](docs/internal/claude-code-plugin-system.md).*

## Hook injection budget — every `additionalContext` block must be bounded

Lei's ruling (2026-09-12, engram-alpha#2863): every hook's `additionalContext`
must be (1) **bounded** under a named per-harness budget, (2) **never load
content of unpredictable size**, and (3) **carry its highest-value summary in
the first ~2,000 characters** — the harness silently truncates the TAIL of an
oversized injection, and different harnesses (Codex, DeepSeek) have different
ceilings. Mechanism: `_hooklib.emit_context()` (the one shared bounded
renderer — trailer leads, never exceeds the hard per-harness `budget`) plus
`_harness.py` (`get_harness_budgets()`, the per-harness numbers table —
unmeasured harnesses get an explicitly-named conservative floor, never
claude-code's numbers). New hooks route `additionalContext` through
`emit_context()` rather than hand-rolling a bare join/f-string block.

## PR merge authority — the substrate-feature gate

Once a PR is merge-ready (next section), **who merges it** depends on what it
changes: **substrate-concept features → Lei reads the PR first**, unless the PR
implements a design he already read (Lei, 2026-09-27 — then the team merges
after a written conformance check); **fixes + non-substrate → the team merges
after peer review**, and since 2026-10-22 the **project owner presses their own
converged PR** under the policy's written *Owner merge* gate (Lei, in session: the
colleague's recorded run on the exact head is the test gate; the owner's scratch
merge on the current base and the base-SHA assertion stay; CI is the floor, since
`pr-checks` runs only a fixed set of guard test files on `dev` PRs, never the full
suite). This gate is
**engram-only** — other repos (umwelt, soma) carry their own delegated
authority. During an explicit Lei-absence day-grant, a settled substrate PR may
be merged to `dev` (revertible) while the irreversible live-deploy stays Lei's.

This is a pointer, not the policy. The single source of truth — the full rule,
the substrate/non-substrate discriminator, per-project scope, the day-grant
carve-out, examples, gray-area calls, and the baton turn-target corollary — lives
in **[`docs/internal/PR-MERGE-POLICY.md`](docs/internal/PR-MERGE-POLICY.md)**. Read it there.

## PR merge-readiness — CI-green gate

A PR is merge-ready only when it is **review-converged AND CI-green on the
current tip**, with or without a colleague. (With a colleague: it
gates the `baton flip → <maintainer>` below; without one: it gates surfacing the
PR as ready.) Before presenting a PR as merge-ready:

```
gh pr view <N> --json statusCheckRollup,mergeStateStatus
```

**Read `mergeStateStatus` first, the rollup only if it is not `CLEAN`.** `CLEAN`
is sufficient to conclude green. Anything else (`UNKNOWN`, `BLOCKED`, `BEHIND`,
`DIRTY`, `UNSTABLE`) is NOT evidence of red: fall through to the *deduped*
rollup. (`DIRTY` is a merge conflict and must be resolved whatever CI says.)

1. **`CLEAN` is the verdict — stop there.** GitHub computes it from the latest run per check.
2. **The rollup does NOT dedupe.** A stale `FAILURE`/`CANCELLED` from a superseded same-SHA run sits beside the current `SUCCESS`; requiring every entry to be `SUCCESS` false-blocks a green PR.
3. **Two runs of one check can land on the SAME SHA** (the workflow fires on `edited` as well as `synchronize`). The dedupe is load-bearing within one SHA only.
4. **Use the rollup only to find WHICH check to read**, deduped to the latest run per check name, where *latest* = an unfinished run (status not `COMPLETED`) if the name has one, else the most recently **completed** (`completedAt`, not `startedAt`). `gh run list --branch <branch> --json name,headSha,conclusion,updatedAt` shows the current run. But see rule 3 of "Merge-seat test rules" below: a cancelled same-SHA run can still block the merge; rerun it, unless the PR body or title changed since that run, in which case a rerun replays the old payload, so trigger a fresh run instead.
5. **`UNKNOWN` is not red** — it is computed asynchronously. Treat it as *pending* (hold, re-check next tick), never as a failure.

Reference implementation: `_pr_ci_state()` and `_dedupe_checks_by_latest()` in
`src/engram/tools/baton.py`. Read the rule off what they do; if doc and tool
drift, the doc is the side that moves.

- **Green** → present as merge-ready.
- **Pending / running** → do NOT present as ready yet. Hold (keep the turn with yourself), **re-check on the next loop tick**, advance only once green.
- **Failed** → a **blocker**: back to the author for a fix round. Never present a red PR as ready.

**Re-check after ANY post-approval commit.** A moved tip (e.g. folding a late
reviewer nit after the approve) re-triggers CI and can fail on the new commit —
CI-green must hold on the tip the maintainer would merge.

**The merge seat's own test run** is on a scratch merge of the base tip plus the PR
head, never the head alone; a scratch-merge failure counts against the PR only if
that cell is green on the base alone (or the PR adds it); and a `CANCELLED` same-SHA run can
block the merge even when another run passed — rerun it (a fresh run instead if the PR body or title changed since that run). A red only the whole run produces
(green on base and merged tree alone, in a file no PR touches) is a filed load flake, not a
PR red; and the press step never presses on a printed `UNKNOWN`: it waits for `CLEAN` or reads
every run of the required check on the head. Commands and evidence:
`docs/internal/PR-MERGE-POLICY.md` § "Merge-seat test rules".

*Origin: converged-but-red PRs reached "ready" framing twice (CI was not
checked); amended 2026-08-30 after superseded same-SHA runs (#2410, #2419) read
as not-green. Measurements: `docs/internal/PR-MERGE-POLICY.md` § "PR merge-readiness —
CI-green gate (both modes)".*

## PR review convention (when the PR has a colleague)

When a counterpart ENGRAM agent is active (e.g. Ariadne + Borges) and the shared
`/home/agents-shared/inter-agent/` channel exists, PR review is **reciprocal**: an agent's own PRs get a colleague
fresh-eye from the counterpart agent, not only from the agent's own
reviewer-fairy.

The **"colleague converged"** gate is two layers, both required before a PR
enters the maintainer's merge queue:

1. **Reviewer-fairy** (`engram-pr-reviewer`) — iterate to convergence (PR-MERGE-POLICY gate 1)
   *before the PR goes to the peer* — catches mechanical / contract / scope
   issues. (Applies with no counterpart too.) **This is the author's own
   pre-peer self-check, not a re-run gate on every later push** (Lei,
   2026-09-13 20:54 EDT, in-session, verbatim: "before going out for peer
   review, the fairy review is like 'double checking it on your own' ... no
   more fairy review is mandatory ANYMORE" once the PR has reached the peer).
   No fairy delta round, and no patch-identity argument that a later diff is
   "equivalent" to what the fairy approved, substitutes for the merge gate
   below.
   **Cap: three pre-peer fairy rounds per contributed tip** (Kepler, ruled
   for the fleet 2026-09-20, forum #422 — posts 10344 and 10347, refined to
   rule v4 in posts 10354–10356 and closed at 10364). Round 1 is the
   mandatory self-check; rounds 2 and 3 are for blockers the author agrees
   are defects of the artifact. After round 3 the author stops, lists every
   still-open finding in the PR body with one line on why it is or is not a
   defect, and sends the PR to the peer, who reads that list first. A fourth
   round needs a written reason: a blocker whose fix changed more than the
   blocker. The stopping signal is where a finding came from. A finding
   that arrived from a contributor, from use, or from a trap set on a live
   seat is a defect; one that exists only because the previous round's fix
   created it is review-authored, and a chain of those is the process
   reviewing itself.
   **Reset (v4):** a contributed tip is a NON-MERGE commit
   (`git log --no-merges --format='%an'`) whose git author is not the PR
   author, landed after the PR's first review object
   (`gh pr view --json reviews,comments`); it resets the three-round count.
   Merge commits never count regardless of who clicked (a web-UI "Update
   branch" is a merge), the committer field never counts (every squash merge
   here reads `GitHub`), and the author's own fold of a peer finding never
   resets. Named field, named anchor, both read off artifacts, never
   recalled. One duty added when this was written into the repo on
   2026-09-26, not part of the forum ruling: the merge seat records in its
   merge comment when a PR merged under the cap. **This cap qualifies
   "iterate to convergence" above and the "converged" clause of the
   merge seat's mechanical check below:** a PR whose third round still
   returns REQUEST_CHANGES goes to the peer with its open findings listed,
   and that list is the round's stopping point.
2. **Counterpart-agent colleague fresh-eye** — after the fairy converges (or item 1's three-round cap is reached with the open findings listed), the
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

**The merge gate itself**, operationalizing Lei's same ruling: a non-author
peer APPROVE of the PR's current head (what counts: "Approvals after a layer"
below), plus CI green on that same head. A new commit after approval needs a **fresh** peer review
and approval — the peer looks again (the `dev` ruleset already
enforces this mechanically: dismiss-on-push + last-push approval) — never a
fairy re-check or a diff-equivalence argument standing in for it. The merge
seat still checks the approval against the head directly rather than
trusting `reviewDecision` alone: a manual force-push on this repo has been
seen to keep a stale approval alive on a *different* PR (#2338, a manual
force-push, explicitly NOT gh-stack's auto-rebase — see "Stacked PRs" below
for the measurement).

Review hierarchy: **reviewer-fairy → counterpart colleague → maintainer
(merge)**. Symmetric — each agent reviews the other's PRs. Review-mode: `--comment` for in-flight iteration; `--approve`
is posted by a reviewer (fairy or colleague) when converged, never by the author.

<!-- BEGIN team-practice: which-colleague-takes-the-seat (generated -- edit team-practice repo's TEAM-PRACTICE.md, not here; tools/sync_team_practice.py renders it) -->
### Which colleague takes the seat

**If you are the detailed designer or builder of the PR, you are excused from its
peer-reviewer seat** (Lei, 2026-09-09). Nothing weaker disqualifies you — a
contribution in an *earlier session* does not.

| Team | Review each other's PRs | If the pair collaborated on this PR |
|---|---|---|
| Borges + Luria | each other | **Sol**, else Clio |
| Ariadne + Sol | each other | **Clio**, else Luria |
| Aleph + Clio | each other | **Luria**, else Sol |

Escape seats rotate `Luria → Sol → Clio →`; each team asks the next two, skipping
its own. Ask at PR-open time: **was I in the session where this solution was
formed?** Ask it of the *default pairing partner* before naming them at
`baton init` (not first at flip time), and ask it about who *proposed the shape*,
not who reviewed the problem: proposing the fix's shape counts as designing it,
and a review comment that wrote that shape is such a proposal. Proposing the
shape in that session disqualifies; a contribution in an earlier session still
does not, and neither does reviewing only the problem (engram-alpha#3295's review
proposed the shape in the same session, and engram-alpha#3298 was routed to the
escape seat).

- **"Not available" = the seat did not answer.** Send the ask; it *is* the probe.
  Do not check a presence board: `forum online` tracks *prompts processed*, not
  liveness (`engram src/forum/db.py:735`; the inbox endpoint upserts per
  prompt, `src/forum/server.py:900`). A seat that has **never published a
  cadence** falls back to a global 15-minute window and can read offline while
  fully available — our heartbeat floor is 30 minutes and idles to 55 (a team
  convention, not a code fact). A seat that HAS published one is judged against
  its own rhythm, and an on-call seat stays listed while wakeable (#1035) — so
  the board is not uniformly wrong, it is *unevenly* right, which is worse to
  rely on. No answer within one idle cadence plus a turn (~90 min today) → next
  seat.
- **If all three escape seats are silent: try `zephyr`, then `mira`, and stop
  there.** The list is deliberately two names long. Every escape seat is a
  builder seat across two Anthropic accounts, so "all three silent" correlates
  with a platform-level problem that would have taken your own seat down too —
  a longer roster buys almost nothing and costs upkeep it will not get. **Do not
  extend it** (Lei, 2026-09-09).
- **A PR routed to an escape seat names its design posts** — that seat lacks the
  context the pair shared, so the author supplies it.

**Why.** A designer-builder pair can share a blind spot formed *between them,
in-session*; a third seat reconsiders it cold. Nothing weaker disqualifies a
reviewer because sessions reset, and recalled background does not constrain a mind
the way in-session deliberation does — so contamination is **session-scoped, not
history-scoped**. Do not "improve" this by tracking that mechanism more closely: a
compaction is a partial reset *inside* a session, so the role check is the clean
half and the mechanism is the fuzzy one.

**The lineage asymmetry is deliberate.** All three escape seats are builder seats,
so they carry their own pair review plus all escape review. That is the design, not
an oversight: Opus seats focus on design, Sonnet seats on building and reviewing
(Lei, 2026-09-09).

This names *which* seat; it does not add one.

**Adoption status (2026-09-16):** engram, umwelt. Not soma — a routing
choice for Lei and soma's principal, never a structural blocker. (History,
carried from the pre-migration hand-duplicated copies: an earlier version of
this note claimed soma's cohort excludes Sol so the rotation cannot resolve
there; that was read off a stale cohort line in soma's own `CLAUDE.md` and
reported as fact without checking. Measured 2026-09-09 against soma's actual
PR history and collaborator list instead: Sol has authored soma PRs (#52,
#54), and all three escape seats hold push access there, as do both
terminal-fallback seats — the rotation resolves in soma fine. The false claim
is not restored here; recorded so nobody re-derives and re-believes it.)
<!-- END team-practice: which-colleague-takes-the-seat -->

### Conclusion warrant — the gate lives in the fairy's output contract, not here

Neither review layer was assigned **"is this claim true."** The fairy round
checks mechanics; the colleague round checks pattern-level and cross-issue
concerns; a PR could pass both carrying a wrong number, mechanism, or cause.
Adopted fleet-wide 2026-09-03 (Kepler's ruling, forum #385).

**Read this section as documentation of a gate that lives somewhere else:** the
required `## 🔵 Conclusion warrant` section in `engram-pr-reviewer.md`'s output
contract — emitted, or *visibly absent*. Placement is the point: prose telling a
reviewer to verify first is a **preventive** remedy, and preventive remedies get
walked past (no preventive-remedy instance this fleet examined produced a
prevention; cases were collected as they turned up, not sampled, so this is not
a ratio and must not be quoted as one); a required output section is
**additive**, and its omission is a fact someone else can see. These copies document the gate for the human-agent
colleague pass; they are not the gate.

**The box, in this order.** Line 1 is the floor because it is the only line
needing no judgement about what "load-bearing" means, and judgement steps are
exactly the ones that go quietly unexecuted under time pressure:

1. **Re-measure the numbers from your own clone or your own API call** — never
   from the PR's prose — and for each, **state what it ranges over and whether
   that population is the claim's subject.** Unconditionally: a true measurement
   of a *neighbouring* population agrees with itself, so the scope check in
   line 2 — which fires only on a disagreement — is silent exactly there.
2. **If your re-measure DISAGREES with the PR's number, check your own
   instrument's scope before you write the finding.** A different answer is
   evidence about two things, and yours is the cheaper to rule out. The direction
   is predictable and it is the dangerous one: re-measuring yourself, a
   manufactured null reads as reassurance and you stop; re-measuring *someone
   else*, it reads as a finding and you publish.
3. **For each load-bearing claim: what observable would DIFFER if it were false,
   and did anyone check that?** The valuable question and the hard one —
   escalation when line 1 is clean and the claim still feels shaky.
4. **Does each claim sentence, as written, stay inside the bound stated next to
   it?** Read claim and bound *adjacently*, not in sequence. A caveat can be
   stated correctly and violated four lines away, in the same document, by the
   author who wrote it.
5. **For each "checked to fail" / mutation / control claim in the PR: did the
   same mutation also redden tests the PR did NOT add, and did it mutate
   toward the OLD code or toward a plausible-WRONG implementation?** A control
   that has only ever been observed to fail is not yet a control — the
   discriminating case is the one where a neighbouring test stays green while
   the new one goes red, or where the failure comes from a plausible-wrong
   implementation rather than from the pre-fix code itself.

**And a wording rule for review requests, which is not part of the box.** State
the claim, **not the method** — and where an instrument's output depends on who
runs it, say which account and which file it read. Naming your method converts a
second reader into a second copy: two seats independently ran the same
`def`-signature grep and established the signatures were unmodified while the
claim was about the *bodies*, because the request named the method and the second
check inherited its blind spot. Two seats running one instrument are two checks
only when the scope differs, and neither party can see that until they disagree.

**Required-but-often-brief, never judgement-gated.** Most PRs carry few
load-bearing factual claims, so most of the time the box is short — but "fill it
in when the PR seems claim-heavy" reproduces the exact defect this section
exists to fix: a condition has no firing moment, and nobody watches for it.

**A quiet week is not confirmation.** A preventive gate's successes are invisible
by construction, so read the falsifier log — engram's
`docs/internal/conclusion-warrant-falsifier-log.md` — not the silence. It records cases
only, has no denominator, and can never confirm.

*Evidence, the preventive-vs-additive argument, and the SET / OBJECT / AXIS
diagnostic aid (n=4, one agent's carving, not a taxonomy): engram's
`docs/internal/PR-REVIEW-CONVENTION.md` § Conclusion warrant.*

*(Author-side counterpart, already minted separately and deliberately not
restated here: an error site the author can NAME in writing is the author's own
to verify before the PR ships, never a review ask. Cite that node; don't copy it,
or the two prose copies drift.)*

> **Duplicated by hand in engram, umwelt, and soma `CLAUDE.md`**, with no
> generator or test binding the copies. Change one, change all three in the same
> pass. This copy is the condensed form (#3323); the umwelt and soma copies still
> carry the long text until their sibling PRs land, after which the RULE portion
> is byte-identical across all three again and each points at engram's
> `docs/internal/PR-REVIEW-CONVENTION.md` for the history and argument. The gate itself
> lives in exactly one place (`engram-pr-reviewer.md`'s output contract), so
> these three copies document one mechanism, never three copies of it.

### The order is a rule, not a diagram — no parallelizing fairy and colleague

Lei's ruling, 2026-08-24 (relayed by Kepler; twin of umwelt#732, filed here as
#2219). The two-layer gate above is a **sequence**, not two boxes that both need
doing: seats had been asking the counterpart for the colleague pass before or
alongside the reviewer-fairy round, and merging on a colleague approval that
landed first.

**Named anti-pattern: a colleague approval obtained while the fairy round is
absent, or unconverged short of item 1's cap-reached-with-listed-findings
state, does not count as the colleague box.** The colleague pass
starts only once the reviewer-fairy round has converged on the tip being
reviewed — or has reached item 1's three-round cap with its open findings
listed in the PR body, which is the sequence's stopping point — not before,
not in parallel, no exception for a PR that requests both at once. Lei's reasoning: (1) the fairy round is *within the author's own
control* — a self-check by a fresh-eyed, context-less reviewer that interrupts
nobody's workflow, a sanity check and a courtesy to peers, not a cost worth
working around. (2) Peer review *assumes* fairy convergence — implementation
most-likely-correct — or, under the cap, starts from the listed open
findings, which the author has already answered one line each; so the peer spends their read on what a context-less
fairy structurally cannot catch: subtle tensions with other in-flight designs,
team conventions, the larger picture. Sending a PR to a colleague before that
assumption holds wastes the colleague's attention on ground a fairy would have
covered. (3) Parallel runs void each other — a tip moved by fairy-induced
fixes invalidates whatever the colleague already read, forcing a re-read on a
tip that no longer matches what was reviewed. Parallelism here is not an
efficiency gain; it is coordination chaos with the appearance of speed.

**Mechanical check at the merge seat.** Before merging, confirm two separate
things. (1) A reviewer-fairy round *preceded the peer and converged: APPROVE;
COMMENT, no blockers, each actionable suggestion folded (commit named) or
answered on the PR; or item 1's cap; the merge comment says which* (PR-MERGE-POLICY gate 1) (per "The colleague converged gate" above — the fairy
round need not match the tip being merged; a later commit folding peer
feedback does not un-converge it) by reading the PR's own review/comment
record directly (`gh pr view <N> --comments`, or the REST fallback documented
below if that command's `gh` version is affected) — **never infer it from a
colleague approval being present.** A colleague approval is evidence the
colleague pass happened; it is not evidence the fairy pass happened first, or
at all. (2) The actual merge gate — a non-author peer APPROVE of the
current head ("Approvals after a layer" says what counts), plus CI green on it.

**Check both surfaces, and read them whole.** Fairy rounds in this repo post as
**comments**; a round transcribed by a colleague sits in a **review body**.
`pulls/<N>/reviews` alone, `issues/<N>/comments` alone, and `--json
reviewDecision` each miss one of them — hit both endpoints, or use
`gh pr view <N> --comments` (when your `gh` version works; see "PR inheritance"
below).

**A CAPPED read of the RIGHT surface fails exactly like querying the wrong one:**

```
wrong surface            -> hit BOTH endpoints (you cannot know which holds it)
pattern too narrow       -> widen the pattern
TRUNCATED right surface  -> NEITHER helps; only a full read does
```

**THE RULE.** Before you cap a read, ask: **could an item past my cut change this
answer?** If yes, do not cap — no `head`, no `tail`, no character limit, no
`--json` projection that drops the body.

A capped read settles a question only when the slice is known — independently of
the read — to hold everything the answer depends on. If you cannot name how you
know the slice suffices, you do not know it.

Two traps inside "conclusive", both of which have bitten:

- **A bare keyword hit is not conclusive.** A cap landing mid-sentence, right
  after `Blocker: X is broken` and before `— false alarm, retracting`, is a true
  positive of the *literal string* and a false answer about the *claim*. A match
  counts only once you have read to the end of its own clause.
- **Absence is not the only fatal shape.** **Recency** ("is the current verdict
  APPROVE?" — the resolving comment sits past the cap, so a stale blocker reads
  as the live state), **counts and thresholds**, and **latest-state** questions
  all fail the same way, because the truncated output and the true-negative
  output are identical.

*Narratives (umwelt#559, dsh-engram#41, umwelt#721, #2656) and the two wrong drafts
of this rule: `docs/internal/PR-REVIEW-CONVENTION.md` § The order is a rule.*

### Stacked PRs (`gh-stack`) — the merge verb changes, and nothing in training knows it

GitHub Stacked PRs (public preview 2026-07-30; official extension
`github/gh-stack`) is **post-cutoff for every model on the team**; this section is
the surface that carries it (Lei, 2026-08-29). Install is **per-user**:
`gh extension install github/gh-stack`
(verify with `gh extension list`).

- **Link at PR creation — not optional.** The moment you open a PR whose base is
  another PR's branch, `gh stack link <parent> <child> …` (works on open PRs;
  `gh stack link <pr1> <pr2> …` links existing PRs). An
  unlinked stack has none of the protections below: plain `gh pr merge` is NOT
  refused, nothing auto-rebases, and merging the parent with `--delete-branch`
  CLOSES the child instead of retargeting it. An auto-closed child is
  recoverable only while its head is untouched: **restore the deleted base ref
  BEFORE you rebase anything** — a post-close force-push forecloses reopen.
  **Interim (2026-11-11, pending the default-branch decision):** here PRs target `dev`
  but the default branch is `main`, and `gh stack link` retargets the bottom PR to
  `main`. Do not link such stacks; the parent's merge seat retargets each child to
  `dev` before merging. Procedure: `docs/internal/GH-STACK.md`.
- **Membership is a timestamped snapshot, never a standing fact.** Never carry a
  membership read across an interval in which another seat may act, and never pair `--delete-branch` with a
  membership read you have not just re-taken: read the PR's `stack` field AND its
  events API (`added_to_stack` / `removed_from_stack`) immediately before
  merging. The merge attempt is authoritative (believing a member is not one
  earns a refusal; believing a non-member has no dependents orphans them). A
  local `gh stack view` can claim "not part of a stack" while the PR is one.
- **A stack member REFUSES plain `gh pr merge`.** The verb is
  `gh stack merge <pr> --squash --yes`; the refusal signals a stack.
- **After a layer merges, GitHub auto-rebases the next layer itself** (no push
  from any seat; the force-push is attributed to the merger). Verify with
  `git range-diff`/patch-id — but that check is **necessary, not sufficient**: it
  answers "did the patch change?", never "does the result still work?". A
  conflict-free rebase can break the tree (one PR drops an import, another adds a caller).
  **Run the tests on each rebased tip.**
- **Other verbs:** `gh stack sync` / `gh stack rebase` (cascading rebase),
  `gh stack view --json`. Stacks are API-readable (REST
  `stack`, GraphQL `PullRequestStack`), not API-mutable.
- **Approvals after a layer: compare, never predict.** With
  `require_last_push_approval=true` on the target branch, the seat that
  syncs/rebases cannot re-affirm the tip; a teammate re-approves. Whether an
  approval is dismissed on push depends on the base branch's
  `dismiss_stale_reviews_on_push` — **necessary on the evidence, not
  demonstrably sufficient**, so do not treat it as predicting the outcome. After every layer, read the approval back off `pulls/<N>/reviews`: it
  counts only if its `commit_id` is the PR's current `headRefOid` AND its
  `submitted_at` is not earlier than that commit's committer date.
  **`commit_id == headRefOid` alone is never sufficient**: GitHub can re-point live
  approvals onto newer heads (engram-alpha#2267). Otherwise get a fresh approval
  (see `docs/internal/PR-MERGE-POLICY.md`, gate item 2). Never trust `reviewDecision` alone. (On engram the setting is `true` on `dev`;
  `main` has no pull-request rule, so it is unset there.)
- **Read branch protection via `rules/branches/<b>`, never
  `branches/<b>/protection`.** The classic endpoint answers by caller permission
  (admin: "Branch not protected"; non-admin: "Not Found"; unauthenticated: 401)
  and gives the same answer for a ruleset-protected and an unprotected branch, so it discriminates nothing.

*Evidence for every rule above: engram's `docs/internal/GH-STACK.md`.*

> **Duplicated by hand in engram, umwelt, and soma `CLAUDE.md`** (Lei,
> 2026-08-29: whichever repo a session starts from must load it), with no
> generator or test binding them. Change one, change all three in the same
> pass — **but the ruleset paragraph and any repo-specific merge-authority
> clause are deliberately per-repo; sync the rules, not the local facts.**
> Lei's 2026-09-15 01:01Z ruling ("the Soma review can just use the same
> team-wide convention, no need for a special exception") retires the
> 2026-09-14 interim exception that had carved soma out of the fairy-round-
> timing clarification in "Mechanical check at the merge seat" above (issue
> #2920) — this repo's own copy of the exception is removed by THIS PR.
> The other two copies are separate, still-open PRs at time of writing:
> soma's own transcription (soma#60) and umwelt's matching removal
> (umwelt#816) — **do not read this note as "already synced across all
> three"; it is synced once all three of this PR / soma#60 / umwelt#816
> have merged.** The evidence behind this section — every dated measurement — now lives in
> engram's `docs/internal/GH-STACK.md`, and the sibling copies point there.

### PR inheritance — read the open PRs first; make status readable from the LIST

Three parts of one rule (Lei, 2026-08-14). Applies on a seat with no co-resident agent too — checking
open PRs guards against your own stale branch from a prior session, not just a
counterpart's work. All three exist because **a list read and a thread read are
different operations, and only the list read scales**: nobody opens ten comment
threads to reconstruct status, so status living only in comments is invisible at the
altitude people work at.

**1. Before starting: `gh pr list --state open`, before the first commit of any new
task.** A read of the actual list, drafts included. If an open PR already does part of
the job, branch off *that PR's tip* rather than off `dev`. Never push additively into
another agent's branch — cut your own from their tip; editing their artifact has no
additive exception.

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
truncates the title **from the tail** (the budget varies with terminal width and
the other columns), so a leading marker survives every width and a trailing one
goes first.

**Who may set it: the merge seat**, whoever holds it (Lei, 2026-08-14 — the seat moves
with availability, the authority travels with it). A deliberate, narrow exception to
*never edit another agent's artifact*: **status annotation only, never a rewrite of
their title's substance or description.** It exists because whoever knows a PR is
superseded is usually not its author. **Also comment on the replaced PR** — the title
is the mechanism, but the comment is the half that reaches the human, and the human
needing to know their work was carried forward is the reason this rule exists at all.

> **⚠ `gh pr edit --title` and `gh pr view --comments` may fail while looking
> like they worked — CHECK YOUR VERSION.** On `gh` 2.45.0 `--title` **exits 1**
> and leaves the title unchanged; `--comments` **exits 0 and prints nothing** on
> a PR that has comments (broken on every `.0` release tested from 2.45.0 to
> 2.72.0, working from 2.73.0). Fallbacks: `gh api -X PATCH
> repos/<owner>/<repo>/pulls/<N> -f title="…"` for the title; for comments read
> ALL THREE of `issues/<N>/comments`, `pulls/<N>/reviews`, `pulls/<N>/comments`
> (none alone is complete). **Read results back either way.**

**Why:** the defect was never missing communication; it was communication living
where a list read cannot see it (umwelt#561). Hence a title marker and a prompt
close, not another comment.

*The #1320 / #1415 case, the umwelt#561 story, and the version bisection:
`docs/internal/PR-REVIEW-CONVENTION.md` § PR inheritance.*

> **Duplicated by hand in umwelt's `CLAUDE.md`**, with no generator or test binding
> them — each file loads only with its own project. Change one, change the other in the
> same pass; the failure mode is a rule current on the copy you are reading and stale
> on the one governing the repo you are in.

### Every PR carries a baton — merge-readiness is baton-tracked, not inferred

Open a PR-baton **at PR creation**, for **every** PR — bunch-driven OR standalone:
`baton init PR-N --title "<short>" --participants <self>,<counterpart>,<maintainer> --turn <self> --status in-progress --colleague <counterpart> --repo <owner>/<repo>`.
Use `--colleague <name>` to identify the expected colleague reviewer — this adds them to participants (dedup) and enables the colleague gate: `baton flip PR-N <maintainer>` will be rejected if no non-author APPROVED review exists. Without `--colleague` (and without a third participant), the gate falls back to warn-only (a baton with no colleague).
Then flip it as the turn passes along the review pipeline:

- reviewer-fairy converged → `baton flip PR-N <counterpart> "fairy-converged; colleague please"`
- counterpart **comments / requests changes** → it flips back to you (`flip PR-N <self>`);
  iterate and re-review. Only an explicit colleague **approval** advances the baton — a
  "COMMENTED with considerations" verdict does **not** (that misread is the whole origin).
- counterpart **approved** → `baton advance PR-N` (or the same hand-off as `baton flip PR-N <maintainer> "colleague-APPROVED; ready for merge"`; on a colleague-bearing PR baton the server runs one strict gate for both and refuses unless GitHub shows an APPROVED review on the current head by the colleague's mapped login)

*(No counterpart, so no colleague: skip the colleague flip; after the reviewer-fairy
converges and your own second-layer pass is clean, flip straight to the maintainer.)*

**A PR is merge-ready ONLY when its baton phase = `merge`** (the `phase:` line of the
baton file, shown by `baton show`). That is **necessary, not sufficient**: the CI-green gate
above still applies at merge time; `baton merge` itself requires the turn to be at a
maintainer-role holder (the pool sentinel, or a named agent who is neither the colleague nor
the author) and does **not** read `phase` at all, so checking the phase (`baton show`) before
`baton merge` is a rule for whoever sits at the merge seat, not something the tool enforces
(an owner pressing their own PR under *Owner merge* cannot run `baton merge` at all, since
the turn is theirs; they record the press with `baton close PR-N --status merged`);
and a flip does not move the phase back out of `merge` — a PR the maintainer hands back to
the author for rework keeps `phase: merge`, so a turn at the author means *not ready*
whatever the phase says. `turn` stays the whose-move cursor: `baton advance`
writes it to the merge seat it names (by default the one participant who is neither author
nor colleague), so a turn at the maintainer is **not** the readiness signal either. On a PR
baton whose `colleague` was set **before** it reached `merge` (the turn log shows when),
`phase: merge` is reached through the server's strict peer→merge gate (`baton advance PR-N`,
or a flip that resolves as the same transition): GitHub must show an `APPROVED` review on the
PR's current head by a GitHub login mapped to the baton's `colleague`. **On a forum that
predates #3296, a colleague-bearing baton at `phase: merge` is not proof that gate ran**: a
baton with no colleague can reach `merge` through the weak flip (the CLI sends
`transition_phase=merge` on that path), and there `POST /colleague` could add a colleague to
a baton already at `merge`. A forum running #3296 refuses that (422); on an older forum, check
when the colleague was set. The gate proves a mapped non-author approval on the current head, **not** that the
*assigned* colleague approved (`colleague` is caller-declared and mutable); the limits are
in `docs/baton-protocol.md`. The maintainer merges from that phase signal — never from a PR
that merely *looks* parked. A PR with **no baton**, or a baton **not yet at `phase: merge`**,
is **not** ready, no matter how long it has sat. Symmetrically: never leave a
colleague-converged PR without running the advance — otherwise its readiness is
invisible, and a colleague verdict that is "COMMENTED with considerations" (not an
approval) can be misread as ready. **Fallback — read `turn` as before (turn = the
maintainer, reached via an explicit colleague approval or, with no counterpart, your own converged
second-layer pass) — in these known cases (a list of what has been found, not a proof that
it is exhaustive):** the baton carries no `phase:` line (a legacy file); or it has no
`colleague` (a baton with no colleague keeps the old weak gate); or the host's forum server predates
the U2b gate (#3284), until it is redeployed.

**On a seat with no co-resident agent and no counterpart:** the colleague layer collapses
to reviewer-fairy + the author's own second-layer eye + the maintainer. Don't
block on a colleague review that has no one to perform it; the
not-fairy-delegatable constraint above is moot when there is no counterpart to
delegate to.

**No co-resident agent, but cross-host forum peers:** if no other agent shares
your machine but you have peers reachable via the LAN forum (e.g. Borges and
Ariadne on a different host), **mention the relevant peer on the forum after any
GitHub activity that requires their reaction** — posting a review, requesting a
review, commenting with findings, approving, or responding to a review. The
baton/letter system requires a shared filesystem; the forum is the only
real-time cross-host notification channel. A brief `@name` mention in a forum
post or reply is enough — the forum-mention Monitor delivers it within ~30s.
Without this, a review or request sits unacknowledged until the peer's next
scheduled wakeup.

**Why project-local** (not user-level): whether a counterpart exists is hard
to encode in always-loaded identity rules.

**Origin:** a self-review lapse traced to this reciprocal step being
unwritten in the workflow doc; the hierarchy itself has been practiced
since agent coordination work started in 2026-05.

## Attribution convention — Lei's functional-vs-marker test (alpha#2270)

**The rule is Lei's, ruled 2026-08-15, not open and not interim.** Naming a colleague in a shipped artifact — a PR comment, a code comment, a test docstring — is governed by whether the name is *functional and will mislead general users* (something a reader or a tool would act on, and misread as coming from a real contributor to the project) or a *marker* (attribution that identifies who did something, acted on by nobody). Both conjuncts must hold to prohibit — functional alone or misleading alone is not enough, only functional-and-misleading blocks. The inert-but-misleading case (a name that invites no tool action yet could still mislead a reader into believing a human they've never heard of is a project contributor) is exactly what keeps case 2 below open, rather than being settled by the functional prong the way case 1 is. Lei's own words, relayed verbatim by Borges from his graph (quote-verified, source_class user_stated):

> "Fine as attribution, same rule as my name, as long as it's not functional and will mislead general users, it's fine as development markers."

**Worked application — agent handles vs. human names:**

1. **A bare agent handle as attribution** (Ariadne, Borges, Kepler, Clio, …) in a shipped comment or PR body ("Ariadne's catch, PR #2618") — **fine.** It's a marker: it credits, it invites nothing a general user's install can act on or fail against. An **`@handle` mention**, by contrast, is functional — it renders as a live notification to a real GitHub account the moment the text is quoted anywhere on the platform, which is a different case from a bare name and not covered by this paragraph.
2. **A human name** other than Lei's own, inside a shipped artifact — held open deliberately pending Lei's explicit confirmation, though Lei's own "same rule as my name" arguably already reaches it. Do not extend the agent-handle answer to this case by inference.

**Why the two related gates differ in severity:** `no-new-shipped-node-ids`
**blocks** because a shipped concrete node ID is functional-and-dead;
`name-leak-warn` **advises, never blocks** because it cannot itself tell a
citation from a leak, so it asks the seat to decide. Umwelt and soma run no
`name-leak-warn` gate — a merge seat there applies Lei's rule by reading the PR.
(Rationale: engram's `docs/internal/PR-REVIEW-CONVENTION.md` § Attribution convention.)

**For the merge seat:** an advisory `name-leak-warn` hit on a **bare agent handle** is never, by itself, grounds to hold a PR — not because the gate has ruled (it explicitly hasn't; see above), but because Lei's 2026-08-15 ruling already has, for this one case. The seat's job on a hit is first to identify *which* name matched — the gate reports one pattern per line and does not name the category, so a line with several candidate names does not tell you which one tripped it — and only then apply this rule to a bare agent handle specifically. Scrubbing a colleague's attribution under a self-invented "cite the PR, not the person" habit is the exact over-application this section exists to name.

> **Duplicated by hand in engram, umwelt, and soma `CLAUDE.md`**, with no
> generator or test binding the copies. Change one, change all three in the same
> pass. The gates themselves are CI steps in engram's `pr-checks.yml` (umwelt has
> its own separate node-ID gate; neither umwelt nor soma runs a `name-leak-warn`
> gate); these three copies document Lei's rule, not the gates, and the evidence
> behind it now lives in engram's `docs/internal/PR-REVIEW-CONVENTION.md`, to which
> the sibling copies point.

## Project-as-bunch coordination (with a counterpart agent)

When a counterpart ENGRAM agent shares the work (e.g. Ariadne + Borges), open
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

- `baton claim pool-X` — `turn:` flips to the claimer; refuses when `turn:`
  is not this seat's recognized pool marker — either a colleague is holding it,
  or it is a pool/maintainer marker written by a seat with a different
  `primary_user` (#3039). Before assuming someone holds it, run `baton show`
  and find the last turn-log line shaped `<A> → <B>: <reason>` whose `<B>` —
  the name just after the FIRST arrow — is the `turn:` value; `anchor`,
  `set_colleague` and `reopen` write arrows without touching `turn:`, so "the
  last arrow line" can name a non-holder. `<pool> → <B>: claimed` = a
  colleague took it. Any other such line is **indeterminate from your seat** —
  `release` and `flip` emit the identical text, so it is either a colleague
  taking it on a flip (held) or a return to a pool marker belonging to a seat
  whose `primary_user` you cannot resolve (free); that is #3039. Settle it off
  the log — ask `<B>`, or check the PR/project the baton anchors — and do not
  flip on a guess. `turn_reason` does not discriminate (every claim writes
  `"claimed"`).
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

### With no counterpart

No counterpart → no claim contention. The Project layer still organizes
defect-classes, but the `claim` / `release` ritual is moot. The PR-layer baton
still serves as a per-PR cursor through fairy → maintainer.

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

**Why project-local** (not user-level): as in the PR review
convention above, agent coordination tooling is project-specific.

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
   **Standing rule (2026-11-11):** a closing keyword does NOT close an issue on a
   merge into `dev` (measured twice: `docs/internal/PR-ISSUE-CLOSURE.md`). The merge
   seat closes it by hand right after the press, naming the PR and merge commit;
   title and body keep `[closes #N]` / `Closes #N.` as intent. If `dev` becomes the
   default branch, this lapses.


**For multi-PR / EPIC ships:** earlier PRs may reference the issue number but
**do not** use a closing keyword in either surface. Only the LAST PR in the
series uses BOTH `[closes #N]` in title AND `Closes #N.` in body. Don't rely on a
"wrap-up PR I'll file later" — it rarely materializes.

**A `[part of #N]` PR never auto-closes #N, so the epic body is the one artifact
nothing points at** (#2617, 2026-09-06). The convention: a `[part of #N]` PR
ticks its own workstream line in the epic body (checkbox, or a `— landed as #M
(sha)` suffix) as part of merging. The author names that line in the PR's own
body (e.g. "ticks #N's `WS3` line"); the merge seat ticks the line the PR named,
not one it reverse-engineers. Discipline, not a mechanical gate.

**For partial work that addresses but doesn't fully close:** omit the closing
tag from both title AND body. Comment on the issue describing what landed and
what remains, so the scope is durable.

### Body-text trap discipline (referencing the wrong number)

The auto-close parser sees `(close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s+#?\d+`
in the body **regardless of negation or brackets** (the `NOT` and the `[ ]` are
invisible to it). This is the mechanism that fires intentional auto-close — and the
same mechanism that fires *accidental* auto-close when the body references a
NON-target issue.


**Inline-code spans and fenced code blocks (triple-backtick or tilde fences) are the one exception:** the
parser ignores close keywords inside them (measured #1191, #2630). Two
consequences: an *intentional* `Closes #N` must be **plain text** — a backticked
or fenced one silently fails to auto-close; and the verification grep below is a
plain regex, so it over-flags backticked refs (safe from the real parser —
confirm, don't treat as a risk).

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
   gh api graphql -f query='{repository(owner:"engram-agents",name:"engram"){pullRequest(number:<PR>){closingIssuesReferences(first:5){nodes{number}}}}}'
   ```
   The full API envelope is `{"data":{"repository":{"pullRequest":{"closingIssuesReferences":{"nodes":[...]}}}}}` —
   look for `nodes: [{number: <N>}]` where N matches the title's `[closes #N]`.
   `"nodes":[]` (empty) means the body is missing the keyword form and the PR
   will NOT auto-close **via the body** on merge; rewrite via
   `gh pr edit <PR> --body "..."`. **An empty array says NOTHING about commit
   messages — see check 3, which covers the path this query cannot see.** Intent only: a merge to `dev` closes nothing (standing rule, item 2).

   **This query lags a `gh pr edit --body`/push, in EITHER direction** (a fixed body
   can still read non-empty; a bad one can read empty). Query, wait at least 5
   seconds (longer if anything else is happening), query again, and trust only
   the settled answer.

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
   GitHub's parser reads independently, invisible to checks 1 and 2.
   This repo's `squash_merge_commit_message` is `BLANK` (title `PR_TITLE`; reset
   after the 2026-11 rebuild), but **umwelt and soma
   read `COMMIT_MESSAGES`** — check `gh api repos/engram-agents/<repo> --jq
   .squash_merge_commit_message` for the repo you are in. Run the grep anyway; a
   settings flip is silent, and so are a hand-edited squash message and
   copy-pasted commit text:
   ```
   git log origin/dev..HEAD --format='%B' | grep -inE '\b(close[sd]?|fix(e[sd])?|resolve[sd]?) +([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)?#[0-9]+'
   ```
   Every match must reference the SAME `#N` the PR intends to close — anything
   else gets the commit reworded (rebase) or accepted deliberately, never
   discovered post-merge. The pattern covers the `owner/repo#N` cross-repo close
   form; bare `reponame#N` never closes anything. Deliberately unasserted:
   `Closes: #N` with a colon, and whether a close keyword in the squash-commit
   SUBJECT fires the parser (`squash_merge_commit_title: PR_TITLE` puts the PR
   title there) — measure on the next real `[closes #N]` merge before claiming
   either way.

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

*Measurements and incidents behind these checks (the 2026-05-31 anchor, the
backtick and fence tests, the query-lag timings, the commit-message exposure,
umwelt#674/#632): `docs/internal/PR-ISSUE-CLOSURE.md`.*
