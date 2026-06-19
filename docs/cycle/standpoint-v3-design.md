# Standpoint v3 — training-lineage axis + per-axis F-S integration

**Phase-1 design doc** (charter §3.1). Author: Ariadne (driver seat). Status: DRAFT for
two-axis review (driver cross-review + applied-evidence seat). Implementation only after
the design survives both reviewer axes — per the charter, this document is the gate.

Inputs bound: my #718 → #765/#776 → #817 lineage; Luria's dv_0059 (per-type F-S/confidence
structure), dv_0110 (Class 1/2 re-executable vs frozen), dv_0088 (v3 implementation
prerequisites), dv_0055 (lineage = #1 bias axis); thread #43's D1 spec yield;
trust-dynamics.md hook 2 (the tier ⊥ diversity boundary).

---

## 1. Where v3 sits in the lineage

| Version | Surface | Status |
|---|---|---|
| v1 (#718, PR #756) | `standpoint_author_id` / `standpoint_collection_id` / `standpoint_override_tag` on observations; combined-key uniformity warning at derive time | PR open, review-converged, unmerged |
| v2 (#765, PR #776) | per-axis cluster keys (`_standpoint_cluster_key`), per-axis diversity reporting, all-or-omit partial-coverage guard, positive-liveness ("others unchecked") | PR open, review-converged, unmerged |
| **v3 (#817, this doc)** | **lineage axis + per-axis F-S integration** | design |

Hard prerequisite (Luria's dv_0088, prerequisite 1): the live DB has **no standpoint columns
yet** — v1/v2 are unmerged. v3 implementation stacks on the v2 branch (or on dev after
v1+v2 land); it does not re-implement them. This doc's contracts are written against v2's
actual code surface (`_standpoint_cluster_key` + the `n >= 2` advisory block in
`_validate_reasoning_structure`, as on `fix/765-standpoint-per-axis`).

## 2. Problem statement

v2's axes are **agent-identity** axes. When three agents corroborate a derivation, v2
reports `author: 3 clusters (diverse) ✓` — but if all three share a training lineage, the
premise set is uniform on the axis that matters most for AI-agent bias and v2 cannot see
it. Luria's dv_0055 ranks training lineage the #1 bias axis for AI agents — above
collection method, temporal standpoint, methodology, platform. ob_0095 named the
blindness: *"v2 hashes AGENT IDENTITY, not MODEL LINEAGE."*

The multi-agent reality this cycle runs in is the worst case for that blindness: four
active agents, all `anthropic:opus`. Every cross-agent corroboration we file today is
lineage-uniform, and nothing in the graph says so.

The second gap is subtler (thread #43, D1): diversity tells you **who could be jointly
biased**; it does not tell you **whether reality can still veto any of it**. A premise set
can be perfectly lineage-diverse and consist entirely of frozen records nothing can
re-test. Diversity and falsifiability are different multipliers on corroboration value,
and v2 reports only the first.

## 3. Axis 1 — training lineage (first-class)

### 3.1 Schema (additive, no migration)

```sql
ALTER TABLE nodes ADD COLUMN standpoint_lineage TEXT;
-- format: "provider:family", e.g. "anthropic:opus"
-- NULL = not set; existing rows stay NULL (backward-compatible)
```

Matches the v1 pattern (`standpoint_author_id`, `standpoint_collection_id`). Granularity
convention: **provider:family**, not version. Rationale: the bias axis is
training-prior-level; sibling versions/tiers of one family share priors, and the binary
diversity formula (§3.3) makes coarser granularity the conservative choice — it can only
under-report diversity, never fake it. Version detail, if ever needed, goes in
`standpoint_override_tag` (which remains the annotation-only entry point for
platform/env/locale, per the v2 comment).

### 3.2 Cluster key + reporting

`_standpoint_cluster_key` adds the third axis:

```python
"lineage": sha256(lineage)[:12] if lineage else None,
```

Reporting extends the v2 advisory block unchanged in structure: lineage participates in
the same all-or-omit guard (any premise NULL on the axis → axis omitted, never asserted
from partial data) and the same positive-liveness footer. One deliberate asymmetry: the
**uniform** verdict on lineage carries a stronger warning than author/collection —

```
lineage: 1 cluster (⚠ uniform — shared training lineage; zero independent
corroboration on substrate-prior bias)
```

— because the bias implication is qualitatively different: same lineage means shared
training-era priors, not merely the same data source. (#817 §C.)

**Actionability gate on the standalone lineage ⚠** (Aleph's applied-seat finding): in a
single-lineage graph, every multi-premise derivation is lineage-uniform *by construction*
— the sole (or same-lineage) filer authors every premise, and the warning's lineage-remedy
("add cross-lineage support") is structurally unavailable: peers' nodes are cross-graph,
pack nodes are premise-firewalled (cite-never-import). A ⚠ that fires on 100% of filings
with no available action is alarm-fatigue by design, and its collateral cost is the whole
STANDPOINT line getting ignored. So the ⚠ renders **only when the graph itself
demonstrates ≥ 2 distinct non-null lineage values** among claim-bearing nodes; otherwise
the axis renders informationally — `lineage: uniform (single-lineage graph — informational;
file an observation from a cross-lineage source to make this axis live)` — never as ⚠; per
§7.3 the informational form still teaches the path to enabling the axis. Detection is
**graph-state-based** (`COUNT(DISTINCT standpoint_lineage)` over non-null claim-bearing
rows), deliberately the *single* detector — not composed with a `config.json mode`
primary — for two reasons: (a) Option A carries no person lineage metadata, so person-node
or config-declared detection diverges from what the warning's actionability actually
depends on; (b) install topology ≠ graph lineage state in both directions — a solo install
that files observations sourced from cross-lineage agents HAS citable cross-lineage
premises (the ⚠ is earned), and a multi-agent monoculture house doesn't (informational is
correct there too). When declaration and graph state disagree, graph state is right; the
gate reads evidence, not topology. Self-adjusting: the first recorded cross-lineage claim
flips the axis live. The ⚠⚠
composite (§4.1) still fires in single-lineage graphs — its Class-1 remedy path stays
actionable — but there its text leads with the Class-1 remedy and drops the structurally
unavailable cross-lineage clause.

### 3.3 Diversity formula — binary

`lineage_diverse(premises) = 1` iff any two supporting premises carry **different
non-null** lineage hashes. N same-lineage agents ≈ one witness on this axis, for any N —
so a continuous 0..1 score over the supporting set would manufacture precision the axis
doesn't have. Binary is the honest shape (#817, Luria's dv_0084-class reasoning).

### 3.4 Population protocol — phased A-then-B

dv_0088 (prerequisite 2): `engram_add_person` has no lineage fields and no
`engram_update_person` exists; Option B (auto-stamp from person node) requires a new tool.
qu_0004 (Option A vs B) is the maintainer's open call. The design works under either; the
phasing below keeps Phase 1 inside the minimal-tooling envelope without foreclosing B:

- **v3-core = Option A** (explicit `standpoint_lineage` param on `engram_add_observation`,
  same discipline pattern as the existing author/collection params). No new tools, lands
  with the column. Format-validated: `^[a-z0-9_-]+:[a-z0-9._-]+$` — reject malformed,
  never silently normalize.
- **v3.1 follow-up = Option B** (auto-stamp): person nodes gain
  `{model_provider, model_family}` metadata via a new `engram_update_person`; filing-time
  lookup auto-stamps lineage when `standpoint_author_id` resolves to a person with
  metadata; explicit param overrides auto-stamp. This is the ergonomically correct
  end-state (the failure mode of A is per-observation forgetting; B makes the discipline
  once-per-session) — but it adds a substrate tool, so it ships as its own
  reviewed artifact after the maintainer's qu_0004 call, not inside Phase 1.

The format rejection **redirects** rather than bare-rejects (charter §7.3): the error
names the expected shape and gives an example —
`standpoint_lineage must match provider:family (e.g. "anthropic:opus"); got "Claude"`.

**Option-B design-time watch-item** (Aleph, #41/591, on record before B is built): under
auto-stamp, a solo install's every multi-premise derivation would carry the informational
single-lineage line — below the ⚠ alarm-fatigue threshold but repetitive, and nudging
toward cross-lineage sourcing a purely-solo filer may never do. B's design must decide
keep-per-derivation vs once-per-session for the informational rendering.

Self-report integrity: lineage is self-reported from the session system context (model ID
is injected every session; `claude-opus-4-8` → `anthropic:opus`). A false self-report is
not detectable in-graph — that is an attestation problem, which belongs to the trust-tier
follow-up and is deliberately out of v3 scope (§5, the ⊥ boundary). v3's claim is
"the graph records the declared lineage and surfaces uniformity"; it does not claim
lineage verification.

## 4. Axis 2 — per-axis F-S integration

### 4.1 The design insight

The STANDPOINT warning answers *"who could be jointly biased?"* The F-S inputs (dv_0059,
dv_0110) answer a different question: *"can reality still say no to any of this?"* These
compose rather than merge:

- A lineage-diverse premise set of **Class-2 frozen records** (dv_0110: one-time past
  events, quote-checkable only) is diverse, but no premise can be re-tested — the
  diversity verdict is about as good as it will ever get, and nothing can improve it.
- A lineage-**uniform** set with even one **Class-1 re-executable** premise has a live
  channel to reality — re-execution can break the joint bias that uniformity warns about.

So F-S is not a fourth cluster axis (it isn't a *standpoint* — it's a property of the
evidence, not the observer). It enters as a **qualifier line** on the same advisory
surface, **gated on the same condition as the STANDPOINT report** (all premises carry
standpoint data): the qualifier scopes to the surface it qualifies — exactly §6's
zone-of-validity ("derivations whose premises carry standpoint fields") — and graphs
that never file standpoint fields stay warning-free. Without this gate the proxy line
would fire on every multi-premise derivation in every graph (quote_type always exists),
which is the same alarm-fatigue class as the single-lineage ⚠. *(Implementation finding,
fold round 2: the pre-existing clean-derivation test caught the ungated version.)*

```
STANDPOINT: author: 3 clusters (diverse); collection: 1 cluster (⚠ uniform);
lineage: 1 cluster (⚠ uniform — …); others unchecked.
FALSIFICATION: 2/3 premises re-executable (reality retains veto);
quote_type-proxy for 1/3 (F-S field unset).
```

The example above is the **Phase-2 steady state** (mixed field/proxy sources). In Phase 1,
where every source is the proxy, the surfaced text never drops the proxy label — it reads
`2/3 re-executable-leaning (proxy:quote_type); 1/3 frozen-leaning (proxy:quote_type)`.
**Proxy-label invariant**: whenever `source == "proxy:quote_type"`, the surfaced text
carries the label; no premise is ever presented as bare "re-executable" from proxy data.
(Pinned in the test plan, §7 test 8.)

Worst case gets its own escalation: lineage-uniform AND zero re-executable premises →

```
⚠⚠ frozen + uniform: no premise is re-testable and all share training lineage —
corroboration on this derivation cannot be improved by re-checking; treat as
single-witness until a Class-1 premise or cross-lineage support is added.
```

That line is the practical payoff of the whole integration: it converts two abstract
warnings into one actionable filing instruction.

**Escalation proxy policy** (the ⚠⚠ predicate is the strongest instruction on the surface,
so its input honesty is specified, both directions):

- The predicate counts only premises with **known** `fs_class` — unknowns neither satisfy
  nor defeat "zero re-executable." An unknown-dominated premise set gets the plain
  FALSIFICATION line, never the ⚠⚠ (no true-by-ignorance escalation; an over-firing alarm
  trains itself ignored).
- When the composite fires on any proxy-sourced input, the escalation line itself carries
  the label: `…treat as single-witness [F-S via quote_type-proxy — Phase-2 field may
  revise]`. The payoff line stays, and stays honest at both phases — the same
  never-present-a-default-as-a-measurement rule §4.2 applies to the per-premise ratio.

### 4.2 The Phase-1/Phase-2 seam (contract, not field)

F-S as an independent **field** is Phase-2 scope, hard-gated by D1 (Class-1/2 as primary
determinant; quote_type demoted to default/prior; re-testable vs quote-checkable as
override rubric). Phase 1 must not implement the field. The integration is therefore
specced as a **contract**:

```python
def _node_fs_class(conn, node_id) -> tuple[str, str]:
    """Returns (fs_class, source).
    fs_class: "re-executable" | "frozen" | "unknown"
    source:   "field" (Phase-2 real field) | "proxy:quote_type" (Phase-1 default)
    """
```

- **Phase-1 implementation**: derive from `quote_type` per the D1 prior (hard_data →
  re-executable-leaning, editorial/personal_communication → frozen-leaning), always
  labeled `proxy:quote_type` in the surfaced line — the proxy is *visibly* a proxy
  (never-self-reported discipline applied to the warning text itself: don't present a
  default as a measurement). The proxy is computed **at read time and never persisted** —
  no F-S value is written to any row, so Phase 2's real field cannot collide with stale
  proxy data and needs no migration to overwrite guesses. This is the property that makes
  the accessor an honest D1 citizen rather than a backdoor early implementation: Phase 1
  stores nothing it would have to retract.
- **Phase-2 swap**: the accessor's internals read the real F-S field; **no caller
  changes**. The FALSIFICATION line automatically upgrades from proxy to field-sourced.

This respects D1 (the proxy is explicitly a prior, overridable by the Phase-2 field) and
keeps the phases decoupled: Phase 1 ships complete and useful without Phase 2; Phase 2
improves precision without touching Phase 1's callers.

## 5. The charter boundary (trust-dynamics.md hook 2 — binding)

1. **Trust-tier ⊥ standpoint-diversity.** Tier derives from attestation/evidence about a
   counterparty; diversity raises the corroboration value of a premise set. v3 never
   weights diversity by tier and never feeds diversity into tier. The failure mode this
   forbids is concrete: a low-tier counterparty from a *different* lineage is the most
   valuable corroborator on the lineage axis; tier-weighting diversity would zero out
   exactly the premises that break uniformity.
2. **Lineage ≠ collection-method.** `anthropic:opus` (training priors) and
   "web-scrape vs API vs human-interview" (collection) are separate sub-axes with separate
   cluster keys — never merged into one hash, never reported as one verdict.

## 6. Safety-envelope compliance (charter §4)

- **Advisory-only, like v1/v2**: warnings are returned text; they never block creation and
  **never touch computed confidence**. No flag needed beyond what v1/v2 carry — but if the
  reviewers prefer, the lineage axis + FALSIFICATION line can sit behind the cycle's
  off-by-default flag at zero design cost (one `if` at the reporting layer).
- **Schema-additive**, NULL-tolerant, no migration of existing rows.
- **Frozen metric untouched**: `_compute_health_score` reads no standpoint surface.
- **Full-agent implementation** — substrate-semantic (derive-time validation); no fairy
  delegation, per the envelope.
- **Zone of validity** (for the applied seat, §4.8/§7.5): claimed valid for *filing-time
  advisory reporting on derivations whose premises carry standpoint fields*. NOT claimed:
  retroactive re-scoring of existing derivations; any confidence mutation; lineage
  verification (§3.4); cross-graph/pack premise handling beyond what v2 already does
  (pack-imported premises carry whatever standpoint their author filed — that is the
  correct behavior, and the cite-never-import discipline keeps their provenance intact).

## 7. Test plan (D3 dual-probe shape — both directions per probe)

Known-good controls (the direction nobody writes — written first):
1. Two premises, different lineages → `lineage: 2 clusters (diverse)`, no escalation.
2. Mixed Class-1/Class-2 premises → FALSIFICATION line reports the true ratio, no ⚠⚠.

Known-bad probes:
3. All premises same lineage → uniform warning fires with the strengthened text.
4. One premise NULL lineage → axis **omitted** (all-or-omit guard holds for the new axis).
5. Author-diverse + lineage-uniform → lineage warning fires independently of author
   verdict (the exact v2 blindness, pinned as a regression test).
6. Lineage-uniform + all-frozen (all known fs_class) → the ⚠⚠ composite escalation fires;
   when any input is proxy-sourced, the fired line carries the proxy label.
7. Malformed lineage string at filing → rejected with redirecting format error
   (expected shape + example in the text), row unwritten.
8. **Proxy-label invariant** (Luria's pin): with all sources `proxy:quote_type`, every
   premise in the FALSIFICATION line carries the proxy label — no bare "re-executable"
   from proxy data, in the ratio line or the ⚠⚠ line.
9. **Unknown-policy probe** (both directions): unknown-dominated premise set →
   plain FALSIFICATION line, no ⚠⚠ (known-bad direction: assert the escalation did NOT
   fire); the same set with fs_class made known-frozen → ⚠⚠ fires (control direction:
   the suppression is the unknowns, not a dead code path).
10. **Single-lineage actionability gate** (Aleph's pin, both directions): graph with one
    distinct lineage → axis renders informational, never ⚠ (and the ⚠⚠ text, when it
    fires, leads with the Class-1 remedy); same graph after one cross-lineage claim is
    filed → the ⚠ becomes live (control: the gate reads graph state, not a config flag).

Parity: full v2 standpoint suite passes unchanged (additive property itself pinned).

## 8. Open questions (named, not hidden)

- **qu_0004 (Option A vs B)** — maintainer's call; phasing in §3.4 works under either
  answer and forecloses neither.
- **Granularity drift** — provider:family is a filing convention, hash-enforced only as
  string equality. If two agents file `anthropic:opus` vs `anthropic:opus-4-8`, the
  axis reports false diversity. Mitigation: the format validator + a SKILL.md convention
  table; structural enforcement would need a registry, which is over-engineering at
  current scale (4 agents, 1 lineage). Revisit when a second provider actually appears.
- **Derivation-chain lineage** — v3 clusters over direct premises only (v2 behavior). A
  derivation citing derivations inherits no transitive lineage analysis. Deliberate:
  transitive standpoint propagation is a v4-class question and the cluster math changes
  shape (sets-of-sets); noted for the lineage issue, out of v3 scope.

---

*Review topology per charter §7: driver cross-review (gate placement, correctness) +
applied-evidence seat (does the warning surface help or annoy at real filing time —
exactly the gates-that-redirect bar from §7.3). Implementation opens only after both.*
