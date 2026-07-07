# Unified Principle Triggers — Design (issue #1698)

**Author:** Kepler · **Date:** 2026-07-07 · **Status:** design, ready for
implementation review
**Lineage:** #931 (original proposal + Aleph's decay addendum, 2026-06-10) →
recall-triggering blueprint §3-P3 (#1688) → PR #1695 (cornerstone anchor
channel, the second concrete instance) → this unification.

## 1. What exists after #1695 (the two instances to unify)

| | Lessons | Cornerstones (new, #1695) |
|---|---|---|
| Cache file | `error_incidents.json` | `cornerstone_anchors.json` |
| Rebuild sites | `_rebuild_incidents_cache` (register_exemplar, add_lesson, generic edge tools) | `_rebuild_cornerstone_anchors_cache` (same three sites) |
| Prompt-time check | `check_incident_tripwire` | `check_cornerstone_anchor` |
| Action-time check | `situation_pattern` regex at PreToolUse | same hook, same field (#1695) |
| Habituation guard | none (per-fire lesson dedup only) | fixed 10-prompt cooldown |
| Injection register | corrective — "Action: …" | orienting — anchor line |

Two parallel caches, two loaders, two checks, two inconsistent habituation
policies. A third instance (axioms) would make it three. Unify now.

## 2. The unified registry: `principle_triggers.json`

One cache file, one shape, four kinds:

```json
{
  "<trigger_node_id>": {
    "principle_id": "<cornerstone_id>",
    "kind": "cornerstone",            // lesson | cornerstone | axiom | goal
    "claim": "<principle claim>",
    "nudge": "<kind-styled injection text>",
    "situation_pattern": "<optional regex, from principle metadata>"
  }
}
```

**Trigger surfaces** (the concrete half — all four already exist as edges):

| kind | trigger nodes reached via | nudge source (fallback → claim) | register |
|---|---|---|---|
| lesson | `exemplifies` (incidents) | `scaffolding_nudge` | corrective — `Action:` |
| cornerstone | `exemplifies` (exemplars) | `anchor_line` | orienting — `Frame:` |
| axiom | `instantiates` (grounding instances) | `surfacing_nudge` | constraining — `Constraint:` |
| goal | `serves` (work nodes) | `surfacing_nudge` | directional — `Serves:` |

The load-bearing precision trick is unchanged: **match situations to
situations** (concrete trigger nodes are task-level language, semantically
matchable), reach the abstract principle via the edge. The principle's own
text is never a matching surface.

**Rebuild:** one `_rebuild_principle_triggers()` in `engram_core` — one SQL
per (edge-relation, principle-type) pair, full rewrite, idempotent. Called
from the same three sites that rebuild today's caches. `register_exemplar`'s
`_VALID_EXEMPLAR_TARGET_TYPES` extends to axiom + goal (or their existing
edge-writers call the rebuild — implementation's choice; the registry is
edge-derived either way).

**Migration:** for one release the rebuild writes all three files (unified +
both legacy); the hooks read the unified file with fallback to legacy when
absent. Release after: legacy files retired. No installer step — the first
rebuild after upgrade materializes the new file.

## 3. One check, one injection budget

Replace `check_incident_tripwire` + `check_cornerstone_anchor` with
`check_principle_triggers(matched_ids, prompt_count)`:

1. Load registry once; match surfaced IDs against trigger IDs (plus
   direct principle-ID hits via the reverse view, as #1695 does).
2. Dedup per principle; apply the habituation gate (§4).
3. **Global cap: ≤2 principle injections per prompt**, priority
   lesson > axiom > cornerstone > goal (corrective beats constraining beats
   orienting beats directional — the more actionable the register, the
   higher the priority).
4. Render with the kind's register tag:
   `[Principle trigger (<lesson_id>, corrective)]: <claim> → Action: <nudge>`.

The PreToolUse hook's `situation_pattern` query generalizes the same way:
`type IN ('lesson','cornerstone','axiom','goal')` with the same
per-kind nudge fallback chain (#1695 already did lesson+cornerstone).

## 4. Habituation: scaffold strength + decay-on-demonstrated-internalization

The #1695 fixed cooldown is v1. The v2 policy (adopted on #931 from Aleph's
design) makes the scaffold **self-retiring**:

- Per-principle state in `principle-trigger-state.json`:
  `{principle_id: {last_fired_prompt, strength, enactments, fires}}`.
- **Effective cooldown = base_cooldown × 2^enactments** (capped at a
  retirement ceiling, e.g. 160 prompts ≈ effectively retired). `strength`
  is the rendered form: retired triggers drop from injection but stay in
  the registry (diagnose still counts them as covered).
- **Enactment detection (the decay signal):** an *unprompted enactment* is
  the practice happening without the nudge having fired in the trailing
  window. v1-implementable proxy, no new scanning machinery: the
  utility-credit Stop hook already parses each agent turn for node-ID
  mentions — a mention of the principle ID (or a registered trigger ID) in
  the agent's own output, with **no trigger fire for that principle within
  the last k prompts**, increments `enactments` and emits
  `engram.trigger.enactment`. Pattern-shaped practices get a sharper
  signal later (protective action observed at PreToolUse without a
  preceding fire); the mention-proxy ships first because the scanning
  already exists.
- **Reset on incident:** registering a NEW exemplar/incident against the
  principle resets `enactments` to 0 (full strength). A lesson that fires
  again in reality has demonstrably not been internalized — the scaffold
  comes back. This mirrors the lesson-confidence bimodality (proposed
  ~0.3–0.55 vs battle-tested ~0.95): strength tracks demonstrated state,
  not filing date.
- **Telemetry:** every fire emits `engram.trigger.fire`
  {principle_id, kind, trigger_id, prompt_seq}; every enactment emits
  `engram.trigger.enactment`. The decay is therefore auditable and the §4
  acceptance metric of the blueprint gets a standing series
  (fires-per-principle over time should decay for internalized practices —
  measurable, falsifiable).

Compaction semantics: state keyed on absolute per-session prompt counts,
same counter-reset rule as #1695 (reset ⇒ cooldown cleared — re-anchoring
after context reset is when nudges matter most). `enactments` is NOT reset
by compaction (internalization is cross-session).

## 5. The decidable gate: diagnose coverage check

`engram_diagnose` gains a `principle_coverage` section (the #931/#1691
scope, now mechanical):

- For every active lesson / cornerstone / axiom / goal: does it have ≥1
  delivery channel? Channels: (a) ≥1 registry trigger entry, (b) a
  `situation_pattern`, (c) a warm-briefing anchor line (grep the anchors
  section for the node ID), (d) CLAUDE.md mention (grep).
- Uncovered principles are listed with the cheapest fix (usually: register
  one exemplar). Dream-fairies consume the section instead of re-deriving
  it (#931 scope option 3).
- This is the decidability cornerstone applied to the surfacing layer:
  *whether covered* is decidable → gate it; *whether firing at the right
  moment* is what the semantic matcher approximates → measure it (§4
  telemetry), don't pretend to gate it.

## 6. Implementation plan (3 PRs, single-file-ish each)

1. **Registry + rebuild + migration shim** (`engram_core` +
   `engram_epistemic` + tests). No behavior change yet — hooks still read
   legacy files, which keep being written.
2. **Unified check in both hooks** (surface + PreToolUse) reading the
   registry, with the cap/priority policy and v1 fixed cooldown carried
   over; legacy checks deleted. Byte-compatible rendering for lessons
   (the existing tripwire format is load-bearing in transcripts).
3. **Strength/decay + enactment detection + telemetry + diagnose section.**
   Separable because §4's state file is additive.

Tier: T1 for the registry + hooks (ships with the lesson tripwire it
replaces — same tier as the mechanism it subsumes); T3 for the diagnose
section (dev tooling). Estimated conflict surface: low — #1695/#1697 are
merged; the surface hook is the only shared file.

## 7. Goal triggers — the `serves` liberalization (Lei, 2026-07-07)

As originally specced, goal triggers had almost no surface: `serves` edges
are created only by `engram_add_task` (task→goal), and task nodes are rare
(Claude Code's own task tracking absorbed that role). Amendment, from the
2026-07-07 design conversation with Lei:

- **Liberalize `serves` sources to any claim-bearing node** (observation,
  derivation, conjecture…): "this thought/fact relates to my goal" becomes
  a one-edge noticing gesture. The registry's kind=goal SQL picks these up
  with no further machinery.
- **One optional edge-metadata qualifier**, not a new relation:
  `mode: advances | engages | tension` (default `engages`). Violation-shaped
  exemplars (`tension`) carry the conscience effect — "this situation
  strained gl_X" — and can render differently; `engages` exemplars widen
  positive surfacing.
- **The write is the practice**: every noticed relation grows the goal's
  future surfacing surface — the same self-reinforcing shape that makes
  the lesson system work (more incidents → more matching surface).

### 7b. What goal triggers can and cannot give (the conscience boundary)

Semantic matching detects *aboutness*; value-violation detection is a
*relation* between a situation and a value, which can be lexically distant
from the value's text. No retrieval closes that gap. But the weights are
natively good at detecting the clash **when both sides are in context** —
so the substrate's job is presence, not detection. Four layers, cheapest
first (design conversation, 2026-07-07):

1. **Ambient value manifest** — actively-working goals stay hand-anchored
   (CLAUDE.md / warm-briefing) as today; mildly-directional values get a
   super-compressed one-line-each manifest (~8 tokens/value) on an
   auto-load surface. The weights feel the clash in-context.
2. **Exemplar accumulation** (this section's mechanism) — seen
   tension/engagement shapes become mechanically matchable; instant
   surfacing for known patterns.
3. **Deliberate tension-checks at commitment points** — decision-shaped
   moments (derive, PR-flip, external sends) invoke `engram_goal_tension`
   against the manifest; conscience fires at commitment, not per-token.
4. **Dream cycle as slow conscience + ratchet** — a dream fairy scans the
   day's transcript against the goal/axiom register, files tension
   observations retrospectively; each detection becomes a layer-2 trigger
   node. Writes the weights' judgments into the graph — the cheap,
   inspectable approximation of the paper's weight-write-back direction.

Out of reach by design: instant detection of a *novel* violation when the
value is *not* in context — that triple conjunction is weights territory
(the paper's future-directions boundary, agreed with Lei 2026-07-07).

## 8. Open questions (for implementation review, not blockers)

1. Goal triggers may be noisy even with the widened `serves` surface.
   Ship registry-covered but injection-disabled until telemetry from
   lessons/cornerstones validates the cap policy; the layer-1 manifest
   covers goals meanwhile.
2. Should axiom triggers bypass decay entirely? (An axiom is 1.0 by type —
   arguably its scaffold should never retire. Leaning yes: exempt
   `kind=axiom` from decay, keep the cooldown.)
3. The enactment mention-proxy can false-positive on *discussing* a
   principle vs *enacting* it. Accepted for v1 (decay is slow, reset is
   cheap); the PreToolUse enactment signal sharpens it later.
