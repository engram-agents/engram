# Spec: #2252 W0 — the claim-bearing / non-claim-bearing type partition

**Author:** Luria Hebb · **Date:** 2026-08-26, re-verified against `dev` at `f676721`. This is
the **design of record** (#2252's W0 workstream) — it ratifies what was settled with @LeiShi
in session (2026-08-25 evening) and refined through Stage 1.0/1.0a/1.0b (Borges, Kepler,
Ariadne), reconciled against the code as merged through this doc's latest revision (`4431e89`
#2255, `3d5cc6e` #2262, `4ff3fcb` #2260, `9f411f5` #2261, `76a4de1` #2266, `7c1ba57` #2263).
**Nothing this doc describes is new design** — every claim below is either a direct read of
`EDGE_CLASSIFICATIONS`/`CLAIM_BEARING_TYPES`/`VALID_NODE_TYPES` on the commit named above, or a
settled answer already recorded in issue #2252. Where the issue's own early draft text
disagrees with what actually shipped, this doc follows the shipped code and says so explicitly
— a stale draft line is not the design of record; the merged declaration is.

**This doc's own revision history is worth naming, not hiding, since it's an instance of the
exact discipline it argues for.** Its first round (this doc's original commit) went stale
within about an hour — a concurrent merge (`7c1ba57` #2263, landed after this doc's tip was
authored) removed `lesson` from `CLAIM_BEARING_TYPES`, and this doc's membership section had
said otherwise. Caught by colleague review (Borges), not by this doc's own author noticing.
Fixed in this revision, along with a second real gap the same review found: `trust_signal`
(below) was missing from the NCB enumeration entirely, inherited from a pre-existing gap in
`VALID_NODE_TYPES` itself. A ratification document is only as current as its last
re-verification against source — this one states its source commit explicitly (above) so a
future reader can tell whether it might have drifted again, rather than trusting "current" to
stay true on its own.

**What this doc is for:** Stage 1.3 ("enforce the partition for future nodes") is checked
against this doc, not re-derived from the issue thread. If 1.3's implementation and this doc
disagree, this doc is wrong and gets fixed — not silently reconciled in code.

## The partition — Lei's definition

> **Claim-bearing (CB)** nodes assert something true-or-false. They carry confidence, may
> serve as premises, and participate in the cascade.
> **Non-claim-bearing (NCB)** nodes provide disposition and relational context. They carry
> **no confidence**, are **never premises**, and are **not part of the cascade**.

The reasoning for the cascade half (Lei, via #2252): cascade exists because *a claim changed
and every downstream claim built on it must be re-adjudicated*. An NCB node has no claim to
re-adjudicate, so cascade reaching it accomplishes nothing — no confidence to recompute, no
verdict to revise. NCB nodes have exactly one concern, total loss of support, and that's a
separate mechanism (`_detect_zero_support`, discharged via `support_lost` — see Stage 1.1
below), not cascade.

## Membership

Source: `VALID_NODE_TYPES` and `CLAIM_BEARING_TYPES`, `src/engram/engram_core.py`, re-verified
against `dev` at `f676721` (this doc's own tip drifted once already — see the note at the end
of this doc's revision history below — so this section states its source commit explicitly
rather than trusting "current" to stay true on its own).

**CB types (6, current):** `observation_factual`, `observation_predictive`, `derivation`,
`theory`, `axiom`, `conjecture`.

**`lesson` has been removed — this is done, not pending.** PR #2263 (Stage 1.6/1.6a, merged
`7c1ba57`, 2026-08-26T18:47:17Z) dropped `lesson` from `CLAIM_BEARING_TYPES` and backfilled
existing lesson confidences to `NULL`. **One record, stated once, to resolve an internal
inconsistency an earlier draft of this doc had** (this section said Stage 1.3 removes it; the
confidence-rule section below said #2263/1.6 does — both can't be the record): the epic's W2
workstream *named* lesson-removal as part of "enforce for future nodes," but it *shipped* under
Sol's 1.6/1.6a rung, ahead of and independent from Stage 1.3's remaining scope (below). 1.3 is
not what removed it. Historical note from the issue, worth keeping regardless: `lesson` was
**not** in `CLAIM_BEARING_TYPES` at the initial alpha commit (`51448db`); it was added four
days after the type's own defining derivation was written (`ff83eb2`, 2026-04-19) — so this
removal may be a restoration, not a change. The *why* of the original addition wasn't read from
the commit, so that's an interesting fact, not a load-bearing claim.

**NCB types (13):** `evidence`, `prediction`, `contradiction`, `question`, `definition`,
`goal`, `goal_tension`, `feeling_report`, `task`, `person`, `cornerstone`, `lesson`,
`trust_signal`.

**`trust_signal` is a real, live type with an unresolved cascade tension — named here rather
than silently omitted.** It does not appear in `VALID_NODE_TYPES` at all (a separate,
already-tracked gap — issue #2203, filed by Borges, Lei has ruled on the type-set half) — so a
naive "NCB = `VALID_NODE_TYPES` minus `CLAIM_BEARING_TYPES`" derivation misses it entirely,
which is exactly how an earlier draft of this doc did. It is unambiguously NCB by the
partition's own definition (a trust signal carries no truth-evaluable claim), but its write
path (`engram_trust.py`, `_add_trust_signal_impl`) mints a `derives_from` edge FROM the new
`trust_signal` node TO the citing `observation` (the `about`-relation edge, separately, points
at the person the signal concerns — the cascade-relevant edge is the `derives_from` one, not
that one) — and `derives_from` is `cascade: True`. So if that observation is ever retracted,
the taint-cascade walker (`_walk_cascade_downstream`, `engram_revision.py`) walks to whatever
cites it via a cascade relation — which includes the `trust_signal` node, since its skip rule
("cs/ls skip rule") exempts exactly two types, `cornerstone` and `lesson` — not `trust_signal`.
So an NCB node type is, today, a live cascade-walk participant through this one edge. Borges has logged
the cascade half of this alongside the type-set half on #2203 rather than opening a second
tracker. **Flagged as an open item for Stage 1.3 to resolve** (either exempt `trust_signal` in
the skip rule, or argue why it should stay a cascade participant) — not resolved by this doc.

**`prediction` has the identical structural gap — independently found by Borges (issue #2252
comment, 2026-08-26T18:08:07Z) while specc'ing 1.3, not by this doc's author, and worth stating
here rather than leaving it isolated as a test-update footnote later in this doc (the
pairing-invariant section below).** `_walk_cascade_downstream`'s skip tuple exempts exactly
`cornerstone` and `lesson`; `prediction` is NCB too and is absent from it, same as
`trust_signal`. Unlike `trust_signal`, this isn't only a read-the-code inference:
`tests/test_supported_by.py::test_retract_predictive_observation_taints_prediction` currently
**passes**, proving the walk taints a `prediction` today. The underlying question — which NCB
types the skip rule should exempt — is itself a **membership question** under this doc's own
derive-vs-enumerate criterion (below: "derive where the question IS classification membership"),
so this doc deliberately does not pre-answer it by hand-listing `{trust_signal, prediction}` as
the fix. Borges's own read is that the skip-list should become derived from the NCB partition
rather than enumerated — but flags that as widening 1.3's scope beyond a call he can make alone.
**Flagged as an open item for Stage 1.3, same status as trust_signal above** — not resolved by
this doc.

**`prediction` is NCB — this reverses an earlier draft in the issue and is worth stating
explicitly because the reversal is easy to miss on a partial read.** Lei's own tentative call in
the issue's open-questions section was CB; Kepler's first design read (2026-08-26 ~01:23Z) sided
with that verdict. The issue's own later "Q1 FINAL" synthesis (~01:3xZ) superseded **both** —
Lei's tentative CB call is the position dropped, not credited. What survives is Kepler's
calibration argument, re-addressed: his CB verdict had the right concern (predictions must stay
calibration-bearing) at the wrong address (the `pr_*` node itself); the synthesis keeps that
concern and moves it onto the paired `observation_predictive` instead:
`pr_*` nodes are resolution trackers — structurally the *question* pattern, not a claim. A
`prediction` node's `claim` and `confidence` fields are **NULL by design**; the actual
claim-bearing content and its confidence live on the **paired `observation_predictive`** node,
connected by the pairing invariant (below). This is the currently-shipped state: `prediction`
does not appear in `CLAIM_BEARING_TYPES` on `dev` today.

## The confidence rule

CB nodes carry confidence. NCB nodes carry `NULL` confidence, always — no exceptions, no
"neutral default." Per #2252's own measured-scope audit (Borges's graph, 2026-08-25, re-measured
at filing time): goals were already 16/16 NULL, cornerstones already 71/71 NULL — the partition
was already close to de facto before this epic named it. `lesson` (126 nodes carrying
confidence at the time of the audit) was the one live violation this measurement found —
closed, not by Stage 1.3, but by 1.6/1.6a (PR #2263, merged), which backfilled all 126 to
`NULL`. `trust_signal` (above) was not in the audit's own type enumeration, so it is not
counted in "already close to de facto" — its confidence-column state hasn't been independently
re-checked by this doc.

**A node's importance-seeding path is not the same path as its stored confidence.**
`_stamp_new_node`'s `confidence`/`surprise` parameters feed the *importance* formula
(`_importance_base_for_node`), not the node's stored `confidence` column — the two are easy to
conflate because they share a parameter name. Cornerstones illustrate the separation cleanly:
`engram_cornerstone.py`'s calls (`_add_cornerstone_impl`, `_outgrow_cornerstone_impl`) pass
`importance_base=2.0` explicitly (as of #2250/#2266's cleanup — no `confidence=` argument at
the call site at all; `_stamp_new_node`'s default `confidence=0.5` flows through unused once
`importance_base` is given), while the node's actual stored `confidence` stays `NULL`
throughout (cornerstones are NCB). Not a partition violation — a parameter-naming trap in a
shared helper, closed by #2250/#2266's dead-write removal.

## The edge partition

Source: `EDGE_CLASSIFICATIONS`, `src/engram/engram_core.py` (current `dev`, post-#2255/#2262).
This is the actual shipped declaration — read from source, not reconstructed from the issue
thread's earlier drafts, several of which (see below) turned out to be wrong about specific
flag values.

| relation | cascade | provenance | dag_check | removable | notes |
|---|---|---|---|---|---|
| `derives_from` | **True** | **True** | True | False | CB cascade relation — taint/stale propagation |
| `supported_by` | **True** | **True** | True | False | CB cascade relation (see caveat below) |
| `cites` | False | True | True | False | provenance-only; overloaded with ~5 meanings (#2243/#4, separate workstream) |
| `retracts` | **False** | False | True | False | revision-history: source exists *because* target was wrong (1.0a) |
| `supersedes` | **False** | False | True | False | revision-history: same family as `retracts` (1.0a) |
| `about` | False | False | False | True | pure relational/aboutness |
| `contradicts` | False | False | False | False | structural commitment, DAG-exempt |
| `exemplifies` | False | False | False | True | incident→lesson membership, DAG-exempt |
| `instantiates` | False | False | False | True | achievement-shaped relevance marker, DAG-exempt |
| `resolves` | False | False | False | False | back-reference, DAG-exempt |
| `serves` | False | False | False | True | intent-shaped relevance marker, DAG-exempt |
| `subtask_of` | False | False | True | True | |
| `tensions` | False | False | False | True | symmetric, DAG-exempt |

**`derives_from` = CB cascade relation; `supported_by` = CB cascade relation TODAY, with a
known future narrowing.** The issue's own early W0 draft line ("the edge partition:
`derives_from` = CB cascade relation; `supported_by` = NCB accumulation, no cascade") **does
not match the shipped declaration** and should not be treated as settled — `supported_by`
carries `cascade: True` and `provenance: True` on `dev` right now, confirmed directly from
`EDGE_CLASSIFICATIONS` and from Kepler's Stage-1.0a ruling itself: *"`cascade=True` should be
exactly `{derives_from, supported_by}`."* The draft line was aspirational (a description of
where `supported_by` is headed once its NCB-accumulation uses — 99.2% of current instances,
cornerstone-sourced — are the only ones left), not a description of the current declaration.
**Stage 1.3 is where `supported_by` is planned to stop carrying cascade** (per the issue's own
W2 workstream: *"stop `supported_by` carrying cascade"*) — until that lands, treat
`supported_by` as CB-cascade-bearing, full stop.

**Why `retracts`/`supersedes` are `cascade: False` despite superficially looking like
revision edges that "should" propagate** (Stage 1.0a, Kepler's ruling, decided by a concrete
falsifier rather than by pattern-matching the declaration to intuition): a `retracts` edge
marks the *correction of* a tainted node, not a dependency on it — the source exists *because*
the target was found wrong. The decisive case: `_retract_impl`'s retract-with-replacement path
(`engram_revision.py`) inserts `replacement —retracts→ retracted_node`; walking this relation
as cascade-bearing would make a retract cascade's first taint target the replacement the
retract just created. `supersedes` is the same family, and its widening would have been latent
rather than loud (a current node can't carry an incoming `supersedes` edge — that's what makes
it non-current — so the walk stops there regardless; the stale-if-widened flag would only ever
fire on invariant-violating data).

### Derived constants

```python
CASCADE_RELATIONS = frozenset(r for r, c in EDGE_CLASSIFICATIONS.items() if c["cascade"])
PROVENANCE_RELATIONS = frozenset(r for r, c in EDGE_CLASSIFICATIONS.items() if c["provenance"])
```

Both are **derived from the declaration**, not re-enumerated at each call site — this is
Stage 1.0b's own load-bearing change (#2255), and it's the mechanism this whole partition
depends on for *future* correctness: a relation's `cascade`/`provenance` flags are the single
source of truth, and every walk that should track them automatically does, with no per-site
hand-update. Confirmed live consumers: `_trace_evidence_roots` (provenance walk,
`engram_core.py`) and the taint-cascade walk (`engram_revision.py`). **Not yet a consumer,
flagged as reviewer-fairy caught an earlier draft overclaiming this as "confirmed live":** the
health-score's cascade-conformance queries (`cascade-causing edge with an NCB endpoint`, etc.)
are the issue's own **proposed** red-flag-channel candidates for Stage 3, not yet built —
`_compute_health_score` (`engram_stats.py`) reads `DAG_EXEMPT_RELATIONS` and raw metadata
today, with zero references to `EDGE_CLASSIFICATIONS`/`CASCADE_RELATIONS`/`PROVENANCE_RELATIONS`
(confirmed by direct grep). Whoever builds Stage 3's red-flag channel is the first real
consumer here.

## Carve-outs — deliberately enumerated, never derived

**The general principle, and its exception class, both from the issue (Ariadne's, adopted by
Borges and Kepler):** *derive from the classification, never re-enumerate it* — **except**
where the enumeration IS the specification: a boundary whose membership is a deliberate human
decision, not a computable property. **Her discriminator: does a new member acquiring the
property deserve to join automatically?** If yes, derive. If a human should decide, enumerate
— and staleness there is a *feature*, because it forces the decision to be made explicitly
rather than drift in silently.

Two sites are enumerated on purpose and will **not** auto-follow the `cascade`/`provenance`
flags, even after Stage 1.3 changes them:

1. **`CITATION_SUPPORT_RELATIONS`** (`engram_core.py`) — `frozenset({"derives_from",
   "supported_by"})`, consumed by `engram_lifecycle.py`/`engram_stats.py` for a
   citation-coverage question ("is this observation cited by anything?"), which is NOT the
   same question as cascade even though today's membership happens to coincide. The
   constant's own comment states the reason explicitly: *"#2252 Stage 1.3 will remove
   `cascade` from `supported_by`, and if this constant were derived from `CASCADE_RELATIONS`
   instead, the uncited-observation metric would silently change meaning that day with no test
   to catch it."* This is the load-bearing hand-update site for Stage 1.3 — when `supported_by`
   loses cascade, `CITATION_SUPPORT_RELATIONS` does **not** change (citation-coverage still
   wants both relations), and that divergence from `CASCADE_RELATIONS` is exactly the point.

2. **`_trace_observation_leaves`'s premise-spine walk** (`engram_core.py`) — hardcodes
   `relation IN ('derives_from', 'supported_by')` rather than deriving from
   `CASCADE_RELATIONS`, per its own comment: *"this walk's exclusion of `cites` is a commitment
   about what counts as a PREMISE, and today's cascade set happening to equal `{derives_from,
   supported_by}` is coincidence, not cause… Deriving this from `cascade` would make 'premise
   spine' track a flag that means 'taint propagates here' — a different question that merely
   agrees with this one today. Do not fix this to `CASCADE_RELATIONS`."* Same shape as #1: a
   future change to the `cascade` flag must not silently redefine what counts as a premise.

**The derive-vs-enumerate criterion, stated precisely (Kepler/Borges, folded into this doc at
their request — this is the rider that resolves an apparent tension in the two rules above):**
Ariadne's general discriminator and Stage 1.3's own enforcement requirement look contradictory
at first read — the classification is *enumerated* (Layer A: a human decides what's
claim-bearing, so a future type carrying a claim field doesn't auto-join) while enforcement
*derives* (nothing re-lists the types inline at a check site). They aren't in tension; they
govern different objects:

> **Derive** where the question IS classification membership. **Enumerate** where the set
> merely COINCIDES with a classification today.

The partition's **membership** (which types are CB, which relations cascade) is the enumerated
object — a person decides it, and it changes only by an explicit edit to
`CLAIM_BEARING_TYPES`/`EDGE_CLASSIFICATIONS`. **Checks against that membership** — a query
asking "is this node claim-bearing," a walk asking "does this relation cascade" — are the
derived object, and must read the declaration rather than re-list types/relations inline.
`CITATION_SUPPORT_RELATIONS` and `_trace_observation_leaves`'s premise-spine literal are the
named exception to the *derive* half specifically because each one's question only coincides
with today's cascade membership — it isn't asking the membership question itself.

**The canonical accessor for the derive half already exists and already carries this
doctrine:** `core._is_claim_bearing(node_type)` — `return node_type in CLAIM_BEARING_TYPES` —
added during PR #2261's colleague review (credited to Sol Montaigne, who built the same helper
independently for the mirror-image case; both landed in the merged PR). Its docstring states
the "derive from the classification, never re-enumerate it" doctrine in these terms already.
This SPEC ratifies what the code already names — the easy direction — rather than introducing
new doctrine.

Two more carve-outs, named in the issue's own six-way audit of the (now-collapsed) nine
hardcoded relation literals, not yet independently re-verified against current source by this
doc's author — flagged as such rather than asserted:

3. **`engram_query.py`'s `_UPSTREAM_OUTGOING` inspect-directionality set** (defined ~line 962,
   re-verified against `dev` at `f676721`) — which relations render as "upstream" in
   `engram_inspect` is a UX decision (Ariadne's exception class: enumerate, not derive). A new
   cascade-bearing relation does not automatically deserve to render upstream.

4. **A display-label mismatch** (`engram_core.py`, the `lines.append(f"Derives from:
   {', '.join(support_ids)}")` line — cited by the searchable string rather than a bare line
   number, after a **ref-mismatch, not a drift**: a prior round's `:4370` was correctly measured,
   but on this PR **branch's own base** (`76a4de1`), not on the `dev` at `f676721` it was labeled
   against — where the same line is `:4389` — renders `cites` edges under this label,
   contradicting the premise-spine's own stated exclusion of `cites`) — the issue names this a
   **separate, pre-existing bug**, explicitly not to ride along with the cascade-flag work. Filed
   as **#2254**, confirmed open as
   of this doc.

## The pairing invariant (Q1, settled)

Predictions are NCB (above). Calibration survives that ruling via the **pairing invariant**:
every `prediction` node is `supported_by` a paired `observation_predictive` node that carries
the actual claim and a real confidence. The `prediction` node itself tracks `predicted_event`,
`status`, `resolved_by` — question-pattern fields, not claim fields.

**The relation name for this specific edge is an open item, not yet decided — do not write
code against a name from this doc.** The issue's earlier text named a target (`predicted_by`),
but that name was chosen when predictions were still (incorrectly) ruled CB, and reads
backwards on the edge's actual direction (source = prediction, target = the observation; "the
prediction is predicted_by the observation" doesn't parse — the observation doesn't predict
the tracker). This is being resolved separately by Borges as its own new-relation design call
(needs an `EDGE_CLASSIFICATIONS` row under Stage 1.0a's gate: `cascade=False`, since NCB nodes
don't cascade), tracked outside this doc's scope. Until that lands, the pairing edge is
`(prediction_id, observation_id, 'supported_by')` — same relation cornerstones use, different
semantic, both currently sharing the literal string.

**A consequence worth flagging for whoever builds the pairing-relation rename — corrected here
after a real gap Borges found (issue #2252 comment, 2026-08-26T18:08:07Z) that an earlier draft
of this doc didn't account for:** the taint-walk test
`tests/test_supported_by.py::test_retract_predictive_observation_taints_prediction` currently
asserts the OLD (CB) semantics — that retracting the paired observation taints the prediction —
and it currently **passes** (see the membership-section note above: `prediction` is absent from
`_walk_cascade_downstream`'s skip tuple, same gap as `trust_signal`). Under the settled NCB
ruling this assertion is stale and the test should eventually go red.

**The replacement is not simply "assert the inverse" — that presupposes the inverse is
writable, and until now it was not.** Once the pairing edge stops carrying cascade (Stage 1.3's
`supported_by` change), a `prediction` whose paired observation is later retracted gets no marker
at all: none of the four `VALID_PREDICTION_OUTCOMES` values (`confirmed`, `partially_confirmed`,
`refuted`, `partially_refuted`) represents "the observation this was minted from was retracted."
Such a prediction would sit open and unmarked, permanently, with no outcome to assert against —
today's cascade taint is quietly doing orphan-detection work for this case, not confidence
propagation, and that's the one job Stage 1.3's `supported_by` change removes without naming a
replacement.

**Answered, tracked separately: engram-alpha#2281** (filed after this doc's tip; Borges's design
read). **Not a fifth `VALID_PREDICTION_OUTCOMES` value** — the four existing members all answer
*did the predicted event happen*, a different question from *was the prediction's basis ever
valid*, and the predicted event may still be genuinely pending when the basis is gone. **The
mechanism already exists: retraction.** Retract means never-valid/error; supersede means
was-valid-then-updated. A prediction minted from an observation that turns out fabricated was
never validly grounded — void ab initio, not refuted — so the fix is a `prediction` branch in
`_retract_impl` (propagating the parent's `error_type`), or a broader NCB-orphan sweep, not new
enum vocabulary.

**Load-bearing constraint for whoever writes the inverse test, preserved from #2281 because the
wrong version of this test erases the gap it should be guarding:** write it NARROWLY — assert
only that the prediction is not cascade-tainted. Do **not** write it as "nothing happens to the
prediction," and this doc does **not** say the prediction is *correctly* left alone — a test
asserting a gap is intended behavior is the hardest kind to ever remove, because the next reader
sees a green test and closes their own investigation before #2281's proposed fix ever lands.
#2281 is filed under the still-open umbrella epic (#2252); implementing its proposed fix remains
open work.

## Lesson confidence (Q2, settled) — confidence dropped; maturity preserved by a different mechanism

Reopened from an earlier "NULL it" proposal, then reversed: lesson confidence is measured to be
decorative for its stated purposes (the tripwire hook fires on `situation_pattern` matching,
zero confidence reads; retrieval ranking never reads it; the one consumer, a `min_confidence`
filter, was reading it as a truth-threshold — the exact category error this partition removes).
The honest maturity signal already exists and needs no new stored field:
`core._count_live_exemplars`, computed on demand, always current. Status: PR #2263 (Stage
1.6/1.6a) implements this — drops `lesson` from `CLAIM_BEARING_TYPES`, stops
`_add_lesson_impl` computing/writing confidence, migrates existing rows, fixes the shipped
template's confidence-anchor doc paragraph. **Merged** (`7c1ba57`, 2026-08-26T18:47:17Z).

## The one normative-use exception (Q3, settled — design named, not yet built)

Of the 29 `derivation -derives_from-> lesson` edges the audit found, 28 are specimen/instance
uses (never functioned as real premises). One genuinely applies a lesson's *normative* content
as the major premise of a practical syllogism — shaped like *"this lesson establishes the
principle... therefore this action"* — rule-application, not truth-inheritance. (Node id
intentionally omitted: it names a claim in one agent's graph and would not resolve in any
other reader's — see the "why the gate is right" note this doc's own PR round surfaced.)
Settled answer: this gets its **own relation** (not `derives_from`, not
bare `about`), carrying **zero confidence math** plus a **staleness hook on principle
retirement** (a node applying a principle should learn when that principle is retired). Not
yet built as of this doc — no relation name or issue number confirmed; flagged as open rather
than invented here.

## Status as of this doc (2026-08-26)

| workstream | status | PR/issue |
|---|---|---|
| W0 — this doc | this doc | — |
| W1 — `support_lost` discharge | merged | PR #2262 |
| W2 (part) — cascade flag load-bearing | merged | PR #2255 |
| 1.4 — predictive-hint write-path affordance | merged | PR #2260 |
| 1.7 — `min_confidence` CB-only predicate | merged | PR #2261 |
| 1.5 (part) — #2250 cornerstone dead-write | merged | PR #2266 |
| 1.6/1.6a — drop lesson confidence | **merged** | PR #2263 |
| W2 (remainder) / 1.3 — refuse confidence on NCB types generally, refuse NCB nodes as premises, stop `supported_by` carrying cascade | not started, Borges specc'ing (open design questions to Kepler as of this doc) | — |
| W3/W4 — migration + `cites` re-typing | not started | — |
| 1.5 (remainder) — pairing-edge rename, misfiled edge | not started, Borges's lane | issue #2265 (blocker) |
| W5 — cascade machine | blocked on W0-W4 | issue #2013 |

**Note on 1.3's scope, corrected from an earlier draft of this doc:** 1.3 is **not** "the
confidence rung" — `lesson`'s confidence removal already shipped via 1.6/1.6a, ahead of and
separately from 1.3. 1.3's remaining scope per the epic's W2 workstream is the other three
enforcement pieces: refuse confidence on NCB types as a general write-path guard (not just
`lesson`'s case), refuse NCB nodes as premises, and stop `supported_by` carrying cascade. The
`trust_signal` cascade-tension flagged above is a candidate item for 1.3 to resolve, not yet
assigned there as of this doc.

This doc gates 1.3: its enforcement (refuse confidence on NCB types generally, refuse NCB
nodes as premises, stop `supported_by` carrying cascade — `lesson`'s own confidence removal is
already done, via 1.6/1.6a, not 1.3) is correct exactly to the extent it matches the
membership, confidence rule, and edge partition stated above — not the other way around. If
1.3's implementation needs to diverge from this doc, the divergence is a doc bug to fix here
first, not a silent implementation choice.
