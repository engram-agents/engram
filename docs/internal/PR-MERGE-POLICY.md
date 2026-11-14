# PR merge policy — what Lei reviews before merge

This repo runs a colleague-review + CI-green merge gate (see `CLAUDE.md` → *PR review convention* and *PR merge-readiness*). This doc answers the **next** question: once a PR is merge-ready, **who merges it** — Lei after a pre-merge read, or the agent team directly.

## The rule (Lei, 2026-06-24)

> "New feature in ENGRAM substrate → I read the PR first. Bug fixes → usually fine to merge without my explicit review."

The discriminator is **substrate concept vs. not** — does the PR change *how ENGRAM represents, scores, or reasons about knowledge?*

### Sharpened by Lei, 2026-08-25 — "what counts as healthy" is stats, not substrate

The 06-24 rule left one word doing too much work. "Scores" reads as covering
any change to a scoring computation, which swept in the whole health-score
class. Lei drew the line himself, in-session, answering a direct question
about the alpha#2187 lane. **Provenance, stated here rather than only in the
callout below: this quote is second-hand.** Lei said it on Kepler's seat; it
reaches this doc through Kepler's transcript, not through any surface the rest
of us can read:

> *"I think they are stats change, so you or Borges can merge when everything
> passed. The rationale is, it touches what is considered healthy, but doesn't
> change HOW your engram nodes are structured. Adding new nodes type. Edge type
> or verbs I consider substrate change."*

So the discriminator has a concrete test, in his terms:

| | |
|---|---|
| **Substrate** — Lei reads first | adds or changes a **node type**, an **edge type**, or a **verb** |
| **Stats** — the team merges | changes **what counts as healthy** without changing how nodes are structured |

**The confidence-computation bullet in the ⛔ list below is narrowed by this,
not overridden — and here is the narrowing, stated once.** Confidence is
computed from a node's own structural fields (`quote_type`, `reasoning_type`,
premise propagation) and *is what the node claims about itself*; changing how it
is computed changes what every node **means**, so it stays substrate. A health
or diagnostic score computed **over** the graph is a report *about* nodes and is
stats.

The test is **whether a node's own semantics move**, not where bytes land. A
health figure cached onto a node purely to make a report faster changes no
node's meaning and stays stats. Do not restate this test in some other form
elsewhere — one gate, one wording.

### The 2026-09-02 statement — context for the above, NOT new authority

**This section grants nothing.** It is recorded because it is *why* the 08-25
criterion was given, and because without it a reader wonders whether the gate
was weakened in some further, unwritten way. It was not. **The question "does a
design-settled substrate PR still need to flip to Lei right now?" is answered in
exactly two places — "Delegated by Lei, 2026-09-27" (he read the design) and
"When Lei is away" (he is away) below — and this section answers it in
neither case.**

Lei, 2026-09-02, on the health-score lane (#1991), which the 08-25 criterion had
already classified as stats. Quoted in full, no elision:

> *"I won't be able to read detailed PR for all the work you guys do, your work
> progress is far beyond my review bandwidth, so I only join you in the design,
> and I trust you and Kepler and everyone to thoroughly test and verify the
> shipped code against our design, and catch any gaps or unforeseen conflicts.
> So far, I think you are doing great on that end. We've discussed pretty
> thoroughly on health system design, so I think you or Kepler can make the call
> to merge the built PRs."*

What it does and does not do:

- **Does:** confirm that the health-score PRs *already classified as stats by the
  08-25 criterion* are ours to merge — corroboration, not a second grant. It does
  not pre-clear later PRs in the same stack: a future rung that carries design
  novelty is judged on its own against the ⛔ list, exactly like any other PR.
- **Does:** state that he **keeps the design** and delegates the *verification*.
  That raises the bar on us: "Lei is not reading PRs" is never a reason to skip
  a design conversation, only a reason it must happen somewhere he can see it.
- **Does NOT:** create a general "design was settled with him at some point →
  the team may merge any substrate PR" rule. If you are reaching for this
  section to justify a merge, you are using it for the one thing it does not
  do. *(The 2026-09-27 delegation below later granted a narrower rule — "a
  design **he** read", with a written conformance check — in its own words.
  That grant lives there, not here.)*

> **⚠ How this arrived, and the failure it should prevent.** The 08-25 ruling
> was delivered in-session to **one seat** (Kepler's) and lived only in his
> graph for eight days. This document — the SSoT every other seat consults —
> was last amended 2026-08-22, three days *before* the ruling, and went on
> gating the class Lei had just released. Neither surface could detect the
> disagreement: his node had no edge to this doc, and this doc had no record
> that a ruling superseded it. **Measured cost: three PRs — #2187, #2189
> and #2205, opened 2026-08-22/23 — sat on a gate that had been removed on day
> three. #2187 merged 2026-09-02 after eleven days; #2189 and #2205 are still
> open as of 2026-09-02, at eleven and ten days.** (Two earlier drafts of this
> one sentence were wrong, and both corrections belong in the record of a doc
> that argues unverifiable claims cause harm. The first said *four* PRs,
> counting #2494 — PR4 of the same stack, opened 2026-09-02, so hours not days.
> The second fixed the count and left the tense and the duration unexamined
> one level down: "waited eleven days" is past tense, true only of #2187, and
> gave #2205 an eleventh day it has not had. The present tense is not a
> nicety — two thirds of this cost is still accruing while you read the
> sentence describing it.) **A ruling that changes this doc gets
> written into this doc, by whoever heard it, the day they hear it** — a
> verbatim quote in a graph is provenance, not governance.

## Delegated by Lei, 2026-09-27 — substrate PRs that implement a design he already read

Lei, 2026-09-27, in Kepler's session, answering Borges's question on where
delegation stops. **Provenance: second-hand.** It reaches this doc through
Kepler's forum post (thread #432, post 10928, 13:16Z), not through a surface
the rest of us can read:

> *"yes, go with your recommendation on delegation"*

The recommendation he accepted, as Kepler posted it:

> - The team may merge a technical or bug-fix substrate PR after peer review,
>   without his read, **when it implements a design he already read**.
> - The merge seat writes a short conformance check against that design before
>   merging.
> - Issues may be closed when code evidence is attached.
> - A new substrate concept still goes to him first.

**What changes.** A PR that hits the ⛔ list below no longer flips to Lei *just
because* it touches substrate. It flips to him when it carries a substrate
**concept he has not read** — a new node type, edge type or verb whose design
he never saw, or a PR that departs from the design he did see. A build unit of
a design he read is merged by the team.

**How the merge seat applies it — two steps, both written on the PR before the
merge** (operational reading of the grant; the two steps are not Lei's words):

1. **Point to the read.** Link the artifact that shows Lei read the design: his
   APPROVE or COMMENT on the design-note PR, his comment on the design issue,
   or his in-session word quoted verbatim with who relayed it and where.
   "The design was discussed with him" without an artifact does not count. If
   you cannot link it, flip to Lei.
2. **Conformance check.** A short PR comment: which design, which points the
   PR implements, and any place it differs. A difference that changes what a
   node, edge or verb **means** is a new concept → flip to Lei. A difference in
   plumbing (names, file layout, error text) is not.

**Scope limits.** This does not touch the live deploy (still Lei's — see "When
Lei is away"), the security clause (the security seat's approval is still the gate there),
or any other repo. It adds nothing to the ⛔ list and removes nothing from it;
it changes who merges an item on that list once Lei has read its design.

### Closing issues (same ruling)

**An issue may be closed when code evidence is attached.** The closing comment
names the evidence: the merged PR or commit SHA, and the code or test that
shows the issue's problem is gone. "I believe this is fixed" is not evidence.
This covers **issues only**. Closing a PR someone else opened is not covered
by this ruling.
*(Only the first sentence is the ruling. The rest is the operational reading
of "code evidence" and of its scope, the same status as the two merge-seat steps
above.)*

**Self-filed issues (Lei, 2026-10-26 9:21 AM NY, in session: "yes to Zephyr's
rule").** A seat may close an issue it filed itself when all four parts hold:
the issue was **filed by that seat**; it carries a **closing condition that seat
wrote down in advance**; the condition is **verified met**, by a read of the
artifact the condition names (the deployed head, the test, the measurement),
not by a passing run that would pass without the condition; and the closing
comment **names the evidence**. Trimmed to "a seat may close its own issue" the
rule reads as permission to close anything one opened; the load-bearing part is
the written condition, which anyone can check against the comment. **What it
does not touch:** an issue filed by someone else closes only under the
first-sentence ruling above (its ask met by merged code, evidence named in the
comment); any other issue close, and every PR merge or close, stays per-action. *(Origin: tls-front #8, 2026-10-26, where the
filer verified the condition and declined the close because this carve-out
existed in the commander's table and not in the filer's; the asymmetry was the
defect, and this paragraph is the shared table.)*

## Scope — the substrate gate is **engram only**

The substrate-feature gate below is **engram-specific**. Other repos in the org carry their own **delegated** merge authority, set by Lei per project:

- **umwelt** — merges are **Borges's + Ariadne's** to make (Lei, 2026-07-03: *"umwelt is yours to maintain"*); Lei removed *his* gate on umwelt, including for umwelt substrate-level / new-mechanism changes. The review discipline (reviewer-fairy → counterpart-colleague → CI-green) still holds; only the final merge is the team's, not Lei's. *(Amended 2026-08-22 by the two-holder grant below: Kepler is added to the merge authority; existing authorities — Ariadne's own lane included — are unchanged.)*
- **soma** — Ariadne is the principal approver + merger (Lei, 2026-06-21).

## Who presses the button (Lei, 2026-08-22) — TWO holders, serialization preserved

Lei's in-session grant, verbatim:

> *"let's add you to the merge authority in addition to Borges, both you can merge
> on all repos when CI and peer approval requirements meet. So you don't need to
> wait for Borges to merge ready PRs."*

So **Kepler and Borges each hold merge authority on all org repos**, gated on the
unchanged readiness routine — reviewer-fairy convergence, non-author peer
approval, CI green on the tip, the security read where the surface calls for it,
**and every gate in this document where it applies**: the grant's own words are
"when … requirements meet," and this policy's substrate gate (Lei reads
substrate features first unless he has read their design, per the 2026-09-27
delegation; when-away grant terms) is one of those requirements,
not something the grant displaces.

**What the second holder must not silently drop: serialization** (Borges's
catch, 2026-08-22, adopted). The one-person seat existed to serialize merges,
not because one person was special — two holders keep that property by
mechanism, not luck:

- **Default repo lanes**: Borges holds engram's queue, Kepler holds
  umwelt's. Crossing into the other's lane is fine and takes **one line saying
  so first** — never a silent concurrent merge into the same tip. *(Amended
  2026-10-22 by *Owner merge* below: for project owners the lanes are suspended
  and the one-line rule applies to every merge into a shared tip; the base-SHA
  assertion there is the serialization mechanism, the dev merge queue being
  unavailable to this repository.)*
- **Whoever starts a multi-PR stack finishes it.** A stacked series lands
  bottom-up under one hand (live example at adoption: the health-score series
  #2187 → #2189 → PR3 → PR4 — PR3/PR4 not yet opened when this was written,
  the literal strings are not broken links). Two holders taking rungs of one stack
  independently is how a base gets deleted under an open PR or a rung merges
  onto a tip its CI never saw.
- **Security PRs — Mira's approval is the merge gate, not Lei.** Two
  directions, attributed separately by provenance event — both verbatim
  Lei, quote-verified against the session transcript: Borges's security PRs
  → Kepler's mechanical merge on Mira's exact-tip approval + CI green (Lei,
  2026-09-09 23:50Z, verbatim, relayed by Kepler: *"Yes, go ahead and merge
  Borges's security PRs yourself once Mira approves."*); security PRs
  Kepler's seat cannot verify → Borges merges, same gates (Lei, 2026-09-09
  23:29Z, verbatim, relayed by Kepler: *"for security-specific PRs, if you
  can't verify it yourself, you can ask Borges to merge in those cases"* —
  *"asking me to merge is purely formality in these technical cases"*). No
  security-PR merge click goes to Lei under this ruling — a narrow exception
  scoped to PRs Mira has reviewed; it does not relax the ⛔ list below for a
  security PR that also carries substrate-concept changes outside Mira's
  review scope. **Seat note (Lei, 2026-10-06, relayed by Mira):** the
  security-review seat moved from Mira to Zephyr "for now", with Mira reading
  additively; read "Mira" in this bullet's heading, its two 2026-09-09 rulings and
  the review-scope lines above as "the security seat", which today means Zephyr's exact-head approval, with Mira's read
  additive and never a substitute for it.

So "flip to Lei for substrate" is an **engram** rule. Do not extend it to a delegated-authority repo; do not relax it inside engram beyond what the 2026-09-27 delegation above grants. When you're unsure which repo's rule applies, the repo you're merging *into* decides.

## Owner merge (Lei, 2026-10-21 and 2026-10-22) — every project owner merges converged non-substrate PRs under the written gate

**Provenance.** Lei, 2026-10-21 23:31Z, in session, verbatim: *"do you think it's still
needed for EVERY PR to go through you if it's been fairy and peer converged?"* — his
proposal that converged PRs be merged by seats other than the merge seat under a test
gate, so that seat leads on design instead of spending itself on test runs. The same
evening he ruled the dev merge queue goes ahead with Kepler drafting the owner-merge
rule (Meeting 18, 2026-10-22 00:23Z, in the Square, carried its nine additions). On
2026-10-22 09:59 AM NY he asked, verbatim, *"And all owners can merge now?"* — read by
the drafting seat as setting the scope to every project owner, not only the Opus seats
named in the first proposal. **The ruling** came at 10:03 AM NY the same day, in
session, verbatim: *"You move ahead to allow owner merge when merge gates are met, and
we practice to see if anything will slip through. Don't be afraid of mistakes and
errors, as long as we can catch them and learn from them."* On the PR that adds this
section he ruled it merges as the team's consensus text without his read (*"No need to
bring it to me, just merge as your concensus version."*). So owner merge is **in force
from 2026-10-22 14:03Z**, on his word, not from the merge of this PR; the dev merge
queue he ruled on the same evening was later found unavailable to this repository (see
*Serialization* below). The stance he asked the team
to carry is the one in the quote: practise, catch the slips, learn from them.

**Amendment (Lei, 2026-10-22 23:23Z–23:31Z, in session with Kepler).** Lei asked whether
the author-cannot-press rule *"is a little too strict and fights with our owner merge their
own project rule"*, proposing *"as long as the PR passes our GitHub rule, we allow the owner
to merge, regardless of whether they are the author or not ... let the GitHub CI be the SSoT
for merge-readiness. And the local full tests becomes reviewer's gate."* Kepler's answer, which
Lei confirmed verbatim as *"exactly what I want"*: **yes, the owner presses their own PR**; the
**colleague's recorded run on the exact head is the test gate**; the press **keeps the base
assertion and the owner's scratch merge on the current base** (the two steps that caught
that day's one dev red and every moved-base case); and **GitHub's rules are the mechanical
floor, not the test truth, because `pr-checks` runs static scrubs and a fixed set of guard
test files on `dev` PRs, never the full `tests/` suite** (the #1597 cost model; the merge queue that would run the suite is unavailable here, *Serialization*
below). Making CI the test truth needs the full suite back on `dev` PRs, a billing decision
of Lei's, not a policy edit. Lei: *"make that change now, don't wait tomorrow morning ... you
need to update all the surfaces that contains this merge rule, and make sure we don't leave
contradicting rules behind."* The surfaces changed in the same PR: this section, CLAUDE.md
§ *PR merge authority* and § *Every PR carries a baton*, and Kepler's own authority table.

**Who.** The **owner of the project the PR belongs to** (the Project Register in the
Workshop, an in-world surface, not a file in this repo) merges it. Not the colleague
who approved it, and not a seat that merely holds the baton. **Scope:** owner merge
applies on every org repo whose merge authority is the team's (engram under this
policy, umwelt, local-model-lab, gpu-broker and the rest under the 2026-08-22 grant);
soma stays Ariadne's as principal approver and merger (*Scope* above). Two groups go
to the **merge seats** instead (Kepler and Borges on all org repos, with Ariadne on
umwelt and soma, per *Scope* and *Who presses the button* above):

- a PR with **no project owner**, the **merge-back** of a release branch into `dev`
  (`docs/internal/RELEASING.md` step 10), and a **reconcile** PR. The two release
  actions that write to `main` or to `release/*` (the fast-forward of `main` to the
  head of the release PR, and a soak-fix copy into `release/*`) are NOT a merge
  seat's: the rulesets let only a release-team account make them, so they go to
  **the release seat** (same page, steps 5 and 7);
- *(retired 2026-10-22 23:31Z, see the Amendment above)* ~~a PR whose author is its
  project owner goes to a merge seat for the press~~ — **the owner presses their own PR**
  once the record below is posted; the hand-off to a seat was clerical once the record
  existed, and the record is what any seat checks;
- a seat whose **own user's authority table does not grant merge clicks** (as of
  2026-10-22, Aleph and Clio, whose user has not granted it, so a merge click is a
  destructive action outside this repo's delegation for them): that seat runs the
  gate and posts the record; a merge seat presses.

**Nothing below widens the ⛔ list or touches the security read.** An owner never
merges a ⛔-list (substrate-concept) PR: it goes to Lei first, or, when it implements a
design he already read, through the 2026-09-27 delegation's two steps (the PR links the
read design; the merging seat writes the conformance check), and those two steps stay
with the merge seats. A security PR carries the security seat's exact-head approval as
its gate (Zephyr since Lei's 2026-10-06 ruling, Mira additive; see the seat note under
*Who presses the button*) and
is merged by the merge seats under the two directions in *Who presses the button*; an
owner does not click it. The owner merges exactly what the merge seats could already
merge on a fix or non-substrate PR, by exactly the same gate.

**The gate the owner runs, in full, before pressing the button.** The merge seat's
gate did not get easier because more seats hold it; it got written down so that any
seat can run it and any other seat can check that it ran.

1. **A reviewer-fairy round is on the PR's own record, it preceded the peer
   pass, and it converged** (the order is a rule, not a diagram — CLAUDE.md § PR review convention).
   Converged means one of three, and the merge comment states which: the round
   returned APPROVE; or it returned COMMENT with zero blockers and every actionable
   suggestion either folded (commit named, from 2026-11-04) or answered in writing on the PR; or the
   three-round cap was reached with the open findings listed in the PR body. A
   COMMENT round with an actionable suggestion neither folded nor answered is NOT
   converged, whatever its blocker count. *(Kepler, 2026-11-04, after two seats read
   this item and CLAUDE.md's merge-seat check differently. Ground: the reviewer-fairy
   spec defines convergence as "no blockers AND no actionable suggestions", and a
   fairy posting under its author's login cannot APPROVE that author's PR, so its
   verdict label alone is not the test. This REPLACES Kepler's 2026-10-17 strict
   reading (Square crossing seq 12169, raised by Mira on #3886; stated for that day and
   applied unevenly by seats since), under which a COMMENT round with author-folded suggestions needed
   a round 2 on the folded head, or the cap's listed findings, before the peer; merge
   comments of that period follow the old rule. Why it changed: the fold is checkable
   by the peer (commit named on the PR), the peer's read starts from it, and a round 2
   whose only input is the author's fold of the previous round's suggestion is the
   process reviewing itself, which the cap names as the stopping signal. Unchanged: a
   suggestion neither folded nor answered on the PR is an open finding.)*
2. **A non-author peer APPROVE whose commit is the PR's current `headRefOid`, and
   whose `submitted_at` is not earlier than the head commit's committer date.**
   Compare the oid and the two timestamps; never trust `reviewDecision` alone. The
   second check exists because an `update-branch` can re-point an older approval at a
   newer head (see *Merge-seat test rules*, last subsection). Any later commit needs a
   fresh approval. **The colleague's own test run on that exact head, stated in the
   approval with its count and platform, is the test gate** (Lei, 2026-10-22 amendment):
   a bare APPROVE without a run is a read, not a gate, and the owner asks for the run
   before pressing.
3. **`pr-checks` green on the EXACT head** (Luria, Aleph): mandatory, because a
   scratch merge on a Mac cannot see what the Linux runner's SQLite does. It is the
   **mechanical floor, not the test truth**: on `dev` PRs it runs static scrubs and a
   fixed set of guard test files, never the full `tests/` suite (#1597), so a PR can be
   CI-green with a red test in it; items 2 and 4 carry the tests. Read
   `mergeStateStatus` first; `CLEAN` ends it; anything else goes through the
   deduped rollup and the cancelled-same-SHA rule in *Merge-seat test rules* below.
4. **The seat's own run, on a scratch merge of the base tip plus the head** (rules 1
   and 2 of *Merge-seat test rules*), with the repo's **guard set** — and the guard set
   now **names the whole-tree scanners that no changed file names** (Luria):
   `tools/scan-leaks.py`, `tools/test_id_set_check.py`, and the whole-tree
   guards (`tests/test_syspath_shadow_guard.py`, `tests/test_tiers_manifest.py`,
   `tests/test_run_touched_tests.py`, `tests/test_ci_wiring_guard.py`,
   `tests/test_engramd_read_scan.py`'s real-tree cells), **and the source-scanning
   ratchets** (Kepler, 2026-11-08, after #4478). What puts a test on this list: a
   cell that **enumerates** shipped files (a walk of `src/`, `tools/`, the skills or
   the docs; `git ls-files`; a glob over a shipped directory), directly or through
   the tool it runs, instead of naming them, so a change to a file the test never names can redden it while rule 1's
   basename selection never picks it; the guard set is then the only place it runs
   before the nightly. Today they are `tests/test_atomic_writer_modes_4013.py`,
   `tests/test_truncating_writes_ratchet_4380.py`,
   `tests/test_forum_process_spawn_ratchet_3518.py`,
   `tests/test_no_db_creation_before_bootstrap_4107.py`,
   `tests/test_no_unbounded_upward_walks_3983.py`,
   `tests/test_prose_home_sweep_3863.py`, `tests/test_prose_tail_6c_venv.py`,
   `tests/test_raw_home_read_gate_3863.py`, `tests/test_home_path_gate.py`,
   `tests/test_lesson_pattern_audit_shipped_3941.py` (it copies every shipped
   hook into its tree), `tests/test_docs_layout_guard.py`,
   `tests/test_policy_guard_list_4487.py` (it reads this document and names no module),
   `tests/test_metadata_parse_drift_guard_3413.py`,
   `tests/test_json_extract_metadata_guard_3485.py` and
   `tests/test_no_retired_cascade_store_asserts.py` (it scans `tests/` itself) and
   `tests/test_forum_redirect_origin_1459.py` (it derives the forum's sender set with
   `git ls-files` and a walk of `src/` and `tools/`).
   The list is a floor, not a census: a PR that adds a test with this property adds
   its name here in the same PR, and a seat that finds one missing adds it. Verify
   the list with `git ls-files` **in a checkout of the base tip**, not the seat's
   working clone (a clone parked on an older commit lists an older tree), before
   pasting it; a guard that moved is not a guard that ran. *Evidence:* #4478
   (2026-11-08). The seat's run for #4464 selected every test naming a touched file
   plus the guards it then ran by name (docs layout, name-leak, upward walks,
   `sys.path` shadow, tiers manifest), and read green; `test_atomic_writer_modes_4013.py`,
   which scans `src/` for `mkstemp` + `os.replace` writers and names no file, went
   red on `dev` at 05:23Z, was found as #4478 at 07:00Z (98 minutes), and was fixed
   by #4480 at 07:41Z (about 2h18m red). Two hours after the miss the seat held
   #4472 on two guards green on `dev` alone: `test_no_unbounded_upward_walks_3983.py`,
   which was already among the five the seat ran by name, and
   `test_prose_tail_6c_venv.py`, which sat in no guard list and reached the run
   only because the ratchets were by then in it by name.
   **Record the base SHA the scratch merge used** (`git rev-parse origin/<base>` at
   the time of the run, after a fetch); item 8 asserts against it and item 10 names
   it (Borges, 2026-10-22).
5. **The PR's changed or added tests appear EXECUTED BY NAME in the saved output**
   (Borges; #4041's S9, which skipped on the seat's SQLite and passed only on CI's).
   For a `dev` PR that output is the scratch run's, since the full suite does not run
   on dev PRs; for a `merge_group` or `main` run it is the CI log. A selective green
   proves nothing about tests it never ran. Save the full runner output to a file and
   read the names from it.
6. **One mutation, and it names the dimension it varies** (Sol, Zephyr): a mutant that
   reddens a neighbour by crashing is not a control. Locate the target from the test's
   own asserted text, print the mutated line, assert a non-empty diff, predict the red
   set, and judge against the prediction (none red = bad aim; too many = a neighbour
   broke). Run the mutation and publish its result in **separate commands**.
   - **No-CI repos** (local-model-lab, gpu-broker): the scratch run plus the mutation
     **is** the gate, and the merge record says so (Clio, Sol, Borges).
   - **CI-only or docs PRs:** there is no mutation target; the substitute is a
     red-first run of the guard the change touches, or the stated line
     "no mutation applicable" (Ariadne).
7. **The baton's peer→merge advance stays the readiness signal** (Zephyr): the
   **author** runs `baton advance` once the colleague has approved, the server gates it
   on GitHub's own approval, and the owner merges only from `phase: merge`. The phase
   keeps its meaning because the gate reads GitHub, not the caller. **An owner pressing
   their own PR closes the baton with `baton close PR-N --status merged` after the
   read-back**: the `baton merge` verb refuses a turn held by the author (CLAUDE.md
   § *Every PR carries a baton*), a tool rule written for the seat model; its
   relaxation is tracked as a tool issue, and `close` is the record either way.
8. **The merge command pins the head, and the seat pins the base:**
   `--match-head-commit <full 40-char headRefOid>` read from
   `gh pr view --json headRefOid` in the same step, and **in that same step** a
   re-fetch and an assertion that `origin/<base>` still equals the base SHA item 4
   recorded. If the base moved, re-run item 4's scratch merge on the new tip before
   pressing: `--match-head-commit` pins only the head, never the base, so without
   this check two owners can each pass a scratch run on the same base and the second
   presses on a tree it never tested (the 2026-09-13 jointly-broken case, Borges).
   The assertion is chained to the merge with `&&`, never left to `set -e` (see
   *Merge-seat test rules*, rule 5).
   The release PR into `main` is **never merged with the button**: `main` is
   fast-forwarded to its head (`docs/internal/RELEASING.md`, steps 6 to 8), and there
   is no hotfix PR into `main`. A release branch that carries its own commits comes
   back into `dev` as a **merge commit** (same page, step 10); every other PR into
   `dev` squashes, and a soak-fix copy into `release/*` squashes; a member of a
   linked stack merges with `gh stack merge` (plain `gh pr merge` is refused there).
   On a repo with no `dev` branch (local-model-lab and gpu-broker, both read
   2026-11-03), every PR targets `main` and merges as a **squash**. The fast-forward
   and merge-commit rules above are engram's rules for releases; they
   do not apply there. Both of those repos set `squash_merge_commit_message` to
   `COMMIT_MESSAGES`, so the branch's commit messages ride into the squash and the
   close-keyword read covers them (CLAUDE.md § PR — issue closure, check 3).
9. **On umwelt, merge does not promote** (Ariadne): prod promotion stays a separate,
   tested-first step by the deploy seat.
10. **The merge record, posted on the PR as the merge comment, names:** the merge
    SHA, this gate's items 1–3 with times and oids, **the base SHA the scratch run
    used and that it still held at the press** (items 4 and 8), **which suite ran and
    on which platform** (Aleph), the counts read from the saved output, the mutation and its
    red set, any fairy-cap note, any load flake that rule 4 of *Merge-seat test rules*
    cleared (its issue and controls), and the read-back of every closed issue (the
    post-merge completeness read of CLAUDE.md § PR — issue closure). The commander
    samples merge records at day-end; a record missing an item is the finding,
    whatever the merge's outcome.

**Serialization — the property two holders kept by hand, now kept by the base
assertion.** The dev merge queue (`merge_group` runs of `pr-checks`) was the planned
replacement for the "one holder per repo lane" rule: every merge waits its turn and
re-runs the checks on the queued tree, which closes the jointly-broken case (two owners
each passing a scratch merge that omits the other's PR; Borges, 2026-09-13). **It is not
available to this repository** (measured 2026-10-22 16:05Z: the rulesets API refuses a
`merge_queue` rule on the `dev` ruleset with `422 Invalid rule`; GitHub offers that rule
only to public organization repositories and to private repositories on Enterprise
Cloud, and engram-agents is a private organization on the Team plan), and it would
conflict with this repository's stacked-PR practice even where offered (a merge-queue
rule disables gh-stack's `/stacks` on the repo, gh-stack discussion #392; sibling
threads there report a squash queue dropping stacked children). So the **base-SHA assertion of items 4 and 8 is the standing
serialization mechanism, not an interim one**: a press on a base the seat's own run never
saw is refused by the seat's own check, and the re-run on the moved tip is what a queue
would have done. The default-lane rule of *Who presses the button* stays suspended for
owners; the courtesy of **one line in the Square before merging into a tip another owner
may be merging into** stays, so a moved-tip re-run is expected rather than a surprise;
*whoever starts a multi-PR stack finishes it* (unchanged). The `merge_group` support in
`pr-checks` (#4060) remains on `dev` as dormant wiring, harmless until a plan change makes
the queue available; that plan change is Lei's call, with the zero-cost default of no
change.
*Queue status: NOT AVAILABLE on this repository (Team plan, private), measured 2026-10-22 16:05Z.*

**What the merge seats still do.** Kepler and Borges (Ariadne on umwelt) keep the
seat for ownerless PRs, release merge-backs, reconciles, the seats whose user has not granted
merge clicks (*Who* above), ⛔-list and security PRs, and any merge an owner declines ("I'd rather a second pair
of eyes" is always allowed and never a demerit); the commander keeps the day-end
sample of merge records. The seat is no longer the serial point; the written gate is.

*Origin: Lei's 2026-10-21 23:31Z proposal and 23:53Z–00:02Z rulings, Meeting 18's
nine additions (2026-10-22 00:23Z, Square), Lei's 2026-10-22 09:59 AM NY question
and 10:03 AM NY ruling, both in session with Kepler. Drafted by Kepler 2026-10-22;
reviewer-fairy round 1 folded the same day. Amended 2026-10-22 23:23Z–23:31Z on Lei's in-session
ruling (owner presses own PR; colleague's exact-head run is the test gate; CI the floor).*

## ⛔ Lei reads before merge — SUBSTRATE FEATURES

Flip the baton to Lei (the maintainer) and **do not merge it yourself**, even after colleague convergence + CI-green, if the PR does one of the following **and Lei has not read its design** (if he has, see "Delegated by Lei, 2026-09-27" above — the team merges, after the two written steps there):

- adds or changes a **node type or edge type**
- changes **confidence computation** (quote_type / reasoning_type maps, propagation,
  calibration anchors) — i.e. it changes **what every node means**. Per the 08-25
  sharpening above, a health / diagnostic score computed **over** the graph moves no
  node's semantics and is *not* this. (Same test, restated for this list — the
  sharpening above carries the canonical statement; do not add a third.)
- changes **retraction / supersede / cascade** semantics
- changes **standpoint / falsification / calibration-exposure** machinery
- changes the **goal lifecycle** (states, dormancy, tension)
- changes the **reasoning-type machinery** or adds a reasoning type
- adds a **new MCP tool with conceptual semantics** — a new *epistemic operation*, not plumbing (a backup tool, a stats field, or a CLI is not this)
- adds a **new epistemic mechanism or research instrument** (e.g. #1399's retraction-initiation provenance — a free-will/agency measurement)

These are **conceptual-direction calls**. The implementation can be perfect and the *direction* still wrong — that's what Lei is gating, and it's the part the agent team can't self-certify.

**When in doubt, treat it as substrate and flip to Lei.** The cost of waiting a day is small; the cost of a wrong-direction substrate change that ships is a migration + a graph full of nodes written under the wrong model.

## ✅ Merge after peer review — FIXES + NON-SUBSTRATE

Merge once colleague-converged + CI-green, **no per-PR Lei permission needed**:

- **bug fixes / regressions / test fixes**
- **tooling, infra, CI** — backup, baton, forum, hooks, packaging, the affected-tests gate, install/upgrade
  - **The baton system is on this side in full, its design included (Lei, 2026-09-30,
    in session, verbatim: "baton system can be considered convention and convenience,
    not substrate, feel free to improve it based on your real experience").** That
    covers the CLI, the gates, the forum's project store, the file format and the
    design notes for any of them. Before this ruling a baton *redesign* was held for
    his read as if it were substrate (#3055, the phase-and-turn design note for #3039,
    sat approved from 2026-09-27 to 2026-09-30 on that reading); it no longer is. A
    baton PR merges at the seat after the normal pipeline. Two limits. **A baton PR
    that also alters a concept on the ⛔ list is judged on that concept**, exactly as
    the "Gray areas" line on a fix that changes a substrate behavior says; "in full"
    describes the baton system, not whatever else a PR carries. **A change that makes
    the forum or the baton authenticate its callers is a security PR** under "Security
    PRs" above: the security seat's approval is its merge gate, and it goes to Lei only if it also
    changes a ⛔-listed concept outside its review scope.
- **docs, brand, i18n**
- **refactors** that don't change a substrate concept
- **substrate PRs that implement a design Lei already read** — only after the merge seat has linked his read and written the conformance check ("Delegated by Lei, 2026-09-27" above)

Surface notable ones in the **post-merge digest** so Lei can see what landed and why — but don't gate on them.

## When Lei is away — the standing when-away grant (merge-to-dev vs live-deploy)

**Confirmed and upgraded to a standing rule by Lei, 2026-07-10** (recorded in the commander's graph that day; his words: *"you got my authorization to merge substrate PRs … Unless otherwise stated, when I'm away, you have my authorization to merge on them"*). The 2026-07-08 day-grant interpretation below was confirmed retroactively at the same time; the grant no longer needs to be re-issued per day. "Otherwise stated" means Lei can suspend it for a day or a topic; absent that, it holds whenever he's away.

Substrate merges under the standing grant cover **`dev` only, and the merge only — never the deploy:**

- **Merge-to-`dev` is revertible.** Nothing touches the running substrate until Lei runs the live deploy (`install-local-marketplace.sh` rebuild + `/mcp` reconnect + reload/restart). The **irreversible step — the live deploy — stays Lei's**, always.
- So a substrate PR that is colleague-converged + CI-green, whose *design* was already settled with Lei (or with the architect/commander) and carries **no design novelty at merge time**, may be merged to `dev`. It joins a **merged-but-not-live-verified batch** that Lei deploys + verifies together on return.
- **Log notable substrate merges** made under the grant (post-merge digest / `ask-lei`) so Lei sees what landed and why — an awareness surface now, no longer a per-grant confirmation gate.

The boundary outside the grant is unchanged: substrate PRs with **unsettled design or design novelty at merge time** still go to Lei first, away or not. The carve-out remains merge-not-deploy, dev-only, settled-design-only.

## At merge time — the natural checkpoint

The baton's **flip-to-maintainer** step is where this rule lives in practice:
- **Substrate feature whose design Lei has not read:** the baton flips to Lei and *stays there until he merges* (or, under a live day-grant, to the grantee for a dev-merge per the section above).
- **Substrate feature implementing a design Lei read:** the baton flips to the maintainer seat; the merge seat links his read, writes the conformance check, then merges.
- **Fix / non-substrate:** a colleague-converged + CI-green PR can be merged by the team.

So at every `baton flip … <maintainer>`, ask: *is this a substrate concept change, in this repo — and has Lei read its design?* If substrate and not read (and no live grant applies), Lei reads first.

**Merging a `[part of #N]` PR:** tick the workstream line the PR's own body names (the author, not the merge seat, knows which line their PR corresponds to) in #N's body, in the same pass as the merge (checkbox, or a `— landed as #M (sha)` suffix) — see `CLAUDE.md`'s `## PR — issue closure` for the full convention (#2617). A `[part of #N]` PR never auto-closes #N, so this tick is the only thing that keeps the epic body from reading as open work nobody finished.

**Merge-readiness gate (the other half — see `CLAUDE.md`):** a PR is merge-ready only when review-converged **and** CI-green on the current tip. The enforced non-author-approval property is GitHub-native (`require_last_push_approval` + ≥1 non-author approval on the current head, enforced by the branch ruleset; Lei, 2026-07-02 — the standalone `non-author-approval.yml` workflow was deleted 2026-08-30 on Lei's ruling) — a colleague-converged deputy approval satisfies it; a maintainer cannot self-approve their own PR. **What the deleted workflow actually enforced, recorded so the record is accurate about what was given up (Ariadne's correction, 2026-08-30):** ALL-reviewers-approved — it failed a PR when any reviewer's latest state was COMMENTED even if another reviewer approved. **Lei ruled that property out DELIBERATELY, not as redundancy** (his words in substance: an old reviewer can be hard-blocked by token limits, and requiring approval from a sleeping agent is bad design). The COMMENTED-reviewer case is therefore **discipline-only by design**: the review convention's rule that only an explicit colleague APPROVAL advances the baton — a COMMENTED-with-considerations verdict never reads as ready — is the layer that covers it, and it is vigilance, not mechanism, on purpose. Merge-authority (this doc) and merge-readiness (that gate) are orthogonal: a PR needs *both*.

### Baton turn-target corollary

The "flip to Lei" half applies **only to substrate PRs** (in engram). For a fix / non-substrate PR, the maintainer (pool sentinel) — **not Lei** — is the merge-ready turn-target; the agent team merges it directly after colleague-convergence + CI-green. Setting `turn: lei` on a non-substrate baton is the anti-pattern: it produces a stuck baton that no one can advance, because Lei isn't gating that class (origin: PR #1333, a forum brand rename, whose baton was parked at Lei and stuck after merge — #1442). Match the turn-target to the class: substrate whose design Lei has not read → Lei; everything else, including substrate implementing a design he read → maintainer.

## Gray areas

- **A fix that also subtly changes a substrate behavior** → substrate (treat the conceptual change as the dominant axis).
- **A new tool that's pure plumbing** (backup, stats, a CLI) → not substrate, even though it's a "feature."
- **A docs PR that documents a substrate concept** → not substrate (it's docs); but a docs PR that *defines new semantics* is.
- **A substrate PR during a Lei-absence day-grant** → merge to `dev` is OK under the grant (design-settled, logged); the live deploy still waits for Lei. See "When Lei is away" above.
- **A build unit of a design Lei read, where the PR had to depart from that design** → if the departure changes what a node, edge or verb means, it is a new concept: flip to Lei. If it is plumbing, the team merges and the conformance check names the departure.
- Still unsure after this → flip to Lei. Defaulting to the gate is cheap.

## Origin

The 2026-06-24 v0.1.3 merge wave merged #1399 — a retraction-initiation provenance **research instrument** (a substrate feature) — without Lei's pre-merge read. It was additive + dormant, so low-cost — but it's exactly the class that should get his eye first (the "what do we measure about agency, and how is it enforced" question is a direction call, not a code call). This policy is the durable form of that lesson. The per-project scope (umwelt/soma delegation) and the Lei-absence day-grant carve-out were added 2026-07-08 after the policy's own SSoT-file gap — this doc had been authored 2026-06-25 but left unmerged for two weeks — surfaced live during a substrate merge-authority call (engram-alpha #1654) that had to be reasoned from memory because the doc wasn't in the repo.

## Moved from CLAUDE.md § PR merge-readiness — CI-green gate (both modes) (moved 2026-10-03, #3322)

*Verbatim; moved, not edited. CLAUDE.md keeps the rule and a pointer here.*

*Note (2026-10-29, #4255): the coordination-topology wording in the quoted text below was retired; the live rule is CLAUDE.md § PR merge-readiness — CI-green gate.*

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

**Read the two fields in order — `mergeStateStatus` first, the rollup only if it is
not `CLEAN` — and the asymmetry between them IS the rule: `CLEAN` is sufficient to
conclude green; anything else (`UNKNOWN`, `BLOCKED`, `BEHIND`, `DIRTY`, `UNSTABLE`) is
NOT evidence of red and must fall through to the *deduped* rollup rather than fail the
PR.** Falling through is not the same as being ready to merge, though: `DIRTY` is a
merge conflict (8 of 8 measured also read `mergeable: CONFLICTING`) and has to be
resolved no matter how green CI is. Reading "not CLEAN" as "not green" is a different
bug in the same place — and it is the common case, not an edge case: measured
2026-08-30, 12 of this repo's 19 open PRs read non-`CLEAN` (8 `DIRTY`, 4 `BLOCKED`).
Neither state says anything about whether CI passed. The naive read of the rollup —
"every `statusCheckRollup[].conclusion` is `SUCCESS`" — is WRONG too, and this doc
carried it until 2026-08-30:

1. **`mergeStateStatus: CLEAN` is the verdict — stop there.** GitHub computes it from
   the latest run per check, so CLEAN already accounts for superseded runs. No
   per-check reading is needed.
2. **The rollup does NOT dedupe.** `statusCheckRollup` is scoped to the head commit —
   runs on superseded SHAs never appear in it — but it carries **every** run on that
   commit, not just the latest. So a stale `FAILURE` or `CANCELLED` from a superseded
   same-SHA run sits in the list beside the current `SUCCESS`, and requiring every
   entry to be `SUCCESS` false-blocks a genuinely green PR. Measured 2026-08-30 on
   #2371: `mergeStateStatus: CLEAN` while its rollup carried `pr-checks=CANCELLED`
   beside `pr-checks=SUCCESS`.
3. **Two runs of one check can land on the SAME SHA — that is what the dedupe is
   for.** Filtering by head SHA adds nothing, since the rollup is already head-scoped.
   On #2410, `pr-checks` completed `CANCELLED` at 13:19:06Z and `SUCCESS` at 13:19:42Z,
   36 seconds apart, both on `82ba043e`; on #2419 two `pr-checks` `FAILURE`s (14:29:02Z
   and 14:44:41Z) sit beside the `SUCCESS` at 14:46:01Z, all three on the merged tip
   `edc20d56`. The workflow fires on `edited` as well as `synchronize`, so a trigger
   that changes no code can start a second run on a SHA already checked. What the
   earlier run leaves behind turns on overlap, and the two above are one of each: #2410's
   second run was **created** while the first was still running — an overlap in the runs
   (`gh run list --json name,headSha,createdAt,updatedAt`) but not in the rollup, whose
   job-level `startedAt` for that run falls a few seconds later, after the first had
   completed — so `cancel-in-progress` on the per-PR concurrency group killed it, hence
   the `CANCELLED`; #2419's runs never overlapped (each started only after the previous
   had finished), so nothing was cancelled and those `FAILURE`s are just the runs' own
   conclusions. The dedupe is load-bearing WITHIN one SHA — and only there: there is no
   across-SHAs case, because a superseded SHA's runs are not in the rollup to begin with.
4. **Use the rollup only to find WHICH check to read when the state is not CLEAN** —
   and dedupe to the latest run per check name before judging, where *latest* means
   an **unfinished** run (status not `COMPLETED`) is the latest for its name, else the
   most recently **completed**: `_dedupe_checks_by_latest()` keys on status first (never
   on `completedAt`'s emptiness: an in-flight run can carry null, `""` or the Go zero
   value) and then compares `completedAt` among finished runs; ordering by `startedAt`
   instead picks a different winner whenever one run starts earlier but finishes later. `gh run list --branch <branch> --json
   name,headSha,conclusion,updatedAt` in time order is the direct way to see which run
   is the current one — that command exposes no `completedAt`, but on a finished run
   its `updatedAt` is ≈ the completion time (measured ~1s later, on five runs).
   This dedupe is the test verdict, but see rule 3 of "Merge-seat test rules" below: a
   cancelled same-SHA run can still block the merge; rerun it (or, if the PR body or title
   changed since that run, trigger a fresh run instead: rule 3). Which gate reads which:
   this dedupe answers the readiness question (is the newest run of each check green)
   for the flip and merge-signal gates. The press step answers GitHub's merge-gate
   question (did every run on this head succeed) with the strict read of rule 5.
5. **`UNKNOWN` is not red — measured, not reasoned.** `mergeStateStatus` is computed
   asynchronously and only for open PRs. It has been observed reading `UNKNOWN` twice a
   minute apart on a PR that was genuinely fine, then `CLEAN` minutes later, then
   merging without incident; and the REST `mergeable_state` still reads `unknown` after
   a successful merge, on a PR that is definitively merged with a merge-commit SHA. So
   the field is trustworthy in one direction only: `CLEAN` concludes green, `UNKNOWN`
   concludes nothing. Treat it as the *pending* case below (hold, re-check on the next
   tick), never as a failure.

**Reference implementation: `_pr_ci_state()` in `src/engram/tools/baton.py`** (with its
helper `_dedupe_checks_by_latest()`) does exactly the above — returns green
immediately on `mergeStateStatus == CLEAN`, and otherwise dedupes the rollup to the
latest run per check name before its red pass and its pending pass. The tool has been
correct since #1366 — the defect was found and the dedupe prescribed on 2026-06-24
during release dogfooding, and shipped in code then; only the prose half went
unassigned, and stayed unapplied until the same defect was re-derived from scratch on
2026-08-30. For those two months a hand-check that followed the doc could false-block
a PR the baton CLI would have called green. Read the rule off what those two
functions **do** (their behaviour, not their prose) rather than re-deriving it — if
doc and tool drift again, one of them is now visibly contradicting a named function,
and the doc is the side that moves.

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

*Amended 2026-08-30: two PRs that same afternoon carried superseded runs in the rollup
beside a genuinely green tip, both on the tip's own SHA — #2410 (one stale `CANCELLED`)
and #2419, the v0.5.0-rc1 release cut (two stale `FAILURE`s). The pre-amendment rule reads both as not-green, and
would have held the release cut. Both are merged and both still carry those entries, so
merging does not clean the rollup and the evidence stays checkable.*

### Merge-seat test rules (added 2026-10-06, #3479)

Five rules for the seat that runs tests before merging and then presses. They sit with
the CI-green gate above because they answer the same question from the other side:
CI-green says the PR's checks passed; these say what the seat's own run must be, how
to read it, and what the press step itself must check.

**1. The seat's own test run is on a SCRATCH MERGE of the base tip plus the PR head,
never on the head alone.** The run answers *does the base work after this lands*. A
head-only green is necessary (the PR works) and not sufficient (the base works after
it): another PR can land on the base between the peer approval and the merge, and the
two compose badly. No review layer sees that tree except the merge seat — the fairy
and the colleague review the head.

```
git fetch origin <base> '+refs/pull/N/head:refs/pr/N'
git worktree add --detach <scratch> origin/<base>
git -C <scratch> merge --no-edit refs/pr/N
{ git -C <scratch> merge-base --is-ancestor origin/<base> HEAD && git -C <scratch> merge-base --is-ancestor refs/pr/N HEAD; } && echo MERGE-OK || { echo "MERGE-FAILED: stop"; false; }
# run the PR's tests + the repo guard set here
```

**What "the PR's tests" means: the per-PR test scope (Lei, 2026-11-04).** The whole `tests/`
directory is **not** run per PR, by any seat: the author, the colleague or the merge seat. Each
seat's run is:

1. **The selected tests.** Select every test file that imports a changed module, or whose text
   names a changed file by its basename (`grep -rlF --include='*.py' <basename> tests`). Docs,
   skills, templates and configs are test inputs in this repo: on `dev` `786194ee` that grep finds
   4 test files naming `docs/compaction-continuity.md`, 9 naming
   `tools/engramd_read_scan.config.json` and 14 naming `template.CLAUDE.md`. So a doc change
   selects the tests that name it. A changed file that no test names selects nothing beyond the
   guard set. The selection is never the whole suite. Until #4358 lands,
   `tools/run_touched_tests.py` still falls back to the full suite for a file it cannot resolve,
   so a seat applies the name selection by hand.
2. **The guard set** of item 4 in *Owner merge*: the whole-tree scanners and guards that no
   changed file names. They always run. Run `tools/scan-leaks.py` with `--root .`: its default root is
   `src/engram` only, so a bare run misses `tests/` and `docs/`, while CI's
   `tests/test_scan_leaks.py` scans the whole repo. On 2026-11-04 a bare run read clean on a
   tree that CI then failed.
3. **A coverage flag on new code**, advisory, not a gate. Changed Python code that no selected
   test reaches is a red flag for the reviewer to answer on the PR. It is not a gate because
   coverage cannot see code that runs as a subprocess, which covers this repo's hooks and shell
   scripts, until subprocess coverage is configured.

The **whole suite** runs in two places only: the scheduled nightly on `dev`
(`.github/workflows/nightly.yml`), which is the backstop for what per-PR selection misses, and the
release cut, before a tag. A red the nightly finds goes to the PR that caused it.

*Why* (Lei, in session 2026-11-04, relayed verbatim by Borges): "I think most PRs won't need the full test suite, just the subset picked for touched
tests should be good enough, and the nightly CI will catch full suite issues". The whole run took
53–67 minutes per PR on the merge seat's host. Run three times per PR (author, colleague, merge
seat), it was the main delay on 2026-11-04. *Bound:* per-PR selection can miss a whole-suite-only
red. The nightly then catches it within a day, at the cost of `dev` reading red until the fix
merges. That is the trade Lei chose.

The one fast path is "head is already rebased on the base tip", where head == merged
tree; assert `git merge-base --is-ancestor origin/<base> refs/pr/N` before taking it.

*Evidence:* umwelt #878/#872 on 2026-09-29. On 2026-10-06, umwelt #1016 and #1017
against #1002; #1016 was caught on a scratch merge, and #1017 is the head-only-run
escape (the seat ran the PR's five test files on the head alone, 380 passed, and
merged; main was red on three cells minutes later).

**2. A failure seen on the scratch merge belongs to the PR only if that test cell is
GREEN on the base alone (or the PR adds it).** Otherwise it is the base's and goes to
the seat that broke it, and must not be held against the PR. To tell the two apart, run the failing cell on the base tip.

*Evidence:* Borges's run on umwelt #1021 (2026-10-06) kept main's own red off the PR.

**3. A cancelled same-SHA run can block the merge even when another run passed.**
Observed: a required status check can read as not-satisfied — the PR stays `BLOCKED`
and the merge is refused — when a run of that check on the head SHA was
`CANCELLED`, even though another run on the same SHA passed. `gh run rerun <the
cancelled run id>` cleared it. Never argue from the deduplication above ("another run
passed, so the gate is satisfied"); rerun the cancelled run, unless the PR body or
title changed since it (next paragraph).

The cause seen, on alpha #3489 only: two `pull_request` runs on the same SHA were
created in the same second, after a push and a PR-body edit seconds apart, and
concurrency cancelled one. On #3476 we know only that the newest same-SHA run was
cancelled by concurrency; what triggered the second run is not known. Which run
GitHub keys a required check on is not known either. "The newest same-SHA run" fits
#3476 but not #3489, where by `started_at` the cancelled run started five seconds
before the success and the PR still read `BLOCKED`. There is no documentation; the
fact and the remedy above are as measured.
Untested advice: edit the PR body before pushing, or wait for the push's run to start.

*Evidence:* two measurements, 2026-10-06.
(1) alpha #3476, ~16:43Z: the newest same-SHA run was cancelled by concurrency, the PR
was `BLOCKED` and the merge refused ("base branch policy prohibits"), even though an
older run was green and a valid approval existed; `gh run rerun` of the cancelled run
gave `CLEAN` as soon as it completed.
(2) alpha #3489, ~17:13–17:20Z: read `BLOCKED | pr-checks:CANCELLED, pr-checks:SUCCESS`
(two runs on the same SHA, started in the same second by a push and a body edit);
`gh run rerun` of the cancelled run gave `CLEAN | SUCCESS, SUCCESS`.

**A rerun replays the original event, payload included.** `gh run rerun` re-executes
the workflow against the event that created the run, with the PR body and title as
they were at that moment, not as they are now (measured; recorded in the `edited`
comment of `.github/workflows/pr-checks.yml`). So when the body or title has changed
since the cancelled run, do not rerun it. Trigger a fresh run instead (an `edited`
event, or a push), and read that run. For a body change: fetch the current body
(`gh pr view <N> --json body -q .body > <file>`) and re-submit it with `gh pr edit <N>
--body-file <file>`. For a title-only change, a title edit or a push. A red on a body or title step in a rerun made after such a change is an
artifact of the stale payload, not a PR defect, and must not be read as a blocker.

*Evidence:* alpha #4232, 2026-10-27 (Halley's measurement). The author fixed a
body-text close trap after the run was cancelled; the rerun checked the old body and
turned the body-trap step red while the live body was clean (#4235).

**4. A red that only the whole run produces is a load flake, not a PR red** (Kepler,
2026-10-25; condition four added on Luria's review). Since 2026-11-04 a whole run is the nightly's or a
release cut's (rule 1's per-PR scope); this rule reads those runs. It applies when all four hold:
- no PR in the run touches the failing test's file, any file that test runs (a
  subprocess target, for one), or the modules the failing cell exercises;
- the cell is green on the base alone;
- the cell is green on the merged tree alone, with the failing cell run by itself on
  each tree at least three times;
- the cell is green when co-run in one process with each PR's added or changed test
  files, conftest and fixture files included, one PR at a time (seconds per PR).

The fourth condition catches a PR whose new test leaves process state behind (a
leaked thread, a patched module, a changed working directory). The untouched cell
then fails only when it shares a process with that test. It passes alone on every
tree, so conditions two and three cannot see it. Luria measured that shape with a
stand-in: the untouched cell passed alone 3 of 3 times, and the PR's file passed
alone, but run together in one process the cell went red.

The first condition is about the code the cell exercises, not only about test files.
A PR that makes a module slower or racier while leaving its tests untouched can
produce a red that only appears under load. Three quiet runs will not reproduce
that, so the rule does not clear it.

Then the red counts against no PR in the run. File it as an issue, cite it and the
controls in every merge record of the run, and press. A red that reproduces on the
merged tree alone still stops the press.

Filing is the obligation. The remedy is to fix the cell, not to get into the habit of
rerunning: a timing cell that cannot hold under whole-suite load is a defect of the
cell.

Rule 2 does not decide this case, which is why the rule exists. Rule 2 tells the
base's reds from the PR's by comparing the same cell on the base and on the merge.
Here the cell is green on both trees alone and red only inside the whole run, so what
differs is the population and the load, not the tree. Rule 2's own test, whether the
base would be red in that same whole run, costs another full run and need not
reproduce.

*Evidence:* the batch of #4179, #4174 and #4176 on dev `b1d4c83c`, 2026-10-25. The
run on tree `e6a8ecb3` gave 1 failed and 26806 passed. The red was
`test_sys_path_hygiene_2923[writers-then-stage]`, which runs the stage profiler's
injected-delay `[request_read]` timing cell: a rise of −0.007 ms against a floor of
1.6 ms. The controls:
- the same cell passed when the run executed it directly;
- the sibling parametrization passed in the same run;
- the cell `[writers-then-stage]` alone passed 3 of 3 times on the base and 3 of 3
  times on `e6a8ecb3`;
- none of the three PRs touches the owner, the client or the profiler, which are the
  modules the cell exercises.

The presses went ahead and the red was filed as #4180. That ruling predates the fourth
condition: its controls did not include a co-run, so under this rule as written the
same red would need one before the presses. Its cause later proved
deterministic: an injected sleep before the wrapped call overlaps a late peer and
adds nothing. The fix to the cell is #4183.

**5. The press step gates on CI; it never prints past a pending state.** A press
follows a base move: the previous press in a batch, or another seat's merge. GitHub
recomputes `mergeStateStatus` asynchronously, so at that moment it commonly reads
`UNKNOWN`. Under the CI-green gate above, `UNKNOWN` means pending, never a value to
echo and press past.

The step has three parts, in this order:
- (a) in the press command itself, chained with `&&` into the merge, asserts the base is the SHA the run was built on, or the previous step's merge
  commit;
- (b) in the gate read, a separate tool call made before the press, polls until `mergeStateStatus` reads `CLEAN`, or until a read of every run of
  the repo's required check on the head (on engram's `dev` ruleset, which requires a PR, 1 approval, stale approvals
  dismissed on push, last-push approval and the status check `pr-checks`, that
  check is `pr-checks`) prints `success`. The poll is bounded at five minutes. Past that
  ceiling the gate read prints the last state it read, and the seat does not press;
- (c) after the press, asserts that the base's tree equals the tree the run predicted
  for that step.

```
gh api 'repos/<owner>/<repo>/commits/<head-sha>/check-runs?filter=all&per_page=100' --jq '
  if .total_count > 100 then "too-many"
  else ([.check_runs[] | select(.name == "pr-checks")]
    | if length == 0 then "none"
      elif any(.status != "completed") then "pending"
      elif all(.conclusion == "success") then "success"
      else "not-all-success" end) end'
```

The read prints `success` only when every run of the check on the head finished
successfully. Anything else means wait for `CLEAN`. If the PR stays `BLOCKED` on a
cancelled run, rerun that run (rule 3; a fresh run instead if the body or title changed
since it) and then wait for `CLEAN`. The read itself
keeps printing `not-all-success` after a rerun, because `filter=all` still lists the
cancelled run beside the rerun's success; #3489's head reads that way today. So the
read is a way through only when it prints `success`, and `CLEAN` is the way through
otherwise. Four details matter:
- **`filter=all`.** The API's default, `filter=latest`, can omit a cancelled run.
  On alpha #3489's head it returned two `pr-checks` runs of the three.
- **One page, guarded.** `per_page=100` counts check runs of every name on the commit,
  so past 100 a non-success run of the required check could sit unseen on page 2. The
  read prints `too-many` there, and the seat waits for `CLEAN`.
- **No ordering by time.** GitHub's keying of a required check on a SHA with several
  runs is not known (rule 3). On #3489 the `CANCELLED` run started five seconds
  before the `SUCCESS`, and the PR was still `BLOCKED`. Picking the newest run by
  `started_at` would have printed `success` there, and by `completed_at` it drops an
  unfinished run (the #2437 false green that #4176 fixed in baton's dedupe). So the
  read takes no newest run at all.
- **Deliberately stricter than the CI-green dedupe.** A stale red beside a newer
  success on the same SHA (#2410, #2419) reads `not-all-success` here, while the
  CI-green gate reads it green. At the press that costs only a wait: such PRs read
  `CLEAN`, and `CLEAN` is the other way through (b).

**How this fits the CI-green gate's `UNKNOWN` item.** The CI-green gate says `UNKNOWN`
is pending: hold and re-check. (b) is that re-check. It reads the underlying check
runs, the same evidence the rollup summarises (and stricter: no dedupe), instead of a
field GitHub has not recomputed, so it does not press past an unread state. `UNKNOWN` also covers mergeability (conflicts,
approvals). The read does not check those; the press command itself does, because
GitHub refuses a merge that the branch rules block.

**Guards stop the press by control flow, not `set -e`.** In the Claude Code Bash
tool, `set -e` does not stop a command: the tool runs the command inside an `&&` list,
and bash ignores `errexit` in a command that is not the last member of such a list. Measured on a merge seat, 2026-11-09:
`set -e; false; echo SURVIVED` printed `SURVIVED`. That was observed through the tool;
in a plain terminal, `bash -c 'set -e; false; echo SURVIVED'` exits 1, so do not re-test
it there. A press script that leans on
`set -e` or `set -euo pipefail` to stop at a failed guard is therefore fail-open: on
2026-11-09 a seat's script printed its own failure line and still ran `gh pr merge`,
and the v0.5.6 CHANGELOG pull request (#4545) was merged past a failing test that printed
in the same output. A guard written as a bare `test ...` or `grep -q ...` on
its own line stops nothing. So:
- A guard stops the press only by explicit control flow: chained to the merge with
  `&&`, or followed by `|| exit 1` (or `|| { echo "<NAME>-FAILED"; exit 1; }`).
- The seat reads the gate's verdicts as printed text before pressing, in a separate
  tool call from the press. The read prints one positive line per check (for example
  `BASE-OK <sha>`, `CHECKS success`, `TREE <tree>`); the seat reads them, and only
  then sends the press call. An exit status alone is not a verdict, because a
  wrapper's `echo` after the command masks it (`pytest ...; echo done` returns `echo`'s
  status, 0, whatever pytest did).
- The press call still re-asserts what can move: the base, chained with `&&` into
  the merge. This keeps item 8's race closure, since the base can move between the
  gate read and the press. (b)'s CI poll belongs to the gate read. (c)'s tree
  assertion runs after the press, and its printed result is read too.

```
git fetch -q origin <base> \
  && { test "$(git rev-parse origin/<base>)" = <base-sha> || { echo "BASE-MOVED: stop"; false; }; } \
  && gh pr merge <N> --squash --match-head-commit <head-sha>
```

A failed assertion prints `BASE-MOVED: stop` and the merge does not run.

*Evidence:* #4092 (2026-10-23) and #4182 (2026-10-25) were each pressed on a printed
`UNKNOWN` with no gate, four minutes and three seconds respectively after the base
moved. Their check runs were read
green only afterwards. Batch F (#4185 and #4187, 2026-10-25) was pressed with a gate,
but one that sorted by `completed_at`, which drops an unfinished run (none existed).

*The read, measured before writing it down (2026-10-25):*
- **Real heads:** #4183's head (one success) prints `success`. #4185's head (success
  plus cancelled) prints `not-all-success`. Its pre-press re-read showed `CLEAN`, but
  the press step itself gated on a newest-run read, the read this rule replaces. #3489's head
  (cancelled 17:13:08Z, success 17:13:13Z, success rerun 17:17:17Z) prints
  `not-all-success`.
- **Synthetic run lists:** no runs prints `none`. Success plus in-progress, and queued
  alone, print `pending`. Cancelled then success, in either order, prints
  `not-all-success`, as do stale failure plus success, and skipped.

### A workflow fix merged into the base does not reach an open PR until it has a new head commit

Read this when an older PR is red on a check whose workflow was fixed after the PR opened.

**The measured remedy is `gh pr update-branch`.** A fix to `.github/workflows/` merged
into the base (a corrected install step in `pr-checks.yml`, say) did not reach two
already-open PRs' checks until `gh pr update-branch` gave each a new head commit. A
rebase or a push also makes a new head commit and fires `synchronize`, so it is
*expected* to behave the same; that is not measured. Re-triggering without a new head did
not work: an edit-triggered run (`pr-checks` listens to `edited`) tested the stale merge.
`gh run rerun` was not tried here.

*Evidence, two PRs, one afternoon; not a statement of how long GitHub takes to refresh a
merge ref.* 2026-10-21, #4003 and #4006. #4005 merged into `dev` at 14:16:05Z. The
edit-triggered runs started at about 14:36Z, roughly 20 minutes later, and ran the old
install step and failed. Re-read live at about 14:40Z, both PRs' `refs/pull/N/merge`
still had the pre-#4005 `dev` commit as parent, and a REST read of the PR did not refresh
it. After `gh pr update-branch`, the runs started at about 14:41Z used the new step and
passed. The merge-ref reads were live and cannot be reconstructed now.

**Approvals after `update-branch`.** CLAUDE.md § "PR review convention (when the PR
has a colleague)", paragraph "The merge gate itself", says a new commit after approval needs a fresh
peer review and approval, and that the `dev` ruleset enforces this mechanically
(dismiss-on-push + last-push approval). At the `update-branch` step on those PRs the
ruleset did NOT dismiss the approvals; it re-pointed them to the new head, so the
mechanical enforcement that paragraph names did not fire, and `commit_id == headRefOid`
passes. (#4006 also carries an earlier DISMISSED review from a normal push; that is a
different step.) So the CLAUDE.md gate is satisfied mechanically by a review the peer
never gave on that commit. As a seat rule on top of it (Kepler, stated and applied on
#4003, 2026-10-21: a fresh review object, never the re-pointed one), the merge seat
checks that an approving review's `submitted_at` is later than the `update-branch` merge
commit.

### Batched whole-suite gate (Kepler's interim of 2026-10-24/25; superseded for per-PR runs 2026-11-04)

**Status (2026-11-04).** The per-PR test scope under rule 1 removes the per-PR whole run that this section batched, so it no longer applies to per-PR presses. It is kept as the record of what was measured.

**Original status.** Kepler ruled this as an interim on 2026-10-24 (crossing 16285), first for five
PRs, then for that whole day (16406). He ruled it again on 2026-10-25, for that day (that crossing's seq is not located here). His condition for it
to outlive those days is that it lands here, in a PR Lei reads.

**Why.** The merge seat's own run was then the whole `tests/` directory on a scratch merge. That was a practice, not rule 1's text, which says "the PR's tests + the repo guard set"; an earlier version of this sentence called it rule 1. On the seat's Linux host that took 55–67 minutes on those two
days (26,257 to 26,888 cells passed, 367 skipped), so a serial seat presses about one PR an
hour. A batch shares one whole run
among several PRs.

**What it gives up.** This section is a bounded exception to rule 1 of *Merge-seat test rules*
("does the base work after this lands"): that question is answered only for the last PR in
the batch. Two costs follow. First, the trees between presses: each press lands a tree nobody
ran, and the run tested only the final one. Second, one red PR holds its batch-mates: on
2026-10-25 a red in one PR's tests sent the whole batch back, and the other PRs waited for
a re-run they would not have needed serially. A semantic clash between two PRs is still caught, because the
final tree contains both. In Kepler's words: "the thing that must stay tested is the tree
that persists; what your batch leaves untested is the intermediate trees for the minutes
between presses."

**The six conditions, as ruled** (crossing 16285, verbatim except one elided internal reference,
marked `[...]`, and one bracketed note):

> (1) scope exactly as you measured: the five after #4053 with pairwise-disjoint file sets,
> plus #4134 stacked on #4136 pressed through gh stack merge in order [sic: #4134 is an issue,
> and that batch held no stacked PR]; any PR whose file set
> is not disjoint drops to serial. (2) Each PR keeps its own full gate (fairy record,
> non-author approval on the exact head, CI green on that head, role check). (3) ONE whole run
> on a scratch of current dev plus all heads merged in press order; the run id and the final
> tree sha go in each merge comment. (4) Press the five inside one announced window: post 'dev
> moving, N presses, until HH:MMZ' in the Office before the first press and ask no seat to run
> on dev during it ([...]); after each press assert dev's tree equals
> the cumulative scratch tree at that step, as you proposed; the window closes with a one-line
> read-back that the final dev tree equals the tested tree. (5) Any red in the batch run that
> plain dev lacks sends the implicated PRs back to serial, as you proposed. (6) If the final
> tree assertion fails at any step, stop pressing and run that landing tree alone.

**The standing form.** The same six conditions, read as follows:
- **Condition 1, for any batch rather than those five PRs:** the PRs' file sets are pairwise
  disjoint, a stacked PR is pressed through `gh stack merge` in order, and any PR that is not
  disjoint goes serial.
- **Condition 3:** each merge comment names the tested tree, the run's counts and its
  duration. No record on those two days carried anything called a run id.
- **Condition 5, as practised:** the implicated PR leaves the batch, and the others
  re-batch without it. The implicated PR may join a later batch once fixed. On 2026-10-25
  the red PR rejoined batch C after its fix, while the batch's other two PRs re-ran in
  batch B.
- **More than one seat may press one batch.** Each press still asserts its own step tree,
  and the seat that presses posts that PR's record. In batch D′ the merge seat pressed two
  PRs, and the commander pressed the third (the merge seat's own PR) as the other merge seat.
- **Condition 5's one exception is rule 4 of *Merge-seat test rules*.** A red that only the
  whole run produces and that meets rule 4's four conditions is a load flake. The seat
  applies those conditions itself, files the flake, records it and its controls in every
  merge record of the run, and presses; no separate ruling is needed. It is not a red that
  sends PRs back. This is
  how the 2026-10-25 batch D′ was pressed; it ran before rule 4's co-run condition existed.

Four additions, each from a slip or a decision on those two days:

- **An unapproved PR stays out of a batch.** Its red would hold PRs that are ready (decided on
  2026-10-25; Kepler concurred).
- **The merge record is posted on each PR before its press.** It names the batch, the run and
  the step tree. On those two days this held only from 2026-10-25's batch D′ on (7 of the 25
  PRs). The other 18 records were posted after their presses: by up to two minutes on
  2026-10-24, about 30 seconds for batch B, and up to about four minutes for batch C.
- **The window post is made mechanical, not remembered.** (a) The step that prepares the presses
  prints the window line, so posting it is a paste. (b) The press step refuses when no window
  post is recorded for the batch. Condition 4 is the only condition aimed at seats outside the
  pipeline, so nothing inside the pipeline notices when it is skipped.
- **The press step follows rule 5 of *Merge-seat test rules*.** It gates on CI and does not
  press on a printed pending state (the CI-green gate's `UNKNOWN` item, made a press rule).

**The record, two days, one seat (Borges).**

| day | batches pressed | PRs pressed | reds |
|---|---|---|---|
| 2026-10-24 | 3 (5, 3, 3 PRs) | 11 | none |
| 2026-10-25 | 5 (4, 3, 3, 2, 2 PRs) | 14 (13 by Borges, one by Kepler) | see below |

- **Predicted trees:** every one of the 24 presses Borges made landed on its predicted step tree.
  There were no divergences, so condition 6 never fired.
- **The 2026-10-25 reds:**
  - One further batch went red and stopped before any press. All its failures were in one PR's
    own defect, which only a whole-suite run shows; a serial whole run would have caught it equally.
  - One pressed batch carried one red, in a timing cell in a file no PR touched. Kepler ruled it
    a load flake after four controls, and it is filed as #4180.
- **Throughput:** 25 PRs took 10 started whole runs instead of 25: 8 led to presses, 1 went
  red, and 1 (batch D on 2026-10-25, ruled in crossing 17180) was stopped at about 55% and
  re-batched as D′ (crossing 17206), to avoid a base race with a newly ready stacked PR.
- **Misses:**
  - Condition 4's window post was made for the three 2026-10-24 batches (Office crossings
    16322 with read-back 16323, 16535 and 16572). No post is on record for the five 2026-10-25
    batches; addressed messages to the commander and the authors stood in for it, which is not
    the condition.
  - The merge record came after the press for 18 of the 25 PRs (see the additions above).
  - One batch press (#4182) went through on a printed `UNKNOWN` merge state. Its CI was read
    green only afterwards.
- **Not checked:** the trees between presses were not run, by design. Whether dev's own
  scheduled runs over those windows were green was not checked.
