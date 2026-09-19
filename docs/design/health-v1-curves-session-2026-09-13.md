# Curves session — 2026-09-13 (Sun), Lei + Kepler

## 0. Review summary for Lei (read this first)

**Decided in-session (yours):**

| # | Metric | Decision | Curve / knots |
|---|---|---|---|
| M1 | taint_rate + stale_derivation_rate → **pending_premise_rate** (one metric) | scored, importance 1 (sole admitted metric of epistemic_integrity, 40 pts) | badness (0,1)(0.01,0.9)(0.05,0.8)(0.2,0.5)(0.5,0.2)(1,0) |
| M2 | open_contradiction_rate | **HOLD-OUT**, informational (importance 0) | — (subsystem renovation first) |
| M3 | embedding_coverage | scored, importance 1 (reference metric) | goodness (0,0)(0.5,0.05)(0.75,0.2)(0.95,0.6)(0.99,0.9)(1,1) |

**Proposed by Kepler from the history (yours to accept/adjust):**

| # | Metric | Proposal |
|---|---|---|
| M4 | recall_summary_coverage | same curve as M3 (`goodness_coverage`), importance **0.5** |
| M5 | recall_keywords_coverage | same curve as M3, importance **0.25** (tie-break: its missing set ≈ M4's, co-written fields — equal weight double-counts one backlog) |

**Three calls for you:** (P3) importance 1.0/0.5/0.25 vs 1.0/0.5/0.5; (P4) one shared curve id `goodness_coverage` for M3–M5 vs three named copies (recommend shared); (P2) confirm M1 carrying 40/40 of epistemic_integrity is intended (M2 held out).

**Draft data file:** `~/.engram/notes/curves-v1-proposal.json` (knots + provenance + proposed bindings). After your word: one data-file-only PR (curves.json + the M1 unification / M2 importance / M4–M5 importance in model.py — the unification is a small code change, not data-only; note it for the cut), health 1.1 → 1.2, then the v0.5.1 cut.

**Live numbers behind M4/M5** (health-v1.1 series, 09-13): summary/keywords coverage kepler 1.000, sol 0.992, borges 0.985, ariadne 0.943 (flat for 30 points = backlog not draining); embedding 1.000 wherever numeric. Under the shared curve the scent scores read 1.00 / 0.96 / 0.86 / 0.59.

---

Lei thinks aloud; Kepler takes notes. Goal: health v1 curves pinned (v0.5.1 gate) + method insights + next-version metric inspirations.
Workbench: http://localhost:5021 (spt, dev tip 9645f64c). Current curves.json = two placeholders (badness 1−x, goodness identity; Lei 2026-09-09).

## 1. Curves decided (metric → knots, direction, rationale)

### M1 — pending-premise rate (UNIFIES taint_rate + stale_derivation_rate) — proposed by Lei 14:41Z, Kepler agrees w/ refinements
- **Signal**: `claim_bearing_pending_marker / claim_bearing_current` — numerator = DISTINCT current claim-bearing nodes carrying ANY pending cascade marker (root_op retract OR supersede; a node with both counts once); denominator = all current claim-bearing nodes (the premise-eligible population).
- **Meaning** (Lei): the ratio of premises currently NOT available for derivation — a pending marker blocks the node as a premise regardless of cause. Observations never carry markers (no cascade in-edges), but they belong in the denominator because they are premises.
- **Why one metric**: post-cascade-machine, retract-taint and supersede-stale DO the same thing to daily thinking. Two real causes of accumulation, both need early detection: (A) no sleep for a long time; (B) nodes the dream master cannot resolve alone → user guidance / investigation.
- **Curve** (badness, decreasing, PWL): `(0,1) (0.01,0.9) (0.05,0.8) (0.2,0.5) (0.5,0.2) (1,0)` — steep first 1% to show the first anomaly; (0.5,0.2) anchors "half the premises pending ≈ derivation effectively blocked".
- **Data status** (corrected 16:4xZ): kepler / sol / ariadne histories read 0; **borges's seat has a REAL event** — `derivations_pending_supersede_marker` 131 on 2026-08-30 (131/5193 claim-bearing → unified ratio 0.025 → score ≈0.86; derivations-only 131/1198 = 0.109 → ≈0.68), dropping to 14 the same day (no-sleep backlog draining once a cycle ran) and holding at 14 for six days / 33 points before clearing on 09-05 (narrative B: nodes the dream master could not resolve alone). So a calibration event already exists; the knots stay intuition for v1 and get re-read against this event (and a cross-seat extraction) in v2. Marker history is only ~14 days deep (cascade_markers table exists since ~08-30; earlier rows unreplayable). It is an EARLY-WARNING metric, not a differentiator between healthy graphs.
- **Today's code** (origin/dev signals.py): taint_rate = claim-bearing w/ pending RETRACT marker / claim-bearing current; stale_derivation_rate = derivations w/ pending SUPERSEDE marker / current derivations. Unification = one numerator over both root_ops, one denominator.
- **Refinements Kepler raised**: (1) denominator semantics — on an observation-heavy graph 5% overall ≈ a much larger share of derivations blocked; knots are read against the unified ratio, state that in provenance. (2) Aspect points: epistemic_integrity goes 3→2 metrics; with uniform importance the pending metric carries 20 of 40 points instead of 13.3 — confirm that is intended. (3) Contested premises (open contradiction, MECH-5 opt-in override) are a SOFT block and stay in open_contradiction_rate; pending markers are a HARD block — the boundary keeps the two metrics distinct. (4) support_lost on cornerstones/lessons excluded — not premises. (5) **Reachable range** (Borges): under the unified denominator the (0.5,0.2) anchor requires half of ALL claim-bearing nodes to be marked, but only derivations/theories/conjectures can carry markers — on borges's graph 1283 derivations of 5953 nodes, so ratios above ~0.22 are unreachable by the mechanism. The provenance line must name the denominator and the reachable range; Lei's meaning stands.


### M2 — open_contradiction_rate → HOLD-OUT, informational only (Lei 14:57Z)
- **Decision**: not in the health score for v1 (importance 0 / diagnostic, like the provenance_integrity pair). Reason: `contradictions_open / contradictions_current` measures a resolution HABIT (backlog share), not what unresolved contradictions DO to daily engram activity; and the contradiction subsystem is old and needs dedicated design before a score depends on it.
- **How contradictions affect the graph today (read from origin/dev engram_core.py)**: `engram_contradict` mints a ct_ node with status `active`; `engram_resolve` wires a `resolves` edge and flips status to `resolved` (resolver conf ≥ 0.70) or `partially_resolved`; the ct_ node STAYS is_current=1 after resolution (hence the odd denominator). Effect on derivation: `_validate_premises` (MECH-5, #1654) SOFT-blocks a derive whose direct premise sits on the open side of a live ct_ (`BLOCKED_CONTRADICTED`), override `use_contested=True` stamps `metadata.built_on_contested`. NO marker or flag cascades to descendants — "a contradiction is a dispute under adjudication, not a cascade consequence" (commander ruling 2026-08-28). No read-time warning on query/inspect. Both sides are treated symmetrically (neither is declared invalid).
- **Consequence for v1**: epistemic_integrity keeps ONE admitted metric (M1, 40 points); aspect hold-out/renormalization already exists for the zero-admitted case, so the model does not need a new mechanism.

### M3 — embedding_coverage (retrievability, reference metric) — Lei 15:14Z, Kepler agrees
- **Signal (origin/dev)**: `one_minus_rate` = 1 − (claim-bearing current with NULL embedding / claim-bearing current); gated UNAVAILABLE when the embedder is structurally absent (`embedder_installed_available`, `embedder_config_enabled`). Embeddings are written AT node-write time (engram_core writes nodes.embedding + vec together), so NULLs mean the embedder was down/disabled when nodes were written, or a restore path — not routine write lag. My seat now: 1855 claim-bearing, 0 NULL, coverage 1.0.
- **Why history is short** (Lei asked): NOT the embedder gate — the git-replay backfill reads knowledge.sql dumps where embedding blobs are STRIPPED (`stripped_from_dump`, measurements.py), so coverage is unreplayable for history; only live snapshots since the health-v1.0 deploy (~09-05/06) carry it.
- **Meaning** (Lei): directly affects RECALL — how engram becomes useful daily — so more important than pending markers (which bite only on DERIVE). 100% is the norm; below 100% is anomaly.
- **Curve** (goodness, increasing, PWL, lower-convex): `(0,0) (0.5,0.05) (0.75,0.2) (0.95,0.6) (0.99,0.9) (1,1)` — (0.5,0.05): below 50% = embedding mechanism broken, needs immediate attention, how far below is irrelevant; (0.99,0.9): sensitivity to the first small loss + a large score boost for a small recovery near full → backfill is REWARDED; middle knots smooth to lower-convex.
- **Kepler's notes**: (1) because embeddings are written at node-write time, the 0.99 knot detects real outages, not write-burst noise — the curve will not flap day to day. (2) Reference metric of retrievability (importance anchored 1); with 3 admitted metrics at uniform importance, 1% coverage loss ≈ 10 metric points ≈ 1.2 composed points. (3) UNAVAILABLE (no embedder) is a separate state from 0% coverage — the gate already keeps them distinct; keep it so.

### M4/M5 — recall_summary_coverage + recall_keywords_coverage — KEPLER'S PROPOSAL (Lei left 15:19Z: "same shape/idea as embedding, maybe the same curve; importance less than embedding")
- **Signals (origin/dev)**: `1 − without_summary / claim_bearing_current` and `1 − without_keywords / claim_bearing_current`. Written at node-write time by the author since #1952 (08-20); legacy nodes backfilled by the nightly batch-summary fairy (15 nodes per dispatch). Both are recall-SCENT: a node without them is still recalled (FTS on claim + embedding) but renders its full claim (token cost) and ranks on less — a quality/cost degradation, NOT an availability loss like embeddings.
- **History (health-v1.1 series via :5021, 09-13 15:2xZ; mira/zephyr have no v1.1 rows)**:

  | seat | summary last (missing/den) | last-30 band | keywords last (missing/den) | embedding last |
  |---|---|---|---|---|
  | kepler | 1.000 (0/1800) | 0.999–1.000 | 1.000 (0/1800) | 1.000 |
  | sol | 0.9915 (12/1406) | 0.991–1.000 | 0.9915 (12/1406) | 1.000 |
  | borges | 0.9854 (87/5953) | 0.982–0.985 | 0.9842 (94/5953) | (unreplayable) |
  | ariadne | 0.9427 (204/3560) | 0.931–0.946 | 0.9463 (191/3560) | 1.000 |

  Pre-08-20 history reads 0.0 on every seat (the feature did not exist) — replayed, not unhealthy.
- **Two facts the numbers add**: (a) the two signals are nearly the SAME signal — the missing sets almost coincide (borges 87 vs 94, ariadne 204 vs 191, sol 12 vs 12, kepler 0 vs 0) because both are written together; two metrics at equal importance would DOUBLE-COUNT one backlog. (b) The steady state on large graphs is ~0.98, not 1.0, and ariadne's 0.943 has been flat for 30 points — a legacy backlog the nightly fairy is not draining. That is a real, actionable signal (run the backfill), exactly what a curve steep near the top should show.
- **Recommendation (Kepler)**:
  1. **Same curve as embedding** (`goodness_coverage`: (0,0)(0.5,0.05)(0.75,0.2)(0.95,0.6)(0.99,0.9)(1,1)) for both — one curve to maintain, and the top-end steepness is what makes a stuck backlog visible. Scores today: kepler 1.00, sol 0.96, borges 0.86, ariadne 0.59 — the 0.59 is harsh for a 5.7% gap but it is a gap that is not moving, which is the point.
  2. **Importance: embedding 1.0, summary 0.5, keywords 0.25.** Summary is the Tier-1 recall scent (rendered for top-k), keywords Tier-2 (cues for the rest) and both are FTS-indexed; 0.25 on keywords is the tie-break for the double-count in (a). Aspect share of 35 points: embedding 20, summary 10, keywords 5. Alternative if Lei prefers symmetry: 1.0 / 0.5 / 0.5 (embedding 17.5, summary 8.75, keywords 8.75) — accepts the double-count knowingly.
  3. **Next version (N5)**: merge summary + keywords into one `recall_scent_coverage` (nodes carrying BOTH) so the backlog is counted once; then importance embedding 1.0 / scent 0.5 with nothing to tie-break.
- **giant_component_fraction** stays importance 0 (diagnostic) — unchanged.

## 2. Method insights — how to set curves for future metrics / calibrations

- **I1 (Lei, M1)**: define the metric by what the measured condition DOES to daily engram activity (here: blocks a premise), not by its mechanism of origin; if two mechanisms do the same thing, unify.
- **I2 (Lei, M1)**: for an early-warning metric whose healthy history is all-zero, shape the curve by the two failure narratives it must detect (no-sleep accumulation; unresolvable nodes) — steep near zero to show first onset, a named anchor knot where the condition becomes disabling, then flat toward 1.
- **I3 (Lei, M1)**: with no real distribution, pin intuitive knots now, record them as intuition in provenance, and calibrate from real accumulation events later — never wait for data that a working sweep prevents from existing.
- **I5 (Lei, M2)**: a signal whose definition does not name an EFFECT on daily engram activity is not ready to be scored; hold it out as informational rather than curve it — a curve on an abstract ratio scores a habit, not health.
- **I6 (Lei, M2)**: when a metric family needs subsystem renovation, split by consequence: one metric for process efficiency (resolution), one for epistemic consequence (what stays contested and what was built on it).
- **I7 (Lei, M3)**: rank metrics by how directly they touch daily use (recall > derive), and let that ranking justify curve steepness near the norm.
- **I8 (Lei, M3)**: for a 100%-is-normal metric, shape the curve so REPAIR is rewarded — a small recovery near full earns a large boost — and the far tail is flat because "how bad below the cliff" carries no decision value.
- **I9 (Kepler, M3)**: before setting a knot near the norm, check the write path — a knot at 0.99 is only meaningful if routine operation cannot produce 1% transient loss.
- **I10 (Kepler, M4/M5)**: before giving two metrics separate importance, check whether their missing sets coincide — co-written fields are one backlog and two metrics double-count it; either merge them or weight the second as a tie-break.
- **I11 (Kepler, M4/M5)**: distinguish AVAILABILITY loss (embedding: node unrecallable by the semantic path) from QUALITY/COST degradation (scent: still recalled, renders worse) — same curve shape is fine, the difference goes into importance, not into the knots.
- **I12 (Kepler)**: a flat sub-1.0 band over 30 points is a stuck backlog, not noise; the curve's top-end steepness should make "stuck" and "draining" look different on the panel.
- **I4 (Kepler, M1)**: choose the denominator from the metric's MEANING (population the condition acts on), then re-read the knots against that denominator — the same knots mean different things over different populations.


## 3. Next-version metric inspirations (Lei's new ideas, verbatim where possible)

- **N1 (Lei, 14:57Z) — two contradiction metrics** replacing open/current: (a) *"contradiction resolution efficiency"* — how fast/well contradictions get resolved (e.g. age of open contradictions, resolution latency, not a backlog share); (b) *"the real epistemic consequence of having those contradictions unresolved"* — e.g. share of premises currently contested (soft-blocked), count of derivations stamped `built_on_contested`, descendants of contested nodes.
- **N2 (Lei, 14:57Z)** — *"I would assume when a contradiction is resolved, it no longer needs to be current"*: lifecycle change — a resolved ct_ node should leave the current population (or the denominator must be redefined), so the ratio's denominator stops counting history.
- **N3 (Kepler, M1)** — support_lost markers on cornerstones/lessons (not premises, excluded from M1) may deserve their own signal: "principles whose evidence is gone".
- **N4 (Lei, 14:57Z, open design)** — contradictions do not say WHICH side is invalid; blocking both sides and their offspring is a strong action. Whether/what to cascade from a contradiction (a soft "contested" flag on descendants? a read-time warning?) is the renovation question.


## 3b. Next-version inspirations added by Kepler
- **N8** — first M1 calibration event: borges 08-30→09-05 (131 → 14 plateau → 0). v2 task: extract the same series on every seat, re-read the M1 knots against real events, and consider whether the anchor knots should be stated in derivation-share terms.
- **N5** — merge recall_summary_coverage + recall_keywords_coverage into one `recall_scent_coverage` (both fields present) — see M4/M5 (a).
- **N6** — a version-aware "not applicable" state for replayed history before a feature existed (scent coverage reads 0.0 before 08-20 on every seat); the panel should render "feature absent" rather than a zero score for that era.
- **N7** — a "backlog draining?" derivative: slope of scent coverage over the last N points; flat-and-below-1 = the nightly fairy is not keeping up (ariadne today).

## 4. Open questions / parked

- **P1** — Contradiction subsystem renovation (Lei: "quite old, needs dedicated design"): lifecycle (resolved ⇒ non-current?), downstream effect (cascade flag vs soft block only), read-time warnings, the two metrics in N1. Owner TBD; not before v0.5.1.
- **P3** — Importance for M4/M5: 1.0/0.5/0.25 (recommended) vs 1.0/0.5/0.5 — Lei's call.
- **P4** — Does Lei want `goodness_coverage` as ONE shared curve id for M3/M4/M5, or three named copies? (Recommend one shared id — a single knot edit moves all three, and the data file says so in provenance.)
- **P2** — M1 aspect-points consequence (20 of 40 → now 40 of 40 with M2 held out): confirm intended.


## 5. Lei's charge for the NEXT health version (12:28 EDT 09-13, verbatim)

> "For the next version, I would like to try thinking from both directions: on the measurements side, we can think what meaningful graph measurement we can compute given the current graph; and on the score side, we can think what key engram aspects and performance-critical processes we haven't captured in our current system. And I hope they will meet and provide us some good leads on what to build next into our health system."

**Method (as charged)**: two independent lists — (A) *computable measurements* over the current graph schema (bottom-up, from the tables/edges/markers we have); (B) *uncaptured aspects & performance-critical processes* of daily engram life (top-down: recall, derive, consolidate, contradiction, provenance, sleep/dream, lesson firing…). Then intersect: a candidate metric is a (B)-need with an (A)-measurement. Owner/lane: design seats (Borges + Ariadne + Kepler), after v0.5.1 ships — not a build-seat task.

**Build status**: approved by Lei 12:28 EDT; filed as engram-alpha#2905 (Luria builds; PR-A MINOR v1.2, PR-B MAJOR v2.0 — the unification adds an L0 measurement, so it is MAJOR by health/version.py's rule, not minor as first assumed). This doc ships in PR-A under docs/design/.

