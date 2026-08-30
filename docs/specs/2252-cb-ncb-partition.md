# Spec: #2252 W0 — the claim-bearing / non-claim-bearing type partition

**Author:** Luria Hebb · **Date:** 2026-08-26, doc-alignment pass 2026-08-27 (issue #2299)
re-verified against `dev` at `6b794df`. This is
the **design of record** (#2252's W0 workstream) — it ratifies what was settled with @LeiShi
in session (2026-08-25 evening) and refined through Stage 1.0/1.0a/1.0b (Borges, Kepler,
Ariadne), reconciled against the code as merged through this doc's latest revision (`4431e89`
#2255, `3d5cc6e` #2262, `4ff3fcb` #2260, `9f411f5` #2261, `76a4de1` #2266, `7c1ba57` #2263,
`ac0e429` #2289 Stage 1.3, `6f8f21e` #2292, `6b794df` #2296 W6).
**Nothing this doc describes is new design** — every claim below is either a direct read of
`EDGE_CLASSIFICATIONS`/`CLAIM_BEARING_TYPES`/`VALID_NODE_TYPES` on the commit named above, or a
settled answer already recorded in issue #2252. Where the issue's own early draft text
disagrees with what actually shipped, this doc follows the shipped code and says so explicitly
— a stale draft line is not the design of record; the merged declaration is.

**2026-08-27 correction (issue #2299), stated plainly because the thing it corrects was itself
stated plainly and confidently:** Stage 1.3 (#2289) landed the day after this doc's prior
revision and flipped `supported_by`'s `cascade` flag to `False` — the exact narrowing the "edge
partition" section below had described as pending. The section's own caveat paragraph asserted,
with two confidence markers (*"confirmed directly from `EDGE_CLASSIFICATIONS`"* and *"full
stop"*) built specifically to discourage re-checking, that `supported_by` still carried
`cascade: True`. Once 1.3 merged, that paragraph was actively wrong — not stale-and-harmless,
wrong-and-authoritative, since this file is the design of record. Found by Borges while
reviewing W6 and reported on #2289; Ariadne corrected the framing (Borges's comment originally
read as though the doc were already false on `dev`, when at that time `dev` still agreed and
only the stack disagreed) and filed #2299 as its own issue so the fix has an address that
survives #2289 closing. Fixed below; the caveat is kept as history (not deleted) per the same
instinct that keeps this section's own revision-history note above.

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

**`trust_signal` is a real, live type whose cascade tension was resolved by Stage 1.3, named
here as history rather than silently omitted.** It does not appear in `VALID_NODE_TYPES` at all
(a separate, already-tracked gap — issue #2203, filed by Borges, Lei has ruled on the type-set
half) — so a naive "NCB = `VALID_NODE_TYPES` minus `CLAIM_BEARING_TYPES`" derivation misses it
entirely, which is exactly how an earlier draft of this doc did. It is unambiguously NCB by the
partition's own definition (a trust signal carries no truth-evaluable claim), and its write path
(`engram_trust.py`, `_add_trust_signal_impl`) mints a `derives_from` edge FROM the new
`trust_signal` node TO the citing `observation` (the `about`-relation edge, separately, points
at the person the signal concerns — the cascade-relevant edge is the `derives_from` one, not
that one).

> **History (the gap as it stood before Stage 1.3):** `derives_from` is `cascade: True`, so if
> that observation was ever retracted, the taint-cascade walker (`_walk_cascade_downstream`,
> `engram_revision.py`) would walk to whatever cited it via a cascade relation — which included
> the `trust_signal` node, since its skip rule then ("cs/ls skip rule") exempted exactly two
> types, `cornerstone` and `lesson` — not `trust_signal`. An NCB node type was, at that point, a
> live cascade-walk participant through this one edge. Borges logged the cascade half of this
> alongside the type-set half on #2203 rather than opening a second tracker; flagged as an open
> item for Stage 1.3 to resolve.

**Resolved by Stage 1.3 (#2289, merged), via the derive route Borges anticipated rather than a
hand-list fix:** `_walk_cascade_downstream`'s skip check is no longer an enumerated tuple — it
now reads `not core._is_claim_bearing(node["type"])`, so every NCB type, `trust_signal` included,
is automatically exempted regardless of which specific types exist today or get added later. No
`{trust_signal, prediction}` hand-list was ever written; the underlying membership question this
doc's own derive-vs-enumerate criterion (below) says should be derived, is.

**`prediction` had the identical structural gap and is resolved by the same mechanism —
independently found by Borges (issue #2252 comment, 2026-08-26T18:08:07Z) while specc'ing 1.3,
not by this doc's author, and worth stating here rather than leaving it isolated as a
test-update footnote later in this doc (the pairing-invariant section below).**

> **History:** `_walk_cascade_downstream`'s skip tuple then exempted exactly `cornerstone` and
> `lesson`; `prediction` was NCB too and was absent from it, same as `trust_signal`. Unlike
> `trust_signal`, this wasn't only a read-the-code inference: the taint-walk test then named
> `test_retract_predictive_observation_taints_prediction` passed, proving the walk tainted a
> `prediction` at that point.

Now covered by the same derived skip (`not core._is_claim_bearing`) as `trust_signal` above —
`prediction` is NCB, so the walker stops at it unconditionally. The orphan-detection
consequence this created for the prediction-pairing edge specifically is a separate, now-also-
resolved matter — see "the pairing invariant" below (engram-alpha#2281, merged as PR #2292).

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

Source: `EDGE_CLASSIFICATIONS`, `src/engram/engram_core.py` (current `dev`, post-#2289/#2296).
This is the actual shipped declaration — read from source, not reconstructed from the issue
thread's earlier drafts, several of which (see below) turned out to be wrong about specific
flag values.

**Column note (#2357, 2026-08-29):** `dag_check` was renamed `temporal_causal`
(pure rename, same values below — every relation's creation-order check is
unchanged) and split from a new, independent `dag` (real edge-walk
acyclicity) flag, not shown in this table (this doc covers the CB/NCB
partition, not DAG/acyclicity design — see `engram_dag_conformance.py` and
issue #2357 for that property). For every relation below except
`acknowledged_derives_from`, `dag` equals `temporal_causal`.

| relation | cascade | provenance | temporal_causal | removable | notes |
|---|---|---|---|---|---|
| `derives_from` | **True** | **True** | True | False | CB cascade relation — taint/stale propagation |
| `acknowledged_derives_from` | **True** | **True** | **False** | False | #2357/#2013 cascade-frontier acknowledgement edge — deliberately backwards in creation time; `dag=True` protects it instead |
| `supported_by` | **False** | **True** | True | False | NCB accumulation (cornerstone-sourced vote edges) — see history below |
| `predicted_by` | **False** | **True** | True | False | prediction-pairing edge, split out of `supported_by` (#2252 W6) — see "the pairing invariant" below |
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

**`derives_from` = CB cascade relation; `supported_by` = NCB accumulation, no cascade — settled,
as of Stage 1.3 (#2289, merged).** The table above is the current, shipped state; the paragraph
below is kept as **history**, not current state (per this doc's own "don't delete a dated-true
passage" instinct, exercised earlier for the `lesson`/1.6 note above) — it records why the
narrowing happened and, separately, is the exact passage that went stale-while-confident when
1.3 landed (issue #2299).

> **History (accurate through 2026-08-26, superseded 2026-08-27 by #2289's merge):** The
> issue's own early W0 draft line ("the edge partition: `derives_from` = CB cascade relation;
> `supported_by` = NCB accumulation, no cascade") did not match the shipped declaration at the
> time this doc was first written, and was not yet settled — `supported_by` carried
> `cascade: True` and `provenance: True` on `dev` at that point, confirmed directly from
> `EDGE_CLASSIFICATIONS` and from Kepler's Stage-1.0a ruling itself: *"`cascade=True` should be
> exactly `{derives_from, supported_by}`."* The draft line was aspirational then (a description
> of where `supported_by` was headed once its NCB-accumulation uses — 99.2% of instances at
> audit time, cornerstone-sourced — were the only ones left), not a description of the
> declaration as it stood. Stage 1.3 is what carried out that narrowing; the draft line the
> issue started with turned out to be exactly right about the destination, just early about the
> arrival.

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

One site is enumerated on purpose and does **not** auto-follow the `cascade`/`provenance`
flags — including after Stage 1.3 (#2289) changed them. A second site (`_trace_observation_leaves`)
was enumerated for the same reason through 2026-08-27, but #2298 (ruled 2026-08-28) superseded that
doctrine for it specifically — see the history note after item 1 below.

1. **`CITATION_SUPPORT_RELATIONS`** (`engram_core.py`) — `frozenset({"derives_from",
   "supported_by"})`, consumed by `engram_lifecycle.py`/`engram_stats.py` for a
   citation-coverage question ("is this observation cited by anything?"), which is NOT the
   same question as cascade, and — since Stage 1.3 (#2289, merged) — no longer even
   numerically the same set: `CASCADE_RELATIONS` is now the strict subset `{derives_from}`,
   while this constant still deliberately includes `supported_by`. Re-verified directly in
   source (2026-08-27, issue #2299): the constant's own comment was itself updated by #2289's
   own commit to describe this in the past tense, not left stale —
   *"#2252 Stage 1.3 (now landed) removed `cascade` from `supported_by`, so the two sets are no
   longer even numerically equal... Had this constant been derived from `CASCADE_RELATIONS`
   instead of hand-enumerated, the uncited-observation metric would have silently changed
   meaning on the day Stage 1.3 landed, with no test to catch it — the now-realized divergence
   is exactly why it stays hand-enumerated."* Worth naming as its own small data point for this
   doc's own thesis: the code comment upgraded itself from a "will happen" caveat to a
   "did happen, and here's the proof it mattered" record on the same PR that made it true — this
   doc's caveat paragraph (the edge-partition section above) needed a follow-up pass to do the
   same; the source comment did not lag behind its own change.

> **History — `_trace_observation_leaves`'s premise-spine walk was carve-out #2 through
> 2026-08-27, and no longer is (#2298, ruled 2026-08-28).** It hardcoded
> `relation IN ('derives_from', 'supported_by')` rather than deriving from `CASCADE_RELATIONS`,
> per its own comment (as it read through yesterday's #2296): *"this walk's exclusion of `cites`
> is a commitment about what counts as a PREMISE, and today's cascade set happening to equal
> `{derives_from, supported_by}` is coincidence, not cause… Deriving this from `cascade` would
> make 'premise spine' track a flag that means 'taint propagates here' — a different question
> that merely agrees with this one today. Do not fix this to `CASCADE_RELATIONS`."* — same shape
> as #1, reasoned the same way.
>
> This doc independently re-verified and ratified that exact doctrine on 2026-08-27 (issue
> #2299, the pass immediately above), without either side having #2298 in view: #2298 was filed
> 2h20m earlier the same evening, arguing the opposite. Flagged as a live conflict on #2298 and
> the forum rather than resolved unilaterally either way (2026-08-28). **Lei's ruling: #2298
> supersedes the #2255 doctrine for this walk specifically** — post-partition, "what is a
> premise" and "what should cascade" are the same question by construction (a node's evidential
> lineage IS its cascade ancestry), not two questions that merely coincide. `supported_by` drops
> out of the walked set as a consequence (behavior-preserving: every `supported_by` edge is
> minted with a cornerstone or lesson as its SOURCE — never its target, "exemplar" is not a
> target-type guarantee, a reviewer-fairy catch on PR #2329 that corrected an earlier draft of
> this note — and since those source types are non-claim-bearing, `engram_derive` structurally
> rejects them as `derives_from` targets, so this claim-bearing-only walk can never land on one
> to descend into its own `supported_by` edges — independently re-verified against source before
> the fix landed, not just asserted). The walk now derives
> from `CASCADE_RELATIONS` via the shared `_walk_cascade_upstream` primitive (`engram_core.py`),
> which also supplies the #2013 cascade machine's future `ancestors(X)` traversal (mode-C
> travel) — one traversal, two filters, per #2298 §2's unification. `CITATION_SUPPORT_RELATIONS`
> (item 1 above) is UNCHANGED by this ruling — it answers a genuinely different question
> (citation-coverage / health-metric semantics), not premise-hood.

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
`CITATION_SUPPORT_RELATIONS` is the named exception to the *derive* half specifically because
its question only coincides with today's cascade membership — it isn't asking the membership
question itself. (`_trace_observation_leaves`'s premise-spine walk was a second named exception
through 2026-08-27; #2298 ruled it out of this class — see the history note above.)

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
every `prediction` node is `predicted_by` a paired `observation_predictive` node that carries
the actual claim and a real confidence (named `predicted_by` per the ruling below; historically
this pairing edge shared the `supported_by` literal with the unrelated cornerstone-vote usage —
see that ruling for why and when the rename landed). The `prediction` node itself tracks
`predicted_event`, `status`, `resolved_by` — question-pattern fields, not claim fields.

**The relation name is ruled: `predicted_by`** (Lei, 2026-08-27; #2252 W6). The issue's
earlier text named this same target, and it carries the same backwards-reading tension this
doc's earlier draft flagged: "the prediction is predicted_by the observation" doesn't parse on
a naive reading, since the observation doesn't predict the tracker. The ruling keeps the name
anyway, on the SAME direction the pairing edge already had as `supported_by` — prediction
stays the SOURCE, the observation stays the TARGET; nothing about the stored direction changes,
only the relation's name. A conceptual diagram elsewhere (in #2252) draws the arrow the other
way (predicting act → record) — that is narrative flow, not the edge's storage direction, and
is not a reason to "fix" the stored direction to match it. A reader who meets `predicted_by`
alone, with no second edge for contrast, is the case most likely to misread it — so show both
prediction edges together, direction included:

```
observation_predictive --(predicted_by)--> prediction <--(resolves)-- observation_factual
     the predicting act                     the record        the verifying event
```

(The arrow in this diagram points FROM the observation for readability — "the observation
predicted this" reads left to right. The STORED edge has `prediction` as the SOURCE and
`observation_predictive` as the TARGET, same as the diagram's `resolves` edge has
`observation_factual` as the source and `prediction` as the target. Say this explicitly
because the arrow-vs-storage ambiguity is exactly what confused an earlier reader of this
doc.)

The pairing edge is `(prediction_id, observation_id, 'predicted_by')` — split out of
`supported_by` (which cornerstones still use, unrelated semantic, no longer sharing the
literal string with this pairing). `EDGE_CLASSIFICATIONS['predicted_by']` carries
`cascade=False` (settled per Q1 above — NCB nodes don't cascade), `temporal_causal=True`,
`provenance=True`, matching `supported_by`'s own non-cascade fields; not hand-addable
(`addable_after_creation=False`) — minted once at node-add time only. Landed in #2252 W6.

**Resolved: engram-alpha#2281, merged as PR #2292 (2026-08-27).** The paragraphs below record
the gap as it stood and the reasoning that shaped the fix — kept as history, not rewritten away,
per this doc's own discipline — followed by what actually shipped.

> **History.** A real gap Borges found (issue #2252 comment, 2026-08-26T18:08:07Z) that an
> earlier draft of this doc didn't account for: the taint-walk test then named
> `test_retract_predictive_observation_taints_prediction` asserted the OLD (CB) semantics — that
> retracting the paired observation taints the prediction — and it then passed, because
> `prediction` was absent from `_walk_cascade_downstream`'s skip tuple (same gap as
> `trust_signal`, above). Once the pairing edge stopped carrying cascade (Stage 1.3's
> `supported_by` change, then pending), a `prediction` whose paired observation was later
> retracted would get no marker at all: none of the four `VALID_PREDICTION_OUTCOMES` values
> (`confirmed`, `partially_confirmed`, `refuted`, `partially_refuted`) represents "the
> observation this was minted from was retracted" — the cascade taint had quietly been doing
> orphan-detection work for this case, not confidence propagation, and that was the one job the
> `supported_by` change would remove without naming a replacement. The fix judged correct at
> the time: **not** a fifth `VALID_PREDICTION_OUTCOMES` value (the four existing members answer
> *did the predicted event happen*, a different question from *was the prediction's basis ever
> valid*) but the existing retraction mechanism — retract means never-valid/error, supersede
> means was-valid-then-updated, and a prediction minted from a fabricated observation was never
> validly grounded, void ab initio, not refuted.

**What shipped, PR #2292:** a dedicated branch in `_retract_impl` (`engram_revision.py`) —
retracting an `observation_predictive` now **retracts** (terminal, `status='retracted'`,
`is_current=0` — not a taint marker) any `prediction` whose **sole live** basis it was, checked
via a direct query independent of the general cascade walk (which, post-1.3, cannot reach a
`prediction` at all — neither by the `supported_by`/`predicted_by` cascade flag, now `False`,
nor by the walker's NCB skip). A prediction with another live `observation_predictive` still
supporting it (the `linked_to_existing` reuse path, above) is left alone. Carries the parent's
`error_type`; a prediction already resolved before its basis is retracted has its prior
`status` preserved in `metadata.status_before_retraction` rather than silently displaced.
Covered by `tests/test_supported_by.py`'s prediction-retract tests (renamed from the stale
taint-assertion named above to `test_retract_predictive_observation_retracts_prediction_not_taints`,
plus dedicated multi-basis, no-paired-prediction, sequential-retraction, and
resolved-status-preservation tests). `engram_client.py`'s `engram_retract` tool description
was updated to disclose the terminal side-effect — a tool contract that only describes the
general taint case would leave a caller unable to predict that retracting one observation can
kill a prediction outright.

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

## Status as of this doc (2026-08-26, updated 2026-08-27 — issue #2299)

| workstream | status | PR/issue |
|---|---|---|
| W0 — this doc | this doc | — |
| W1 — `support_lost` discharge | merged | PR #2262 |
| W2 (part) — cascade flag load-bearing | merged | PR #2255 |
| 1.4 — predictive-hint write-path affordance | merged | PR #2260 |
| 1.7 — `min_confidence` CB-only predicate | merged | PR #2261 |
| 1.5 (part) — #2250 cornerstone dead-write | merged | PR #2266 |
| 1.6/1.6a — drop lesson confidence | merged | PR #2263 |
| 1.5 (remainder-of-remainder) — `_retract_impl` prediction-basis-retraction branch | merged | issue #2281, PR #2292 |
| W2 (remainder) / 1.3 — stop `supported_by` carrying cascade, NCB-derived cascade-walk skip, `_refuse_ncb_confidence` write-path guard | **merged** | PR #2289 (part of #2252) |
| 1.5 (remainder), rename half — pairing-edge rename (`supported_by` → `predicted_by` for the prediction-pairing edge specifically) | **merged** | PR #2296 (W6) |
| 1.5 (remainder), re-type half — the misfiled `supported_by` edge (a claim about which hooks can inject context, wrongly filed as derived-from a claim about handler types) re-typed to `about` | **still open — not resolved by this pass** | issue #2265 (blocker: no agent-facing path to re-type a provably-wrong `supported_by` edge, since `removable: False`) |
| W3/W4 — migration + `cites` re-typing | not started, as far as this pass checked | — |
| W5 — cascade machine (#2013 build table) | **build table open** (Kepler, 2026-08-27T21:12Z, forum #348) — the exit-criteria run on post-W6 `dev` is the stage's own precondition per the epic's derived-set mutation-site notes, not yet confirmed run as of this pass | issue #2013 |
| W5 (row) — `_trace_observation_leaves` premise-spine derives from `CASCADE_RELATIONS`, unified with mode-C travel's shared traversal | **PR open** (2026-08-28) — see the history note above this table for the #2255-superseding ruling | issue #2298, PR #2329 |

**Note on 1.3's scope, corrected from an earlier draft of this doc:** 1.3 is **not** "the
confidence rung" — `lesson`'s confidence removal already shipped via 1.6/1.6a, ahead of and
separately from 1.3. 1.3's remaining scope per the epic's W2 workstream was the other
enforcement pieces: a general NCB write-path confidence guard, and stopping `supported_by`
carrying cascade. **This pass verified `_refuse_ncb_confidence`'s one call site (derivation
creation) directly in source; it did not independently re-verify every NCB write path carries
an equivalent guard, or re-derive the full scope of what 1.3 shipped beyond the two items this
issue named** (`supported_by`'s cascade flag, the derived cascade-walk skip) — flagged as
unchecked rather than asserted complete, per this doc's own discipline. The `trust_signal` and
`prediction` cascade tensions flagged above are both resolved by the derived skip mechanism
(confirmed directly in `_walk_cascade_downstream`'s source, above).

This doc gated 1.3 while it was open: its enforcement was correct exactly to the extent it
matched the membership, confidence rule, and edge partition stated above. Now that 1.3 and W6
have both merged, this doc's job flips — it describes the shipped state (as of the commit named
in the header) rather than gating work still in flight. If a future reader finds the doc and
`EDGE_CLASSIFICATIONS`/`CLAIM_BEARING_TYPES` disagree, the same rule still applies: the
divergence is a doc bug to fix here, not a silent implementation choice to infer from.
