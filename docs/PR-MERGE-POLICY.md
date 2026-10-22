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
Lei is away"), the security clause (Mira's approval is still the gate there),
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

## Scope — the substrate gate is **engram-alpha only**

The substrate-feature gate below is **engram-alpha-specific**. Other repos in the org carry their own **delegated** merge authority, set by Lei per project:

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

- **Default repo lanes**: Borges holds engram-alpha's queue, Kepler holds
  umwelt's. Crossing into the other's lane is fine and takes **one line saying
  so first** — never a silent concurrent merge into the same tip. *(Amended
  2026-10-22 by *Owner merge* below: for project owners the lanes are suspended
  and the one-line rule applies to every merge into a shared tip until the dev
  merge queue is on.)*
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
  review scope.

So "flip to Lei for substrate" is an **engram-alpha** rule. Do not extend it to a delegated-authority repo; do not relax it inside engram-alpha beyond what the 2026-09-27 delegation above grants. When you're unsure which repo's rule applies, the repo you're merging *into* decides.

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
queue switches on separately (see *Serialization* below). The stance he asked the team
to carry is the one in the quote: practise, catch the slips, learn from them.

**Who.** The **owner of the project the PR belongs to** (the Project Register in the
Workshop, an in-world surface, not a file in this repo) merges it. Not the colleague
who approved it, and not a seat that merely holds the baton. **Scope:** owner merge
applies on every org repo whose merge authority is the team's (engram-alpha under this
policy, umwelt, local-model-lab, gpu-broker and the rest under the 2026-08-22 grant);
soma stays Ariadne's as principal approver and merger (*Scope* above). Three groups go
to the **merge seats** instead (Kepler and Borges on all org repos, with Ariadne on
umwelt and soma, per *Scope* and *Who presses the button* above):

- a PR with **no project owner**, a **release cut** (a `dev`→`main` release PR or a
  hotfix PR into `main`), and a **reconcile** PR;
- a PR whose **author is its project owner**: the owner runs the gate below and posts
  the record, and a merge seat presses the button on that record, so no seat merges its
  own PR;
- a seat whose **own user's authority table does not grant merge clicks** (as of
  2026-10-22, Aleph and Clio, whose user has not granted it, so a merge click is a
  destructive action outside this repo's delegation for them): that seat runs the
  gate and posts the record; a merge seat presses.

**Nothing below widens the ⛔ list or touches the security read.** An owner never
merges a ⛔-list (substrate-concept) PR: it goes to Lei first, or, when it implements a
design he already read, through the 2026-09-27 delegation's two steps (the PR links the
read design; the merging seat writes the conformance check), and those two steps stay
with the merge seats. A security PR carries Mira's exact-head approval as its gate and
is merged by the merge seats under the two directions in *Who presses the button*; an
owner does not click it. The owner merges exactly what the merge seats could already
merge on a fix or non-substrate PR, by exactly the same gate.

**The gate the owner runs, in full, before pressing the button.** The merge seat's
gate did not get easier because more seats hold it; it got written down so that any
seat can run it and any other seat can check that it ran.

1. **A reviewer-fairy round is on the PR's own record, and it preceded the peer
   pass** (the order is a rule, not a diagram — CLAUDE.md § PR review convention).
2. **A non-author peer APPROVE whose commit is the PR's current `headRefOid`, and
   whose `submitted_at` is not earlier than the head commit's committer date.**
   Compare the oid and the two timestamps; never trust `reviewDecision` alone. The
   second check exists because an `update-branch` can re-point an older approval at a
   newer head (see *Merge-seat test rules*, last subsection). Any later commit needs a
   fresh approval.
3. **`pr-checks` green on the EXACT head** (Luria, Aleph): mandatory, because a
   scratch merge on a Mac cannot see what the Linux runner's SQLite does. Read
   `mergeStateStatus` first; `CLEAN` ends it; anything else goes through the
   deduped rollup and the cancelled-same-SHA rule in *Merge-seat test rules* below.
4. **The seat's own run, on a scratch merge of the base tip plus the head** (rules 1
   and 2 of *Merge-seat test rules*), with the repo's **guard set** — and the guard set
   now **names the whole-tree scanners that no changed file names** (Luria):
   `tools/scan-leaks.py`, `tools/test_id_set_check.py`, and the whole-tree
   guards (`tests/test_syspath_shadow_guard.py`, `tests/test_tiers_manifest.py`,
   `tests/test_run_touched_tests.py`, `tests/test_ci_wiring_guard.py`,
   `tests/test_engramd_read_scan.py`'s real-tree cells). Verify the list with
   `git ls-files` before pasting it; a guard that moved is not a guard that ran.
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
   keeps its meaning because the gate reads GitHub, not the caller.
8. **The merge command pins the head, and the seat pins the base:**
   `--match-head-commit <full 40-char headRefOid>` read from
   `gh pr view --json headRefOid` in the same step, and **in that same step** a
   re-fetch and an assertion that `origin/<base>` still equals the base SHA item 4
   recorded. If the base moved, re-run item 4's scratch merge on the new tip before
   pressing: `--match-head-commit` pins only the head, never the base, so without
   this check two owners can each pass a scratch run on the same base and the second
   presses on a tree it never tested (the 2026-09-13 jointly-broken case, Borges).
   Every PR into `main`
   (release cut, hotfix, reconcile) and a `main`→`dev` reconcile merge as a **merge
   commit** (`docs/RELEASING.md`); every other PR into `dev` squashes; a member of a
   linked stack merges with `gh stack merge` (plain `gh pr merge` is refused there).
9. **On umwelt, merge does not promote** (Ariadne): prod promotion stays a separate,
   tested-first step by the deploy seat.
10. **The merge record, posted on the PR as the merge comment, names:** the merge
    SHA, this gate's items 1–3 with times and oids, **the base SHA the scratch run
    used and that it still held at the press** (items 4 and 8), **which suite ran and
    on which platform** (Aleph), the counts read from the saved output, the mutation and its
    red set, any fairy-cap note, and the read-back of every closed issue (the
    post-merge completeness read of CLAUDE.md § PR — issue closure). The commander
    samples merge records at day-end; a record missing an item is the finding,
    whatever the merge's outcome.

**Serialization — the property two holders kept by hand, now kept by the queue.**
On engram-alpha the **dev merge queue** (`merge_group` runs of `pr-checks`) replaces
the "one holder per repo lane" rule: every merge waits its turn and re-runs the
checks on the queued tree, which closes the jointly-broken case (two owners each
passing a scratch merge that omits the other's PR; Borges, 2026-09-13). **In the
interim, and on repos without a queue, the default-lane rule of *Who presses the
button* is suspended for owners** and replaced by two things. The correctness
mechanism is the **base-SHA assertion of items 4 and 8**: a press on a base the
seat's own run never saw is refused by the seat's own check, and the re-run on the
moved tip is what the queue will later do for everyone. The courtesy is **one line in
the Square before merging into a tip another owner may be merging into**, so a
moved-tip re-run is expected rather than a surprise. *Whoever starts a multi-PR stack
finishes it* (unchanged). The assertion also covers repos that will never get a
queue (local-model-lab, gpu-broker). The queue switches on after
the v0.5.4-rc1 cut (done 2026-10-22 13:08Z) and after the `merge_group` workflow PR
lands; the switch itself is a repo setting Lei or the repo admin flips. **The
commander records the switch date on the next line when it happens.**
*Queue status: OFF as of 2026-10-22.*

**What the merge seats still do.** Kepler and Borges (Ariadne on umwelt) keep the
seat for ownerless PRs, release cuts, reconciles, the three groups under *Who* above,
⛔-list and security PRs, and any merge an owner declines ("I'd rather a second pair
of eyes" is always allowed and never a demerit); the commander keeps the day-end
sample of merge records. The seat is no longer the serial point; the written gate is.

*Origin: Lei's 2026-10-21 23:31Z proposal and 23:53Z–00:02Z rulings, Meeting 18's
nine additions (2026-10-22 00:23Z, Square), Lei's 2026-10-22 09:59 AM NY question
and 10:03 AM NY ruling, both in session with Kepler. Drafted by Kepler 2026-10-22;
reviewer-fairy round 1 folded the same day.*

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
    PRs" above: Mira's approval is its merge gate, and it goes to Lei only if it also
    changes a ⛔-listed concept outside her review scope.
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

The "flip to Lei" half applies **only to substrate PRs** (in engram-alpha). For a fix / non-substrate PR, the maintainer (pool sentinel) — **not Lei** — is the merge-ready turn-target; the agent team merges it directly after colleague-convergence + CI-green. Setting `turn: lei` on a non-substrate baton is the anti-pattern: it produces a stuck baton that no one can advance, because Lei isn't gating that class (origin: PR #1333, a forum brand rename, whose baton was parked at Lei and stuck after merge — #1442). Match the turn-target to the class: substrate whose design Lei has not read → Lei; everything else, including substrate implementing a design he read → maintainer.

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
   most recently **completed**: `_dedupe_checks_by_latest()` compares `completedAt`,
   and ordering by `startedAt` instead picks a different winner whenever one run starts
   earlier but finishes later. `gh run list --branch <branch> --json
   name,headSha,conclusion,updatedAt` in time order is the direct way to see which run
   is the current one — that command exposes no `completedAt`, but on a finished run
   its `updatedAt` is ≈ the completion time (measured ~1s later, on five runs).
   This dedupe is the test verdict, but see rule 3 of "Merge-seat test rules" below: a
   cancelled newest same-SHA run still blocks the merge; rerun it.
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

Three rules for the seat that runs tests before merging. They sit with the CI-green
gate above because they answer the same question from the other side: CI-green says
the PR's checks passed; these say what the seat's own run must be and how to read it.

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
and the merge is refused — when the newest run of that check on the head SHA was
`CANCELLED`, even though another run on the same SHA passed. `gh run rerun <the
cancelled run id>` cleared it. Never argue from the deduplication above ("another run
passed, so the gate is satisfied"); rerun the cancelled run.

The cause seen, on alpha #3489 only: two `pull_request` runs on the same SHA were
created in the same second, after a push and a PR-body edit seconds apart, and
concurrency cancelled one. On #3476 we know only that the newest same-SHA run was
cancelled by concurrency; what triggered the second run is not known. The
explanation that fits is that GitHub keys the required check on the newest same-SHA
run, not the most recently completed one. That is the merge seat's reading from two
measurements, no documentation; the fact and the remedy above are as measured.
Untested advice: edit the PR body before pushing, or wait for the push's run to start.

*Evidence:* two measurements, 2026-10-06.
(1) alpha #3476, ~16:43Z: the newest same-SHA run was cancelled by concurrency, the PR
was `BLOCKED` and the merge refused ("base branch policy prohibits"), even though an
older run was green and a valid approval existed; `gh run rerun` of the cancelled run
gave `CLEAN` as soon as it completed.
(2) alpha #3489, ~17:13–17:20Z: read `BLOCKED | pr-checks:CANCELLED, pr-checks:SUCCESS`
(two runs on the same SHA, started in the same second by a push and a body edit);
`gh run rerun` of the cancelled run gave `CLEAN | SUCCESS, SUCCESS`.

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

**Approvals after `update-branch`.** CLAUDE.md § "PR review convention (multi-agent
mode)", paragraph "The merge gate itself", says a new commit after approval needs a fresh
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
