# PR review convention — history and evidence

Evidence and history behind the PR-review sections of CLAUDE.md (review convention, Conclusion warrant, the order rule, PR inheritance, Attribution).

## Moved from CLAUDE.md § PR review convention — item 1's cap (how the rule reached the repo) (moved 2026-10-03, #3322)

*Verbatim; moved, not edited. CLAUDE.md keeps the rule and a pointer here.*

Written in after
   engram-alpha #3100 merged under the cap (three REQUEST_CHANGES rounds)
   and the merge seat had to find the rule on the forum (forum #431, post
   10837).

## Moved from CLAUDE.md § PR review convention › Conclusion warrant (moved 2026-10-03, #3322)

*Verbatim; moved, not edited. CLAUDE.md keeps the rule and a pointer here.*

Neither review layer was named **"is this claim true."** The fairy round checks
mechanics; the colleague round checks pattern-level and cross-issue concerns.
Both are good at their jobs, and a PR could pass both carrying a wrong number, a
wrong mechanism, or a wrong cause — because being wrong about a *fact* was
nobody's assigned box. Adopted fleet-wide 2026-09-03 (forum #385: Kepler's
ruling, with Clio, Sol, Luria, Aleph and Borges converging; nobody produced a
friction-and-caught-nothing case).

**Read this section as documentation of a gate that lives somewhere else.** The
gate is the required `## 🔵 Conclusion warrant` section in
`engram-pr-reviewer.md`'s output contract — emitted, or *visibly absent*. That
placement is the load-bearing part, not a formatting choice: a line of prose
saying "verify your claims first" is a **preventive** remedy, and preventive
remedies are walk-past-able. **No preventive-remedy instance this fleet examined
produced a prevention** — including a warning one of us authored the previous day,
fired at PreToolUse, with a written pre-commitment to heed it, read past while
committing the exact error it named. **Uncounted denominator**: those cases were
collected as they turned up rather than sampled, and the two candidate positives
that were confounded and left unscored are *additive* remedies, not preventive
ones — so no unscored case sits inside the claim above, and the claim is not a
ratio and must not be quoted as one.
A required output section is **additive**: its omission is a fact someone else can
see. The copies of this section in the three
`CLAUDE.md` files document the gate for the human-agent colleague pass; they are
not the gate.

**A quiet week is not confirmation.** A preventive gate's successes are invisible
by construction, so the falsifier can fire and the corresponding confirmation
cannot. Read the falsifier log —
engram-alpha's `docs/conclusion-warrant-falsifier-log.md`
— not the silence. The log deliberately **does not start clean**: its first
entry is a case where the box would have been filled in honestly — every claim
enumerated, the unmeasured half named explicitly — and the claim sentence two
paragraphs earlier still overstated it. That case is what line 4 exists for.
The log has no denominator and can never confirm — it records cases, and its
header says why.

**Diagnostic aid, offered with its bounds.** Verification failures sort roughly
into wrong **SET** (right axis, too few members), wrong **OBJECT** (right
question, wrong artifact), and wrong **AXIS** (right artifact, wrong dimension
entirely). **AXIS is the load-bearing member** — it is the one that survives
careful work, because collapsing it into "be more thorough" produces a more
exhaustive count of the wrong dimension, which is indistinguishable from rigour
from the inside. It is also the member with the least support. SET and OBJECT
already have action-time tripwires and stay with them. **This carving is n=4, one
agent, one day, and one agent's own carving — not a measured clustering.** Use it
to name AXIS out loud; do not cite it as a taxonomy.

Note what is
> NOT duplicated: the gate itself lives in exactly one place
> (`engram-pr-reviewer.md`'s output contract), so these three copies are
> documentation of one mechanism, never three copies of it.

## Moved from CLAUDE.md § PR review convention › The order is a rule, not a diagram (moved 2026-10-03, #3322)

*Verbatim; moved, not edited. CLAUDE.md keeps the rule and a pointer here.*

Lei's ruling, in-session, 2026-08-24 morning (relayed by Kepler; twin of
umwelt#732, filed here as #2219 per the duplicated-by-hand rule below). The
two-layer gate above is a **sequence**, not two boxes listed in order that
happen to both need doing. In practice seats have been parallelizing them —
asking the counterpart for the colleague pass before, or at the same time as,
the reviewer-fairy round — and merging on a colleague approval that landed
first, without the fairy round ever converging, or running at all.

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

**The hazard is BIDIRECTIONAL, and the paragraph above names only one half.** A
round transcribed by a colleague into a **review body** is invisible to a scan of
`issues/<N>/comments`, exactly as a round posted as a comment is invisible to
`pulls/<N>/reviews`. Whichever single endpoint a hand-rolled fallback hits, it is
blind in the other direction — so hit both, or use the one command that merges
them.

**And read them IN FULL — a CAPPED read of the RIGHT surface fails exactly like
querying the wrong one.** This is the harder half, because it survives doing
everything above correctly. Measured 2026-09-06 on engram-alpha#2656: the merge
seat queried both surfaces and printed each review body **cut at 700
characters**; the transcribed round sat at char 2152 of a 6414-char review, and
the PR was twice reported as having no fairy round. Nothing was missing and no
pattern was too narrow — the slice did not reach it.

That question is the whole of it, and the property behind it is **monotonicity**:
a capped read settles a question only when the slice is known — independently of
the read — to hold everything the answer depends on. Finding your evidence
*inside* the slice is the usual way to know that: a positive existential,
answered YES. *Plainly: finding something proves it exists; not finding it, or
needing to know what is LATEST, does not.* **If you cannot name how you know
the slice suffices, you do not know it.**

*Two earlier drafts of this rule were false and both were caught before merge: it
first claimed the safe case was "any question other than is-it-absent", then that
a positive existential was the only safe case. Recorded because each wrong
version was the kind a reader would have acted on.*

## Moved from CLAUDE.md § PR review convention › PR inheritance (moved 2026-10-03, #3322)

*Verbatim; moved, not edited. CLAUDE.md keeps the rule and a pointer here.*

*What it actually buys, stated honestly:* run here the day it was written, it surfaced
**#1320 / #1415** — both @sol-montaigne's, both adding `engram_check_independence`,
both `closes #1313`, opened a day apart. But that pair was **already known**: Sol
marked #1320 superseded on 2026-06-25, and it is open today because **Lei parked it**
past `v0.3.2-rc1` (his ruling, relayed onto the PR by me on 2026-08-03 — not a comment
of his own; flagged because this section is otherwise careful about relay-vs-first-hand). So the check does not usually find *unknown* duplication. It finds
**known duplication nobody is on the hook for** — the more common failure, and the one
that persists for months.

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

## Moved from CLAUDE.md § Attribution convention (moved 2026-10-03, #3322)

*Verbatim; moved, not edited. CLAUDE.md keeps the rule and a pointer here.*

Kepler's own amendment on alpha#2270 states the relation between this test and the worked application below precisely: *"The governing rule is therefore Lei's FUNCTIONAL-vs-MARKER discriminator, not my handle-vs-human split — mine becomes the worked application (they agree on every case in front of us today, and come apart only at edges: his form permits a human name inside a pure attribution marker, and forbids an agent handle that is functional/misleading)."* The handle-vs-human split is not a competing axis; it's what applying Lei's test looks like in the one case decided so far.

**Why the two related gates differ in severity, and that difference is principled, not accidental — on engram-alpha, where both gates run today:** `no-new-shipped-node-ids` **blocks** because a shipped concrete node ID is functional-and-dead — it invites an `engram_inspect` that fails on every install but the author's, by construction (see engram-alpha's `CLAUDE.md` "PR — issue closure" section, Body-text trap discipline, for the sibling case of a keyword the parser *does* act on). `name-leak-warn` **advises, never blocks** — because, unlike the node-ID gate, it cannot itself tell a citation from a leak (its own comment says so: "these may be valid citations or unintended leaks — please review each one"), so it asks the seat to decide rather than ruling. Both are CI steps in engram-alpha's `pr-checks.yml`; umwelt runs its own separate node-ID gate, and neither umwelt nor soma runs a `name-leak-warn` gate — a merge seat there applies Lei's rule below by reading the PR, not from a gate hit.

it happened twice on engram-alpha (#2618, #2697), both scrubbing the same colleague's credit, both against Lei's already-ruled test.

The gates themselves are CI steps in engram-alpha's `pr-checks.yml`
> (umwelt has its own separate node-ID gate; neither umwelt nor soma runs a
> `name-leak-warn` gate) — these three copies document Lei's rule, not the
> gates that happen to enforce a slice of it on engram-alpha today.
