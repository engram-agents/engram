# The retraction replacement contract — design (engram-alpha#2613)

**Status: DRAFT 4, for review.** Closes #2613 — the whole issue, not a half: the PR body carries the unconditional close keyword and `closingIssuesReferences` will fire on merge. #2013's own closure stays contingent on code and is not touched here. #2611 is the
measurement that motivates it. Depends on #2612, which landed as PR #2614 on
2026-09-06 and is what makes this design possible at all.

## 0. What this is, and what I read to write it

**The obligation this design creates, in one sentence:** every author-initiated
retraction must record a replacement **decision** — either the node that
replaces the retracted claim, or a stated reason why nothing does — and both
retract paths must record it the same way, through one piece of code, in a place
a later reader can scan.

**What it deliberately does not do:** it does not make `retracts` a guaranteed
edge, it does not mint a sentinel node, and it does not repair the retractions
already in the graph. §8 says why the third is a separate decision and names the
one consequence of deferring it that cannot itself be deferred.

**Read-scope, stated so a later reader can disagree with it rather than guess.**
I read `engram_revision.py` and `server.py` at `origin/dev` `ccf47041` under
three questions: *where can a replacement be named today and what refuses it*,
*what does the retraction event actually record*, and *which code paths reach a
`status='retracted'` write*. Draft 1 declared the #2281 prediction-basis branch
out of scope; §9 shows why that gap mattered, so it is now traced and in scope.
What remains untraced: the contradiction-reopening branch, and the taint-marker
writes that follow the retraction. No claim below depends on either.

---

## 1. The problem, in the shape that matters

Supersede cannot be called without a replacement, so its provenance link is
structurally unavoidable. Retract's is optional, so it is almost never there.
That asymmetry produces a graph that **cannot distinguish a retraction whose
author decided nothing replaces the claim from a retraction whose author never
considered the question.** One is a judgement; the other is an omission.

Making `retracts` mandatory is not available: a retracted claim was never valid,
and sometimes the honest outcome is that nothing takes its place. So the edge
must stay conditional on a replacement existing — and the **obligation** has to
attach to something always present. That is the retraction event.

## 2. The measurement, re-run today — and three corrections

Read-only, my own graph, 2026-09-11. One graph — mine. No other seat has been
checked, and #2611 carries the same single-seat bound.

| population | count | with provenance link | ratio |
|---|---|---|---|
| retracted nodes | 101 | 6 | **5.9%** |
| superseded-dead nodes | 746 | 746 | **100.0%** |

**Both denominators, stated as predicates rather than as names**, because §2 is
the section about instrument scope and a denominator named only in prose is the
half that goes unchecked: *retracted* is `status='retracted'`; *superseded-dead*
is `is_current = 0 AND COALESCE(status,'') != 'retracted'`. Note that
`status='superseded'` returns **0** — there is no such status value, so the
obvious guess at this denominator fails closed. "With provenance link" is
`EXISTS(edge WHERE relation=<rel> AND target_id = n.id)` in both arms. **The
graph is live and these counts drift during a single review** — the superseded
denominator was observed at 746 and at 747 twenty minutes apart. The ratio is the
claim; the counts are a timestamp.

**Correction 1 — #2611's "the gap is widening, not closing" is no longer true,
and the right number is not the one draft 1 used.** Draft 1 cited 5.9% against
#2611's 2.1% and called it "four points". That figure is **cumulative**, and 95
of its 101 denominators are pre-#2612 retractions, so it structurally cannot
speak to the post-fix regime. Since `retracts` is `addable_after_creation:
False` (`engram_core.py:767`), a link can only be minted at retraction time, so
edge creation dates partition cleanly: **four of the six retractions since
#2611's 2026-09-05 measurement carry a link — a forward rate of ≈ 4/6, against
2/95 before.** An order of magnitude, not four points.

**Its bound, which matters at n=6:** the author of those four retractions is the
author of #2612 and knew the parameter had changed, so **awareness is not
separable from parameter shape here.** The causal attribution to #2612 is
temporally consistent — all four post-date its merge — and I could not falsify
it; it is not isolated either.

**Correction 2 — optionality is still the dominant cause, and that is what
justifies this contract.** Making the parameter accept the right thing moved the
forward rate to roughly two-thirds against a 100% control. Nothing short of
making the decision *unskippable* closes the remainder. The contract stands; its
justification narrows from "the parameter was never used" to "the parameter is
still optional".

**Correction 3 — draft 1's claim that eight `supersedes` targets carry a second
edge is FALSE, and the true explanation is a defect.** Measured: zero targets
carry more than one `supersedes` edge, and zero `(source, target)` pairs
duplicate. The excess is not multiplicity — it is **eight nodes that are
`supersedes` targets while still live**, each with exactly one incoming edge, all
of type `evidence`, all `is_current=1, status='active'` (the eight are enumerated
in #2839). `engram_core.py:791-794` treats *"a current node
cannot carry an incoming supersedes edge"* as a **premise** of that relation's
`cascade: False` reasoning — so these rows are invariant-violating data, not a
counting artifact. Filed as #2839; §11 records it rather than this design owning it.

**And the instrument-scope lesson draft 1 drew from its own error was wrong
about the error, in a way Correction 3 above already refutes.** Draft 1 said the
wrong instrument was *"counting edges rather than distinct nodes"*. That is a
**no-op** here: there are 756 `supersedes` edges over 756 distinct targets, so
edges-over-distinct-targets is 100.0%, not 101.1%. The instrument that actually
produces 101.1% is a **numerator/denominator population mismatch** — 756 edges
over the 748-node superseded-dead population. Correction 3 states the
zero-multiplicity fact four paragraphs earlier and therefore proves draft 1's
description false; I wrote both.

**The conclusion draft 1 drew survives, and its real mechanism is more
interesting than the stated one.** The retract arm returns `6/101` under both
instruments, so the bug was detectable only on the supersede arm and the headline
figure this design rests on was never protected by the catch that found it. But
the retract arm's immunity is **contingent, not arithmetic**: it holds on **two** conditions, both facts about
today's graph that one bad write would end: **zero `retracts` edges point at a
non-retracted node**, and **no retracted node carries more than one** (6 edges
over 6 distinct targets) — not, as draft 1
implied, because six and six are necessarily the same number. A self-catch fires
where the bug produces an impossible value and is silent where it produces a
plausible one, and here the silence rests on a contingency nobody is guarding.

## 3. Where the decision lives — one chokepoint, three changes

#2613 requires both paths to record the decision identically. The design meets
that by **not having two paths record anything**:

- **Organic retract** — `engram_retract` → `_retract_impl`.
- **Resolution retract** — `engram_cascade_resolve` with
  `resolution_kind='retracted'`, whose `else: # retracted` branch delegates to
  `_retract_impl` **unconditionally** (`engram_revision.py:1453-1459`).

Both `UPDATE nodes SET status='retracted'` sites live inside `_retract_impl`, so
enforcing the contract there gives both paths the obligation by construction,
with no second site to drift.

**But a chokepoint enforces VALIDATION, not ACQUISITION — and this repo already
contains the precedent for how the acquisition half fails silently.**
`_cascade_resolve_impl` is declared with `error_type: str = "other"`
(`engram_revision.py:1246-1248`) — a field `_retract_impl` *requires*, already
satisfied on the resolution path by **a default the caller never sees**. Thread
the decision the same way and the outcome is exact: the refusal is satisfied, the
invariant scans green, both paths "record a decision", and on one path nobody
decided anything. That is the state §6 exists to eliminate, restored by plumbing
rather than by a flag. *(Bound, corrected: **one of the 101 historical retract
events came through the cascade path** — a derivation resolved as `retracted`
through `engram_cascade_resolve` on 2026-09-05, and it carries
`error_type="other"`, the very default this paragraph warns about. So the
inflation is already in the data, n=1. An earlier draft said zero, measured by
matching a `cascade resolution of …` reason prefix, which is **blind by
construction**: the impl sets `reason = note or "cascade resolution of …"`, so
the prefix appears only when no note was supplied. The sound instrument is
`cascade_markers.resolution_kind='retracted'`. **That single event is the live case #2611 cites as what prompted it** — the resolution where the honest verb was retract, the successor already existed, and no parameter accepted it. **The rate, with its population stated — because the obvious one is 93% the wrong thing.** `cascade_markers` holds 258 resolutions, but **241 of them are rung-5 backfill** (`backfill_provenance='recovered'`), resolved in bulk sweeps rather than by an author facing a choice. The population this argument is about is the **17 live-cascade resolutions: 14 `acknowledged`, 2 `superseded`, 1 `retracted`.** So the contrast is **2:1**, not 23:1.)*

*(The guard **penalises** choosing `retracted`, since `superseded` is the only resolution that can carry a replacement today. So the distribution is biased — but the bias is **signed**, and that buys a bound rather than an excuse: every author who wanted to name a successor had to choose `superseded` to do it, so **1 is a lower bound on the demand for retract-with-replacement on the cascade path, not an unmeasurable.** The upper bound is 3, if both `superseded` resolutions were retractions in honest terms. **Both numbers are scoped to the cascade path**, and the scope is load-bearing rather than decorative: §8 counts **six** historical `retracts` pairs on the *direct* retract path, where a replacement is already expressible, so a reader who lifts "the upper bound is 3" out of this paragraph meets a figure this document's own §8 contradicts. **And 3 bounds the demand VISIBLE IN THE RESOLUTION RECORD, not the demand** — it assumes none of the 14 `acknowledged` resolutions carried retract-with-replacement demand, which is nearly definitional and is in tension with §6's own thesis that a penalised path produces authors who satisfy the letter and decide nothing. That range is small and it is the honest statement; "a penalty-shaped distribution cannot measure demand" declined a bound that was available. And change (2) below does not
OPEN that path — the path is already open; change (2) widens what rides on it.)*

**Three changes, not two:**

1. **`_retract_impl` gains the decision parameter and refuses without it.**
2. **The cascade-resolution guard stops discarding the replacement.** Today it
   refuses `replacement_id` whenever `resolution_kind != 'superseded'`, so a
   retraction reached through the frontier cannot name a successor *even when one
   demonstrably exists* — the normal case in a sweep, because you write the
   corrected node first and resolve second. The guard becomes: a replacement is
   *required* for `superseded`, *permitted* for `retracted`, refused otherwise.
   The decision parameter is threaded to `_cascade_resolve_impl` **with no
   default**, and added to `_CASCADE_RESOLVE_FIELDS` (`server.py:2666-2668`).
   The `error_type="other"` default is the anti-pattern not to copy.
3. **The retraction event carries the decision.** Today `_log_edit` writes
   `{"error_type": …, "reason": reason[:200]}` (`engram_revision.py:3210`) and
   **`replacement_id` is not in that row even when one was supplied** — the edge
   is written sixteen lines later, on a different surface. Without this change
   §9's *"checkable against the event log"* is simply false, and no amount of
   parameter-refusing makes it true.

**Change 3 is also what satisfies §5's propagation requirement**, which is why it
is load-bearing twice over rather than a tidiness fix.

All three ship in one PR. Fixing the guard without the contract re-creates
optionality on a second path; adding the contract without the guard makes the
resolution path unable to satisfy it; doing either without the event payload
leaves the invariant unscannable.

## 4. The reason vocabulary

**This section reverses draft 1, which itself silently reversed a position I had
posted on #2613 the same day. Stating that plainly, because a reader holding both
documents was getting two contradictory recommendations from one author with no
marker saying which was current.**

Draft 1 argued the vocabulary *could not be derived up front* — the field that
would have recorded the reasons is the field being added — and therefore proposed
shipping the shape with a mandatory free-text note and deriving the set later. My
#2613 comment had already argued the opposite and supplied a list. **The comment
is right and draft 1's argument overshoots**, for a reason visible inside draft 1:
its best point is the *escape-hatch asymmetry*, and that point is independent of
which members the set has. It does not need the list deferred; it needs a list to
attach to.

What is true is that the members cannot be founded on **frequency data**. They
can be founded on **distinct epistemic situations**, which is what the list does.

| member | the situation |
|---|---|
| `NONE_BECAUSE_NO_SUCCESSOR` | the claim was simply wrong; nothing true stands in its place |
| `NONE_BECAUSE_DUPLICATE` | the surviving node already existed — pairs with `error_type='duplicate'`; see the distribution below |
| `NONE_BECAUSE_SUPERSEDED_ELSEWHERE` | a successor exists but is not this retraction's to name |
| `NONE_BECAUSE_UNKNOWN` | no successor could be determined |

`NONE_BECAUSE_DUPLICATE` is **derived, not guessed**: in the duplicate case the
survivor is *older* than the retracted node, so it can never carry a `retracts`
edge — the same `temporal_causal: True` constraint that killed the sentinel
proposal. The member exists because the graph shape forces it.

**Closed set, with no free-text sibling.** The stated purpose is a scan —
*"which retractions had no replacement, and why?"* — and free text does not scan.
A closed-set-plus-note splits the answer across two fields and the note quietly
becomes the real content.

**The `error_type` distribution, measured here rather than cited to a measurement
this document does not contain.** An earlier draft wrote *"the `error_type` precedent
measured above"* and there was no such measurement above — the two percentages were real
and their population was nowhere stated, which is the defect §3 spent four rounds removing
from the cascade rate and which recurred here because the fix was applied to the instance
and not to the class.

**Population: all 101 `action='retracted'` rows in `edit_history` on my graph.** That is
also exactly the 101 nodes at `status='retracted'` — one event each, no node retracted
twice — so the event count and the node count coincide and the denominator is the same 101
§8 uses.

| `error_type` | all 101 events | authored-live (69) | backfilled (32) |
|---|---|---|---|
| `other` | 26 — **25.7%** | 24 — **34.8%** | 2 |
| `wrong_evidence` | 22 — 21.8% | 3 — 4.3% | 19 |
| `duplicate` | 22 — **21.8%** | 20 — **29.0%** | 2 |
| `hallucinated_claim` | 20 — 19.8% | 19 — 27.5% | 1 |
| `wrong_citation` | 11 — 10.9% | 3 — 4.3% | 8 |

⚠ **Read the split, not the total, and the reason is the same one §2 gives.** 32 of the 101
carry `backfilled: true` — resolved in bulk rather than by an author facing a choice — and
they are not distributed like the rest: they are 19 of the 22 `wrong_evidence` and 8 of the
11 `wrong_citation`. On the 69 an author actually chose, `other` is **34.8%** and
`duplicate` **29.0%**, both higher than the pooled figure.

**Which arm each argument wants, stated rather than left to the reader — and "the pooled
figure is conservative" is NOT the right description, so it is not used here.** Pooled is not
a lower bound on anything; it is a **mixture** of two populations with different generating
processes, and it came out lower only because backfill happens to carry little `other`. Had
backfill been `other`-heavy, the same mixture would have read high. **Both arguments in this
section want the AUTHORED-LIVE arm**, and neither wants a figure outside the two — the
escape-hatch-decay argument is about what authors facing a real choice do, and
`NONE_BECAUSE_DUPLICATE` is **CORROBORATED** by the same arm. Not *justified* by it: the
member's justification is structural, the `temporal_causal: True` argument below, and this
document says two paragraphs up that the members cannot be founded on frequency data. A
frequency that corroborates a structural member is evidence; a frequency that founds one
would contradict the section it sits in. The pooled column is in the table so the mixture is
visible, not because any argument rests on it.

**One conditional note, on the escape hatch only.** `NONE_BECAUSE_UNKNOWN` is the
member that makes the gate free to satisfy, which is how a required field decays
into a default — and `error_type` is the precedent, `other` being its **modal** value at
34.8% among the ones an author chose (25.7% pooled). So `_UNKNOWN` alone requires a free-text
note. Naming
the escape hatch costs a sentence; naming a real category costs nothing. That is
the right way round, and it is the inverse of `error_type`, where `other` is the
cheapest value on offer.

⚠ **`other` may carry a SECOND contamination the `backfilled` split does not isolate, and it
is this document's own §3 subject arriving inside §4's measurement.** On the cascade path
`error_type` CAN be machine-supplied: `_cascade_resolve_impl` defaults it to `"other"`
(`engram_revision.py:1248`) and its retracted branch passes `error_type or "other"` into
`_retract_impl` (`:1456`), which then satisfies its own required-field gate (`:2692-2693`)
with a value the author may never have typed. A **fourth** vector exists on the #2281
prediction-basis branch (`engram_revision.py:3104`) and is measured closed: **zero** of the 101
events carry a `cascade_via` key, so it contributes nothing and the ceiling stays at 1.

**The bound is `≤ 1` of the 101, possibly 0 — NOT the "exactly 1" an earlier draft claimed,
and why the weaker form is the honest one is worth stating.** `error_type` is a declared,
caller-settable parameter on the cascade-resolve verb (`server.py:2667`, documented at
`:2753-2757` as *"optional … Defaults to 'other'"*), so the default fires only when the
caller omits it — and `edit_history` records **no provenance**: the one candidate row — the
edit event for the single `retracted` cascade resolution §3 names — stores
`{"error_type": "other", "reason": …}`, shape-identical to an author-chosen one. Nothing in
the record distinguishes a defaulted value from a typed one, so the number of
machine-supplied values is unmeasurable from here and 1 is its ceiling.

**The bridging fact, stated because the bound is over the 101 and the arithmetic below is over
the 69.** That candidate row is **authored-live, not backfill** — its `details` carries no
`backfilled` key — so it sits inside the 69 and removing it is arithmetic on that arm rather
than on the pooled one. A colleague should not have to take that on trust to follow the step.

**What that does to the figures, with both conventions printed because the verb and the
denominator have to agree.** Discount the event while keeping it in the population: `other`
is **23 of 69 = 33.3%**. Excise it from the population entirely: **23 of 68 = 33.8%**. Either
way `other` stays modal against `duplicate` — **20 of 69 = 29.0%** under the first convention,
**20 of 68 = 29.4%** under the second — so the argument holds on the worst case and does not
turn on which a reader prefers. (Both are printed because the sentence above says the verb and
the denominator have to agree, and a single figure here would break that rule one line after
stating it.) The vector is recorded
because it is a second way a required field can fill itself — precisely what
`NONE_BECAUSE_UNKNOWN` is being guarded against.

**Where the note lives, and its hazard.** `reason` is truncated at `[:200]`
(`engram_revision.py:3211`). The `_UNKNOWN` note must **not** share that row, or
the instrument is silently clipped exactly where the design depends on it.

**The provisional marker is a tripwire, not a disclaimer.** The set ships as
provisional with a measurable falsifier — **and the population is named, because this
document supplies three non-equivalent candidates and a bare "the distribution" picks none of
them.** The falsifier: **if `NONE_BECAUSE_UNKNOWN` exceeds 50% of the retractions AUTHORED
after this ships that supply a no-replacement reason at all, measured a month out, the gate is
decorative.** Not the 101 historical events (none of which can carry the field), not the 69
authored-live among them, and not all retractions authored after the ship date (a retraction
that names a replacement never reaches the closed set). We learn it from the scan rather than
from intuition.

## 5. The replacement must be reachable from what it repairs

Lei's added requirement on #2613: a replacement must **propagate to descendants**.
Recording it on the event is necessary and not sufficient — a node downstream of
the retracted claim has to be able to *find* the successor.

Two structural gaps make the obvious route unavailable:

1. **The cascade ROOT carries no `cascade_markers` row.** Markers are written for
   affected nodes, not for the node whose retraction created the cascade — so
   `resolution_replacement_id`, which lives on a marker, structurally cannot
   carry the root's replacement.
2. **`_cascade_frontier_node_summary`'s `dead_premises` therefore cannot name
   it**, for the same reason: it reads markers.

**The design's answer falls straight out of event-not-edge:** the root's decision
lives on the **retraction event**, so the frontier summary reads it from there
rather than from a marker row that by construction does not exist. This requires
§3's change 3 and nothing further — the decision must be *in* the event row for
anything to read it. Two requirements, one mechanism.

## 6. Required-always, and no boolean opt-out

**Required-always.** `replacement_absent_because` **is** the opt-out; a second
one is not needed and is actively harmful.

#2611's option (2) proposed mandatory-with-`no_replacement=True`. Not adopted: a
boolean carries **no reason**, so it restores exactly the structurally-silent
state this design exists to remove — satisfying the letter of the invariant while
reinstating the defect. Two ways to say nothing, one of them cheaper, and the
cheaper one wins under load.

The discriminator for any future convenience flag: **a valid opt-out records a
judgement; an invalid one records only that the author wanted to move on.**

## 7. What a valid replacement is

#2613's scope, added on the issue by me, because *what makes a valid replacement*
is precisely the contract question this issue exists to settle.

**`replacement_id` is not checked for being claim-bearing.** The gap became
reachable when #2612 retired mint-inside-retraction: the old minting path always
produced an observation, so the question could not arise. Measured on the issue: a
`task` node was accepted as the replacement for an observation — no error, edge
wired.

**The contract is a FLOOR. The narrower question is deliberately left open in §10.**

**The floor:** `_is_claim_bearing(replacement)` must **equal** `_is_claim_bearing(target)` — parity, not an unconditional predicate. That rejects the `task`→`observation` case which made this reachable, and permits
`conjecture`→`derivation`.

**What the floor does to the historical record, stated as two different populations because
an earlier draft ran them together.** 10 of the 101 retracted nodes are non-claim-bearing
(3 questions, 2 evidence, 2 lessons, 1 cornerstone, 1 goal, 1 person) — and parity acts on
a *pair*, so that count is not a population parity evaluates at all. Measured: **none of
those 10 is an endpoint of any `retracts` edge**, so the floor is vacuous on every one of
them. What it does reach is the 6 existing `retracts` pairs, and **all six are
claim-bearing on both ends** — five `observation_factual`→`observation_factual` and one
`derivation`→`derivation` — so parity refuses none of them. That is the measured version of
the claim this section makes further down, in the paragraph beginning *"This is a THIRD rule"*
— named rather than counted, since round 6 removed one counting pointer from this document and
round 7 replaced it with another that was off by three. **Residual gap, named rather than closed:** it does not constrain a non-claim-bearing *pair*, so a retracted question could name a lesson as its replacement and the floor would not object.

**Why the floor must not be narrowed toward same-kind — and the *don't-narrow* half of this is a fact about the substrate, not a preference (the parity floor itself is not — see the paragraph beginning *"That qualifier is load-bearing"* below).** `_supersede_impl` already refuses a type mismatch, and its error text designates the alternative by name:

> *"Supersede preserves type — superseding a conjecture with a derivation (or similar cross-type revision) is not supersede. If the correction changes the epistemic category, use `engram_retract` on the old node and cite the replacement organically in new derivations."*

So **retract is the path the codebase assigns to cross-epistemic-category correction.** **One sentence the argument needs, or it proves too much:** `_supersede_impl` refuses *every* type mismatch and routes all of them here, so taken literally this would rule out parity as well. It does not, because the defect that motivates §7 is a **partition crossing** — a non-claim-bearing node standing as the replacement for a claim-bearing one — and that is where the argument stops. A `conjecture`→`derivation` correction is a cross-type revision retract should carry; a `task`→`observation` one is a category error **for `retracts` specifically**.

**That qualifier is load-bearing and the substrate does not supply it.** The partition itself is substrate-recognised — canonical predicate, ratified spec, CI-bound membership. But *crossing* it is not categorically forbidden: `predicted_by` is a deliberate cross-partition edge, running `prediction` (non-claim-bearing) → `observation_predictive` (claim-bearing). And parity as a *shape* has no precedent either. Of the **thirteen** `_is_claim_bearing` call sites under `src/engram/` (the definition at `engram_core.py:503` excluded, as are two prose mentions of the name), the four in `engram_query.py` classify or filter rather than gate, and each of the other nine tests **one** node's type against a fixed value. Two of the nine sit inside a loop over many nodes, and `engram_revision.py:1639` is a third site the same gloss reaches — a single-node test on a per-call basis, applied repeatedly — so read *"applied N times rather than any comparison between nodes"* as the property, not *two* as its census. The nearest thing to a two-endpoint rule is `contradicts`, which constrains *both* of its endpoints (`engram_derive.py:426` and `:432`) — and does it as two independent one-sided tests, each against the same fixed value, never as a comparison between the two. So this floor is a claim about what the `retracts` relation in particular can hold, argued from the relation's meaning, and it is not the substrate speaking. Stated plainly rather than borrowed authority it does not have. A same-kind requirement on retract's replacement would close the one route the substrate names for exactly that case, and would do it in the document whose subject is making retract's replacement expressible.

*(**This is a THIRD rule, not a restoration of my posted one, and draft 4 first said "restored" — which was false.** My #2613 position was *"claim-bearing at minimum"*: an **unconditional** predicate on the replacement, which is what draft 2 shipped. Parity disagrees with it **in both directions** — a `lesson` replacing a `lesson` is refused by the posted rule and permitted by parity; a `derivation` replacing a `question` is permitted by the posted rule and refused by parity. So this document has now held three positions: unconditional, compatible-kind, and parity. Parity is the best of them and is adopted on its merits — it is the canonical predicate, it rejects the `task`→`observation` case, and it refuses none of the six historical replacement pairs. What it is not is a return to anything.)*

## 8. Backfill is a separate decision — and the one part that is not

#2613 says explicitly: *do not fold it in.* Honoured. This design is
**forward-only** and proposes nothing about the 95 existing unlinked retractions
(101 − 6). `retracts` is `addable_after_creation: False`, so they cannot be
repaired through the normal path regardless.

**One consequence of deferring cannot itself be deferred, because it determines
whether the invariant is checkable at all.** A conformance check asserting *"every
retraction records a replacement decision"* over the whole population starts at 95
violations that **no correct action can ever clear** — the field did not exist when
those events were written.

That a diagnostic must not be immune to its own remedy is Lei's own stated reason
for the CURRENT-ONLY conformance ruling (`engram_conformance.py:37-46`): *"if
non-current state stayed in the violation counts, the fix could never move the
diagnostic."* **The mechanism here is not identical, and the difference is worth
stating so nobody generalises the wrong rule from it:** that ruling excludes
non-current nodes because superseding or retracting them *is* the remedy, so the
excluded set **shrinks** as remediation happens. §8 excludes pre-migration
retractions because there **is no** remedy, so the excluded set is **frozen**.
Alignment-with-a-remedy versus absence-of-one. Same conclusion; do not read it as
"exclude whatever cannot be fixed".

**So the invariant is scoped to retraction events written after the migration.**
Pre-migration retractions are *out of population*, not *in violation*. Whether
they are later repaired is the separate decision #2613 reserves.

## 9. The invariant

> **Every author-initiated retraction event records a replacement decision.**

Checkable against the event log, which §3's change 3 makes true.

**"Author-initiated" is load-bearing, not hedging.** `_retract_impl` retracts a
**second** node on one branch: per #2281, when the retracted node is an
`observation_predictive`, a prediction whose sole live evidential basis it was is
*terminally* retracted (`engram_revision.py:3101`), and that write emits **the
same `action='retracted'` event row** (`:3104`) for a node the author never named.
No author can supply a decision for it, so an unqualified invariant would be
unsatisfiable on that path — the diagnostic-immune-to-its-own-remedy shape again,
arriving through the front door instead of from history.

**Machine-written retraction events are out of population**, and inherit the root
retraction's decision for display. *(Currently n=0 in this graph: zero
`edit_history` rows carry a prediction-basis marker and zero nodes carry
`retracted_via_basis_retraction`. This is a design-completeness gap, not a data
problem.)*

Note what the invariant deliberately is **not**: *"every retraction has a
`retracts` edge"* — shape-deep, and unsatisfiable whenever no replacement exists.
The graph shape stays honest: **an edge exists exactly when a replacement
exists.**

## 10. Open, and whose call

| question | mine to propose | reviewer's | Lei's |
|---|---|---|---|
| the four members of §4 — provisional, with the >50% `_UNKNOWN` tripwire as falsifier | ✓ | ✓ | — |
| making the decision required at all: a **substrate-contract change**, per #2611's own read | — | — | ✓ |
| backfill of the 95 (§8) — deliberately out of scope here | — | — | ✓ |
| **whether the §7 floor should be narrowed below claim-bearing parity** — must a retracted derivation be replaced by a derivation? My posted instinct is no, since the honest correction of a bad derivation is sometimes an observation, and `_supersede_impl` routes cross-category correction here by name | ✓ | ✓ | — |
| the §7 residual gap: nothing constrains a non-claim-bearing PAIR | ✓ | ✓ | — |
| whether machine-written events should inherit the root's decision or be marked absent (§9) | ✓ | ✓ | — |

## 11. What is NOT done

- No backfill design (§8, reserved by #2613).
- The eight invariant-violating `supersedes` targets of §2 are filed as #2839;
  found here, not this design's to fix.
- `engram_revision.py:3226-3232` swallows `sqlite3.IntegrityError` on the
  `retracts` edge INSERT with a bare `pass`, so a retraction can commit while
  reporting a replacement whose edge does not exist. Pre-existing (#2614); no
  case has been constructed that actually trips it.
- Nobody has checked whether a second seat's graph shows the same asymmetry; this
  design and #2611 both rest on one graph.
