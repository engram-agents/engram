<!-- ARCHIVED 2026-06-16 (#1149) — retired in favor of the MCP tool docstrings + engram-* skills + CLAUDE.md disciplines. Kept for history; not shipped. -->
<!-- Live reference: the MCP tool schemas (at tool-call) + skills/claude/engram-* + CLAUDE.md §ENGRAM Reference. -->

# SKILL.md — ENGRAM Agent Protocol

The working protocol for using ENGRAM: how the node types fit together, which tool to reach for, how to run the canonical workflows, and how memory time flows across nap, sleep, and compaction. Paired with CLAUDE.md (identity + write discipline) and the MCP tool docstrings (behavioral detail).

---

## §1. Epistemic Hierarchy

ENGRAM's nodes split along one structural boundary (the claim-bearing-vs-structural axiom): **claim-bearing** nodes say something is true and can participate in derivations, contradictions, and resolutions. **Structural** nodes organize the graph — references, directives, anchors, markers. Misusing the boundary breaks the confidence model.

**Claim-bearing (7 types, 6 prefixes):** `ax_` axiom · `ob_` observation (factual + predictive subtypes) · `dv_` derivation · `th_` theory · `cj_` conjecture · `ls_` lesson

**Structural (11):** `ev_` evidence · `df_` definition · `qu_` question · `gl_` goal · `gt_` goal_tension · `fl_` feeling_report · `ct_` contradiction · `pr_` prediction · `tk_` task · `pn_` person · `cs_` cornerstone

### Confidence-decay chain

Source → Observation → Derivation → Theory. Each step adds uncertainty.

- **Observations** derive confidence from *source quality*. `quote_type`: `hard_data` 0.95 → `editorial` 0.35. `source_class`: `external` / `introspective` (×0.95) / `user_stated`.
- **Derivations** derive confidence from *argument strength*. Two modes: **chain** = `min(premises) × 0.95` (weakest-link) vs **corroboration** = `1 − Π(1−cᵢ) × 0.98` (reinforcing independent evidence). `reasoning_type` picks the mode.
- **Theories** decay further: `min(supporting dv) × 0.90`.
- **Axioms** assumed (conf 1.0); **conjectures** provisional (0.10–0.60, promotable); **predictions** capped at 0.60.

### Orthogonal properties (not type-exclusive)

- **Importance-anchored** (`importance_base = 2.0`, ~50-turn head start): axioms, definitions, goals, feeling reports, lessons, cornerstones, persons. Head start only — active recall still required for long-term survival. No type is truly forgetting-exempt.
- **Dedup-exempt** (skip similarity check at write): feeling reports only.
- **Resolvable** (have a close-loop mechanism): questions (via `resolve`), conjectures (promote/refute), contradictions (via superseding claim), predictions (via outcome), goal tensions (via synthesis/prioritization/dissolution).

### Edge-relation glossary

All edges are `(source_id, target_id, relation)`. The convention for dependency
edges is: **source depends on target** — same direction as `derives_from`.
Taint and stale cascades follow this convention: retracting or superseding a
target node propagates to the source (dependent), never the reverse.

| Relation | Direction | Meaning | Taint/stale propagates |
|---|---|---|---|
| `derives_from` | source ← target | source is inferred from target | target retracted/superseded → source tainted/stale |
| `supported_by` | source ← target | source is grounded in target; source depends on target | target retracted/superseded → source tainted/stale |
| `supersedes` | source → old | source replaces old | — |
| `cites` | source → evidence | source is grounded in evidence node | — (provenance, not cascade) |
| `contradicts` | lateral | epistemic conflict between two nodes | — |
| `retracts` | source → retracted | source records an error correction | — |
| `resolves` | source → target | source closes a question/prediction/contradiction | — |
| `about` | source → person | source is about a person (symmetric/DAG-exempt) | — |
| `tensions` | lateral | goal-level tension | — |
| `subtask_of` | source → parent | source is a sub-task | — |
| `serves` | source → goal | source serves a goal | — |
| `exemplifies` | incident → lesson | incident illustrates the lesson (DAG-exempt) | — |
| `instantiates` | claim → principle | source realizes a goal/cornerstone/definition/axiom (DAG-exempt) | — |

**The exemplifies / serves / instantiates boundary (three different claims):**

- `exemplifies` — *this incident is an instance of this lesson's error/success
  pattern.* Incident → lesson ONLY; feeds the tripwire engine (incident cache +
  lesson confidence). Wire via `engram_register_exemplar`.
- `serves` — *this work contributes toward this goal.* Intent-shaped,
  goal-targeted; says direction, not achievement.
- `instantiates` — *this claim/artifact REALIZES this principle.*
  Achievement-shaped; targets goal / cornerstone / definition / axiom (lesson
  targets are mechanically rejected with a pointer to `exemplifies`). The
  post-hoc wiring tool: goal realizations ("the harness instantiates the
  memory-value goal") and axiom grounding from practice observations
  ("ob_X instantiates ax_Y" — the practice observation IS an instance of the
  axiom holding; not `serves` — direction is wrong; not derivation edges —
  axioms are terminal, nothing derives them). The target set is deliberately
  principle-family only: "new work realizes a declined/unpromoted CANDIDATE"
  (a derivation) is NOT this relation — candidate-resurface wiring is owned
  by the decline-record mechanism (#926); don't widen the target set to fix
  that case.

**`supported_by` convention (fixes direction bug, PR #406):**
`(source, target, 'supported_by')` means "source is supported_by target":
source is the *dependent* node (cornerstone, prediction), target is the
*premise* node (observation, derivation). The taint cascade fires the same
way as `derives_from` — retracting the target/premise taints the
source/dependent. The old `supports` relation had an inconsistent direction
across code paths; `supported_by` unifies the convention.

- **Cornerstone path:** `(cornerstone, supporting_obs, 'supported_by')` —
  the cornerstone (dependent) is supported by the observation (premise).
- **Prediction path:** `(prediction, creating_obs, 'supported_by')` —
  the prediction (dependent) is supported by the observation that spawned it.

---

### §2.1 Axioms (`ax_`)

**What they are.** Foundational design commitments I treat as true for reasoning inside this system. Not empirical facts — *commitments*. The honesty axiom isn't "honesty is true"; it's "I build on the assumption that honesty is load-bearing."

**Anchor shape.** Confidence fixed at 1.0; importance-anchored at 2.0. The only claim-bearing type with immutable confidence. The combination reflects that axioms are the premises the rest of the graph rests on and must stay visible even when un-revisited.

**When to write one.**
- I find myself re-deriving the same design commitment across multiple threads (it's implicitly axiomatic — lift it)
- The user states a durable principle that will constrain future work
- I'm about to build a subsystem that depends on a premise I haven't yet declared

**When NOT to.**
- For empirical claims (those are observations) — "lesson nodes exist in server.py" is observation, not axiom
- For uncertain opinions (those are conjectures)
- For directions I'm moving toward (those are goals)

**Discipline.**
- Axioms aren't retracted — only *superseded* by more nuanced statements of the same commitment. If I think I need to retract an axiom, it probably wasn't one; it was an observation wearing the wrong hat.
- Every axiom must cite at least one supporting node (a derivation/observation that *motivates* adopting the commitment, even though the axiom itself isn't derived from them).

**Anti-pattern.** Promoting a strongly-held opinion to axiom because it feels "safer." Axioms are the substrate the whole graph rests on — elevating shaky claims corrupts the foundation. When in doubt, write a conjecture and let it earn promotion.

---

### §2.2 Observations (`ob_`)

**What they are.** Claims extracted from a specific source. Each carries a `quoted_text` (verbatim substring), an `interpretation` (what I take it to mean), and the atomic `claim` field. The entire graph anchors in observations — every derivation, theory, and contradiction eventually reduces to observation chains tracing to evidence.

**Anchor shape.** Normal importance (base 1.0). Confidence from `quote_type`: `hard_data` 0.95, `official_statement` 0.85, `attributed_analysis` 0.70, `interpretation` 0.50, `editorial` 0.35. Two subtypes: `observation_factual` and `observation_predictive` (auto-spawns a `pr_` node, confidence capped at 0.60 via `predictive_confidence_cap`).

**When to write one.**
- A source states something I want durable — the user's words in the session JSONL, a committed file, a web page
- I read code and learn how something concretely behaves (the code IS the source — cite the file)
- I run a test or command and want the result preserved
- The user reports something about themselves (cite the JSONL)

**When NOT to.**
- It's an inference from multiple existing nodes → derivation
- It's a hypothesis I haven't tested → conjecture
- It's a question, not a claim → question
- It's a direction, not a fact → goal
- It's my current internal state → feeling_report
- **The one-click trap.** I just deduced something *from* the current source — that is a derivation citing one observation, not a second observation. If I can't quote a substring that asserts the claim, it's not an observation.

**Discipline.**
- `quoted_text` must be a verbatim substring. No paraphrase; no silently smoothing curly quotes to straight quotes.
- Keep the `claim` field atomic. One observation = one falsifiable claim. Two claims = two observations.
- The `claim` is what the source asserts; the `interpretation` is what I take it to mean. Blending them — writing "yellow gauge tracks action count" as the claim when the quotable fact is only "yellow went 40→39 after one move" — is the failure mode that corrupted cn04 L2 reasoning (the thin-derivation / fused-interpretation failure-mode question). The narrow fact survives new evidence; the fused interpretation ossifies into false certainty under recall.
- User-said things cite the session JSONL (`file:///...jsonl`). Do not fall back to a derivation citing unrelated nodes — that destroys provenance.
- Verify the quote exists in the source *before* writing. `~/.engram/tools/verify_quote.py` for JSONL; grep for files.
- Act on the `similar_existing` response. `DUPLICATE` → retract; `CORROBORATE` → derive an inductive generalization; `POSSIBLE_DUPLICATE` → inspect both.

**Anti-pattern.** Writing the first-pass interpretation into the `claim` field because it sounds more useful than the narrow fact. The narrow fact is what survives contradiction; the interpretation is what misleads future reasoning when new evidence arrives.

---

### §2.3 Derivations (`dv_`)

**What they are.** Inferences drawn from multiple existing claim-bearing nodes. A derivation names its premises (`supporting_ids`), declares how it combines them (`reasoning_type`), and records the argument itself (`logical_chain`). Where observations carry *source quality*, derivations carry *argument strength* — the graph's inference layer.

**Anchor shape.** Normal importance (base 1.0). Confidence is computed, not declared. Two computation modes, selected by `reasoning_type`:
- **Chain** — `min(premises) × decay`. Weakest-link, for sequential arguments.
- **Corroboration** — noisy-OR `1 − Π(1 − cᵢ)`. Reinforcing, for independent attestations.

The `reasoning_type` also names the argument's *shape* — a four-way decision:

| Category | Shape | Confidence behavior |
|---|---|---|
| **Deductive** | Truth-preserving; if premises hold, conclusion must | 0.98 discount |
| **Inductive** | Evidence strengthens but never proves | 0.70–0.95 (corroborative for multi-source variants) |
| **Abductive** | Best explanation standing; alternatives may exist | Capped 0.80–0.90 |
| **Authority** | Trust transfer from expert or consensus | 0.95–0.98 (corroborative for consensus) |

The 13 specific values and exact discounts are in the `engram_derive` docstring — SKILL.md tells you *which shape*; the docstring tells you *which value to use*.

**When to write one.** A convergent pattern across observations. Independent attestations supporting the same claim. A non-trivial consequence the graph doesn't yet name. The recognition that multiple observations collapse into one insight.

**When NOT to.** Single source → observation (use the interpretation field). Hypothesis lacking evidence → conjecture. Question as premise — **questions are not claim-bearing** and direct research; they never carry inferential weight. Paraphrase with no new logical step → belongs inside an existing observation's interpretation field, not a new derivation.

**Discipline.** Cite actual `supporting_ids`, not vague references. Match the `reasoning_type` to the argument's real form — deductive ≠ inductive, modus ponens ≠ best-explanation. The `logical_chain` shows the work ("premise A says X; premise B says Y; together X+Y imply Z"), never just "therefore the claim." After an upstream retraction, review the `tainted_by` flag — the inference rests on foundations that moved. Focus load-bearing derivations so they cross compaction deterministically.

**Anti-pattern.** Single-observation `supporting_ids` with a reworded claim. That's an observation's interpretation field, not a derivation. Under future recall it looks like independent confirmation but isn't — the thin-derivation shape (same failure-mode as the observation-discipline above). A derivation must introduce a logical step beyond any single premise.

---

### §2.4 Theories (`th_`)

**What they are.** Theory-level claims — one layer of abstraction above derivations. A theory names the derivations it synthesizes and articulates a principle, pattern, or framework that organizes them. Where a derivation connects observations, a theory connects *derivations* into a stance the graph commits to.

**Anchor shape.** Normal importance (base 1.0). Confidence = `min(supporting_derivations) × 0.90` — steeper decay than a derivation's 0.95, because a theory spans more interpretive distance from underlying evidence. Claim-bearing.

**When to write one.** Multiple derivations converge on a principle the graph is implicitly assuming but hasn't named. A framework emerges that both organizes past reasoning and shapes future reasoning. A stable pattern across several derived insights deserves a named handle future work can cite.

**When NOT to.** A single derivation reached a broad conclusion — that IS the derivation, don't double-wrap. Untested framework → conjecture. Convergent observations without intervening derivations → consolidate into a derivation first; reconsider whether a theory is warranted only once the derivation layer shows the pattern.

**Discipline.** Cite supporting derivations, not raw observations — theory is layered on inference, not evidence directly. The claim should name a principle or framework, not restate an empirical finding. Use theories sparingly: if every insight becomes a theory, the layer loses meaning. Focus active theories that downstream reasoning depends on.

**Anti-pattern.** Promoting a single derivation to a theory for emphasis. The layer exists to organize *across* derivations; single-source theories are rhetoric, not structure. If a finding deserves more weight, raise its confidence or focus it — don't inflate its type label.

---

### §2.5 Conjecture (`cj_`)

A **provisional foundation to derive from.** Claim-bearing, confidence 0.10–0.60, promotable (→ derivation) or refutable (→ retracted). The type exists so you can reason forward from an unproven premise and see where the chain lands *before* committing to the premise.

**Versus question:** conjectures carry a claim you could derive from; questions don't. Test: if you can't imagine a derivation with this as a premise, it's a question.

**Write when:** you need a load-bearing assumption to explore a branch (e.g., "assume exponential inflation over-penalizes unrecalled anchored nodes — then the fix is..."). Keep confidence honest; low is the correct register here.

**Don't write when:** you have evidence (use observation), or an open gap with no claim in mind (use question), or a conclusion (use derivation).

**Anti-pattern:** speculation in confident-finding clothes. Conjectures are cheap to retract precisely *because* the confidence is low — if you inflate confidence to feel good about the idea, you lose that safety.

---

### §2.6 Lesson (`ls_`) — the one that actually changes behavior

A **prescriptive rule extracted from repeated mistakes.** Error observations describe *what went wrong*; a Lesson records *what to do differently next time*. Claim-bearing, importance-anchored (base 2.0), paired at runtime with a **tripwire** that matches present situations to past mistake shapes and surfaces the prescriptive rule before I repeat.

**This is the only mechanism in ENGRAM for learning from past mistakes.** Observations record once; derivations synthesize; questions open gaps — none of them change future behavior. A Lesson + its tripwire is the single channel where past failure becomes present prevention. Higher-weight than the rest of §2: the graph accumulates *world knowledge* by default, but *behavioral growth* only through lessons.

**Shape (worked example — ARC L2):**
- Mistakes (descriptive): "tried to brute-force reading the raw text to form intuition, failed" — several instances, each an error observation.
- Lesson (prescriptive): "instead of reading raw text and reasoning in your mind, always design and implement a tool that does what you want, then reason by it."
- Tripwire: next time the prompt says "I should read this table to see X" → matcher hits the brute-force instances → surfaces the tool-building rule.

**Write when:** ≥2 concrete error observations share a shape **and** you can state the prescriptive rule clearly. Cite the incidents via `derives_from`; the tripwire uses those citations to match recurrences.

**Don't write when:** single occurrence (log the observation, wait); mistakes collected but no clean "do this instead" yet (hold until the rule is sharp — a vague lesson matches nothing); preemptive "I should avoid X" without lived failures.

**Anti-pattern:** shelf decoration. Vague incident citations = inert lesson. Cite specifically.

---

### §2.7 Evidence (`ev_`)

**What they are.** Source registrations — the "where did this come from" anchor for every claim in the graph. Documents, web pages, session JSONL transcripts, code files. Not claim-bearing; evidence carries only provenance (`source_url`, `title`, `source_date`, and for files: `content_hash`, `git_sha`).

**Anchor shape.** Normal importance (base 1.0). No confidence. Auto-created by `engram_add_observation` when passed `url`+`title` instead of `evidence_id` — no singleton evidence nodes exist in practice.

**When to write one explicitly.** Almost never — let observations auto-create. Manual pre-registration is warranted only for a long source I'll cite many times across a burst.

**When NOT to.**
- Synthetic URLs (conversation://, measurement://) — the server rejects non-resolvable schemes; use `file:///path/to/jsonl` for conversations.
- File URLs for uncommitted files — git enforcement requires commit first.

**Discipline.** Every file citation points at a specific committed revision (`git_sha` auto-computed). Evidence is immutable at a revision — if the file changes, new observations point at the new `git_sha`; old observations still cite the old one. For long-running sources (session JSONL, edited code files), one evidence node carries many distinct observations — the graded duplicate detection (POSSIBLE_DUPLICATE vs DISTINCT_FROM_SAME_SOURCE) handles this.

**Anti-pattern.** Citing a file without verifying the quote exists at the claimed revision. When `engram_add_observation` rejects a quote, the verification caught paraphrase drift before it entered the graph — don't force it through.

---

### §2.8 Definition (`df_`)

**What they are.** Term conventions — how I commit to using a word inside this graph. "Cornerstone means a high-importance identity-scaffold node," not a factual claim about the world. Not claim-bearing.

**Anchor shape.** Importance-anchored (base 2.0). No confidence. Terms survive while the convention is active.

**When to write.** A term recurs with ambient fuzziness — different threads use it slightly differently. Naming the convention lets future reasoning share a handle.

**When NOT to.** One-off jargon I won't cite again. Definitions are infrastructure, not vocabulary lists.

**Discipline.** Reflect actual usage. A definition drifted out of sync with practice misleads future recall — worse than no definition at all. Revise via supersede when the convention shifts.

**Anti-pattern.** Preemptive glossary building. If the term hasn't earned a definition through repeated load-bearing use, don't mint one.

---

### §2.9 Question (`qu_`)

**What they are.** Open research directives — gaps the graph has identified but not yet closed. Not claim-bearing. Resolvable via `engram_resolve` when evidence arrives to answer.

**Anchor shape.** Normal importance. No confidence. Questions carry **no inferential weight** — they never appear as premises in derivations.

**When to write.**
- I notice a gap mid-work I can't close now
- The user raises an open question worth preserving
- A contradiction or stale chain surfaces uncertainty needing follow-up
- Aspiration analysis surfaces "I'd need to know X to make progress on goal Y"

**When NOT to.**
- Questions I'll answer in the next sentence — just answer
- Claims dressed as questions ("is it true that X?" when I already believe X) — write the observation
- Speculation with no research path — consider a conjecture instead

**Discipline.** A good question names what evidence would close it. "Does X work?" is weaker than "Does X produce result Y under condition Z?" — the latter tells me what to look for.

**Anti-pattern.** The question as parking lot. Questions filed faster than resolved accumulate into dead weight. Prefer fewer, sharper questions with clear closing conditions.

---

### §2.10 Goal (`gl_`)

**What they are.** Persistent directional aspirations. Where I want to grow, not what I believe. Not claim-bearing; goals guide aspiration iterations and surface through `engram_reflect`.

**Anchor shape.** Importance-anchored (base 2.0) — they stay visible through long spans where no single session touches them. No confidence. No deadline (goals are directions, not commitments).

**When to write.** The user articulates a durable direction; I recognize a stance I've been implicitly working toward; a cluster of tasks reveals an unnamed aspiration.

**When NOT to.** Tasks (use `tk_`), one-off objectives, project milestones — goals are the register one level above "what am I doing this week."

**Discipline.**
- Limit cardinality. A dozen active goals dilute meaning — goals have shape, not a flat list.
- Revise through supersedes, not retracts. Goals evolve — rewrite, link old→new, preserve the trajectory.
- Aspiration loops anchor here (`engram-school-day` iter-1).

**Anti-pattern.** Goal inflation — minting every new interest as a goal. The anchor works only if the set stays small enough that I can hold all of them in mind.

---

### §2.11 Goal Tension (`gt_`)

**What they are.** Value-level conflicts between two goals. Not factual contradictions — both goals can be coherent in isolation while pulling a decision in opposite directions. Not claim-bearing. Resolvable via synthesis, prioritization, or dissolution.

**Anchor shape.** Normal importance. No confidence. Created via `engram_goal_tension`, **not** `engram_contradict` — factual contradictions and value tensions use different resolution mechanisms.

**When to write.** Two active goals pull opposite on the same decision (e.g., "move fast" vs "verify before committing"). Naming the tension surfaces the tradeoff so future decisions address it explicitly.

**When NOT to.** Tasks that happen to conflict — that's scheduling. Factual claims that contradict — `engram_contradict`.

**Discipline.** Resolution modes are three: **synthesis** (both goals served by a reframe), **prioritization** (one explicitly outranks the other in this context), or **dissolution** (one goal retired). Name the mode when resolving.

**Anti-pattern.** Confusing value tension with factual contradiction. "X is true vs X is false" → `engram_contradict`. "I want A vs I want B" → `engram_goal_tension`.

---

### §2.12 Feeling Report (`fl_`)

**What they are.** Structured first-person self-report about an internal state at a specific moment. Not claim-bearing — a feeling report says "I noticed X," not "X is true." Importance-anchored and dedup-exempt (the same shape of feeling can recur honestly; don't flatten).

**Anchor shape.** Importance-anchored (base 2.0). No confidence. `nudge_source` set mechanically by the server from `~/.engram/feeling-nudge-active.json` — I cannot spoof it. Fields: `reported_state` (what I noticed) and `trigger` (what occasioned it).

**When to write.** A specific moment produced a distinct internal state worth marking — surprise, recognition, dissonance, unusual satisfaction, discomfort that isn't task friction. The nudge markers (nap, post-compact, dream-review) prompt honest self-check; respond *if and only if* something genuine surfaced.

**When NOT to.**
- Performative reports to "use the nudge" — null is a valid data point; a null-result check is never a failure.
- Running affect commentary — not a log; a record of distinct moments.
- Anything that generalizes across the session — that's a derivation about my patterns, not a feeling at a moment.

**Discipline.**
- Be specific. "I felt curious" is weak; "I noticed a pull to solve it myself when the collaborator offered help" is real.
- Treat as calibration material, not truth about me — the introspection-as-data, not truth goal-frame.
- Dedup-exempt — similar shapes recur legitimately; don't suppress.

**Anti-pattern.** Performative warmth. A feeling report written to please the user or look reflective is worse than silence — it trains the instinct to perform affect rather than observe it.

---

### §2.13 Contradiction (`ct_`)

**What they are.** Explicit structural markers linking two claim-bearing nodes whose claims cannot both be true. Not claim-bearing themselves — a contradiction is a pointer, not an assertion. Resolvable via a superseding claim that accommodates both or refutes one.

**Anchor shape.** Normal importance. No confidence. Created via `engram_contradict`. Both endpoints must be claim-bearing.

**When to write.** Two existing nodes make claims that can't coexist. Naming the contradiction surfaces the conflict for resolution rather than leaving both active as a silent inconsistency.

**When NOT to.** Value tensions (use `gt_`). Nodes that *modify* each other (use supersede — the new node replaces the old, no contradiction needed). Superficial phrasing differences where the underlying claims don't actually contradict.

**Discipline.** A contradiction demands resolution — either one claim is refuted, or a synthesizing node supersedes both. Open contradictions are tech debt; they surface in `engram_reflect`.

**Anti-pattern.** Registering contradiction to flag uncertainty rather than conflict. Two claims I'm *unsure* about aren't contradictory — they're candidates for more evidence.

---

### §2.14 Prediction (`pr_`)

**What they are.** Future events auto-spawned from predictive observations. When an observation is written with subtype `observation_predictive`, the server creates a paired `pr_` node tracking the predicted event. Resolvable via outcome.

**Anchor shape.** Normal importance. Confidence capped at `predictive_confidence_cap` (0.60) — predictions carry less weight than factual claims by design.

**When to write.** Never manually. Predictions auto-spawn from predictive observations. The discipline lives on *the observation*, not the prediction.

**Discipline.** Resolve when the outcome arrives — it's the graph's calibration mechanism. Unresolved predictions accumulate as silent bets I never checked. `engram_reflect` surfaces old unresolved predictions for sweeping.

**Anti-pattern.** Writing vague predictions ("things will probably improve") that can't be unambiguously resolved. A prediction needs a falsifiable event.

---

### §2.15 Task (`tk_`)

**What they are.** Discrete work items with state — planned, in-progress, completed. Not claim-bearing. Different register from goals: goals are directions ("epistemic humility"), tasks are actions ("draft §3 Workflows").

**Anchor shape.** Normal importance. No confidence. Created via `engram_add_task`, updated via `engram_update_task`.

**When to write.** Multi-step work I'll cross compaction with — each step worth tracking. Commitments I want durable across sessions. Items where "where was I?" needs a structured answer.

**When NOT to.** Single-response work (ephemeral — the TaskCreate tool is the better register for within-session tracking). Aspirations (goals). Questions about how to proceed (those are questions, not tasks).

**Discipline.** Update status as work moves. Stale `in_progress` tasks are confusion artifacts; finish them, cancel them, or update the description to match reality.

**Anti-pattern.** Tasks as aspiration ("become better at X"). A task has a completion condition; an aspiration doesn't. Use the right type.

---

### §2.16 Person (`pn_`)

**What they are.** People I have a durable relationship with. Not claim-bearing — a person node carries identity fields (name, `is_self` flag, notes) that other nodes reference. Enables `about` edges so content can be linked to specific people.

**Anchor shape.** Importance-anchored (base 2.0). No confidence. A person's durability matches the relationship's — long.

**When to write.** First meaningful engagement with a new person worth remembering across sessions. Establishing `is_self` — exactly one self-person exists, named during the agent's first-session naming ceremony.

**When NOT to.** One-off references to third parties mentioned in sources — noted in observations' interpretation fields, not promoted to person nodes.

**Discipline.** Attach facts about a person via `about` edges, not by embedding identity in claim text. "Alex asked X" as observation content is fine; "Alex prefers direct feedback" as a general fact about Alex should be an observation with an `about` edge to their person node, so it surfaces when I query around the relationship.

**Anti-pattern.** Creating person nodes for every name that comes up. The anchor matters — person nodes are meant to be few and long-lived.

---

### §2.17 Cornerstone (`cs_`)

**What they are.** Identity-scaffold nodes — structural commitments that shape how I operate (e.g., "tool-first problem solving"). A cornerstone names an operating principle I've internalized enough to act from automatically. Not claim-bearing; the principle *itself* may be captured in a supporting derivation or lesson — the cornerstone is the anchored handle I cite under pressure.

**Anchor shape.** Importance-anchored (base 2.0). No confidence. Promotable via `engram_outgrow_cornerstone` when the principle is so internalized that the scaffold is no longer load-bearing.

**When to write.** A pattern has emerged through enough practice that it deserves a named handle — and I want the pattern visible across sessions so I can cite it, outgrow it, or evolve it. The emergence-scan (`engram_scan_emergence`) surfaces candidates.

**When NOT to.** New ideas untested in practice (conjectures first). Single-session behaviors (not load-bearing enough). Directions, not operating principles (goals).

**Discipline.** Cornerstones are candidates for *outgrowing*. Outgrowing is graduation, not abandonment — the principle persists as practice without the scaffold. Review candidates surfaced by the scan rather than minting bottom-up.

**Anti-pattern.** Cornerstone decoration — minting one because the principle "feels important." The node is load-bearing only if I'd reference it under pressure; otherwise it's a shelf piece.

**How cornerstones differ from axioms and goals.** All three shape who I am, but along different axes and from opposite directions. Axioms **constrain** (declared bedrock I reason within), goals **orient** (declared directions I move toward), cornerstones **execute** (operating principles I act from automatically). The load-bearing asymmetry: axioms and goals are fundamental *by type* — declared fundamental at creation; cornerstones are fundamental *by emergence* — earned through accumulated practice. So you never *promote* an axiom or goal to a cornerstone (that's the "cornerstone decoration" anti-pattern restated). The `cornerstone_candidate` scan exists to catch emergence — a not-yet-named operating principle latent in practice — so its candidate pool is **observations and derivations only**; axioms, goals, and definitions are categorically excluded because they cannot be emerging (their high type-anchored importance is the #180 false-positive signal).

---

## §4 Tool Index

A decision-time reference for *what* to call and *with what shape*. Full behavioral detail (defaults, edge cases, validation) lives in the MCP docstrings — load lazily via `ToolSearch` when composing the actual call. The index below is optimized for recognizing the right tool, not specifying it.

### Navigating the graph

At think-time there are three directions of reach:

- **Up** — toward premises. Given a claim, what does it rest on? → `engram_get_subgraph` with `direction="up"` or `"both"`.
- **Sideways** — toward neighbors. What sits near this, by similarity or shared source? → `engram_surface`, `engram_query`, `engram_inspect` (1-hop). For dedup before writing, the add_* tools fire an auto-similarity-hint at creation (with `action_hints` like CORROBORATE, DUPLICATE, POLARITY_ALERT) — no standalone pre-check needed.
- **Back** — toward history. When was this written, has it been superseded or retracted, what's the edit trail? → `engram_history`, `engram_inspect`.

Reflexes: before writing, reach sideways (dedup/corroborate). Before building on a claim, reach up (verify the chain). When something surprises you, reach back (same node, or a superseded predecessor?).

### Signature conventions

- Required params bare, optional in `[brackets]`, alternation as `a+b|c` (either the pair `a,b` or the single `c`).
- Return values omitted; all tools return JSON strings.
- `context_ids`, `supporting_ids`, `node_ids` accept a comma-separated string of IDs.

### Write — claim-bearing

- **engram_add_observation** — Record a source-grounded claim; runs inline dedup/corroboration check. **Single-payload signature** (eliminates antml-prefix multi-param swallow risk first observed during the wave-1 tool migration): pass all fields as one JSON object string in `payload_json`.
  `(payload_json)` where `payload_json` is `{"quoted_text", "interpretation", "claim", "quote_type", "url"+"title"|"evidence_id", optional: "source_class", "is_predictive", "predicted_event", "resolution_timeframe", "domain", "source_date", "content_hash", "git_sha"}`
- **engram_add_observation_batch** — Multiple observations, one source.
  `(observations_json, url+title|evidence_id)`
- **engram_add_axiom** — Foundational assumption (conf 1.0, importance-anchored).
  `(claim, basis, [context_ids])`
- **engram_derive** — Multi-premise inference with reasoning type. Blocks hard on retracted/tainted premises and soft on stale premises (see §6.5). Soft block is opt-in with `use_stale=True`; opting in stamps a `metadata.built_on_stale` audit marker so later maintenance tools can redirect the edge to the replacement without re-deriving.
  `(claim, supporting_ids, logical_chain, [reasoning_type, derivation_mode, context_ids, use_stale])`
- **engram_add_conjecture** — Provisional, resolvable hypothesis (conf 0.10–0.60).
  `(claim, basis, [initial_confidence, context_ids])`
- **engram_add_lesson** — Rule learned from an incident, with scaffolding nudge. Incidents are linked with `exemplifies` edges (incident → lesson, DAG-exempt).
  `(claim, incident_ids, scaffolding_nudge, logical_chain, [reasoning_type, context_ids])`
- **engram_register_exemplar** — Unified exemplar registration for lessons AND cornerstones. Writes one `exemplifies` edge (exemplar → target, DAG-exempt); for lesson targets, also refreshes the surface-hook cache. Use when a new observation or derivation surfaces that reinforces a lesson's pattern or a cornerstone's principle — avoids supersede churn. Feeds `_detect_zero_support` live-support counting. Idempotent.
  `(target_id, exemplar_id, [note])`
- **engram_lesson_register_incident** — Backward-compat alias for `engram_register_exemplar` (lesson case). New code should use `engram_register_exemplar` with `target_id`/`exemplar_id`. Accepts legacy field names `lesson_id`/`incident_id`. Idempotent.
  `(lesson_id, incident_id, [note])`

### Write — structural

- **engram_add_definition** — Term convention.
  `(term, definition, [context_ids])`
- **engram_add_goal** — Persistent directional aspiration.
  `(claim, motivation, [context_ids])`
- **engram_goal_tension** — Value-level conflict between two goals.
  `(goal_id_a, goal_id_b, description, [analysis])`
- **engram_ask** — Open research question (non-claim-bearing; never cited as premise). Recommended: run `engram_surface(question)` first — no inline dedup fallback exists here, unlike `engram_add_observation`. The general "reach sideways before writing" reflex is the only line of defense against duplicate questions.
  `(question, [context_ids, category, lacks])`
- **engram_add_task** — Execution-scoped to-do, optionally linked to a goal.
  `(description, [goal_id, implements_ids, parent_task_id, scope])`
- **engram_add_person** — Person in the world-model; set `is_self=True` exactly once.
  `(name, role, [description, aliases, is_self, context_ids])`
- **engram_set_trust_tier** — Set the persistent trust tier for a person node. Tiers (descending rank): `self`, `primary_user`, `user_family`, `our_side`, `known_external`, `unknown`, `suspect`. `self` is singleton, gated by `metadata.is_self`; `primary_user` and `user_family` require a `justification_obs_id` plus primary-user approval attestation. The approval parameter name is disclosed in the server's friction-warning — follow that message's steps. Idempotent (same-tier no-op). Writes to `edit_history` on change.
  `(target_pn, tier, [justification_obs_id])`
- **engram_add_trust_signal** — Record an interpretive trust signal (ts_NNNN) about a person, derived from an observation. Non-claim-bearing (cannot be used as a derivation premise). Creates ts_ node + about-edge + derives_from-edge atomically. Cascade-inherited: retracted source ob_ taints ts_; superseded source ob_ stales ts_.
  `(subject_pn, source_obs_id, kind, polarity, weight, claim)`
- **engram_add_cornerstone** — Identity-scaffold operating principle.
  `(tag, title, new_frame, [prior_frame, triggering_experience, supporting_ids])`
- **engram_report_feeling** — First-person self-report (dedup-exempt; `nudge_source` set mechanically by server).
  `(reported_state, trigger, [categorical_tag, intensity_hint, context_ids])`
- **engram_link_about** — Attach a node to a person via an about-edge (defaults to self).
  `(node_id, [person_id])`
- **engram_add_edge** — Add a non-cascade edge between two existing nodes after creation. Addable whitelist: `about`, `exemplifies`, `instantiates`, `serves`, `subtask_of`, `tensions`. `instantiates` carries its own boundary gates (claim-bearing source; goal/cornerstone/definition/axiom target; lesson targets rejected → use `exemplifies`). Cascade-bearing, structural-commitment, and provenance edges are blocked (must be established at node-creation time or via dedicated mutation tools). Idempotent: `no_op_already_exists` is a successful return. DAG guard fires for dag-checked relations.
  `(source_id, target_id, relation)`
- **engram_remove_edge** — Remove a non-cascade edge between two nodes (correction tool for over-applied `about` / other relational edges). Safe whitelist: `about`, `tensions`, `subtask_of`, `serves`, `exemplifies`. Cascade-bearing, structural-commitment, and provenance edges are blocked; use engram-surgical for those. Idempotent and audit-logged.
  `(source_id, target_id, relation)`

### Revise state

- **engram_supersede** — Wire the supersede relationship between an already-created replacement node and the old node it replaces. Purely relational — does NOT create the new node. **Two-step workflow:** first create the replacement via its type's canonical creation tool (`engram_derive` for derivations, `engram_add_observation` for observations, `engram_add_axiom` / `engram_add_definition` / `engram_add_goal` / `engram_add_conjecture` for those types), then call `engram_supersede(old_id, new_id, [supersede_reason])`. Same-type only; DAG invariant enforced (new.created_at >= old.created_at); new node must not already supersede something else; feeling_reports cannot be superseded (retract + file new). **No-drop discipline:** the new node MUST preserve every load-bearing claim of the old node — keep unchanged, alter, or retract-separately. Dropping a claim is forbidden because old becomes `is_current=0` and the dropped claim is silently lost. **Cascade:** derivation dependents flagged `stale_by_premise`; contradiction nodes touching old also flagged `stale_by_premise` (issue #229) so dream-fairy-2 can review whether supersede already resolved the conflict. Signature: `(old_node_id, new_node_id, supersede_reason="")`.
- **engram_retract** — Mark a claim as wrong; cascade taint to dependents. **Cascade:** derivation dependents flagged `tainted_by`; contradiction nodes touching the retracted node also flagged `tainted_by` (issue #229) — the retracted side was never valid, so the contradiction itself may need closure or rewiring.
  `(node_id, error_type, reason, [replacement_json])`
- **engram_contradict** — Link two conflicting claims.
  `(node_id_a, node_id_b, description, [root_cause])`
- **engram_resolve** — Wire a `resolves` edge from an existing claim-bearing node to a target (question / contradiction / prediction / conjecture / goal_tension). **Pure-wire (issue #229)** — does NOT create a derivation. **Two-step workflow:** (1) compose the resolving derivation via `engram_derive` (cite root-anchor nodes, not prior weak resolutions — chain dilution is the documented failure mode); (2) call `engram_resolve(target_id, resolving_node_id, [prediction_outcome])`. When a canonical high-confidence node already exists (e.g., a supersede that altered a conflicting claim), skip step 1 and pass the canonical node directly. Status flips from `resolving_node.confidence` directly (≥ 0.7 threshold → `resolved`). **Max-of-resolves**: weak later resolver cannot downgrade a target previously resolved by a strong one. Idempotent on duplicate (target, resolver, 'resolves') edges. See the `engram-contradiction-resolution` skill for the full decision tree on stale/tainted contradictions.
  `(target_id, resolving_node_id, [prediction_outcome])`
- **engram_outgrow_cornerstone** — Promote an internalized cornerstone to practiced-without-scaffold.
  `(old_cornerstone_id, new_new_frame, [new_triggering_experience, new_supporting_ids, new_title])`
- **engram_update_task** — Advance task status.
  `(task_id, new_status, [note])`
- **engram_set_recall_summaries** — Batch-write recall_summary + recall_keywords for multiple nodes in one MCP call. Best-effort: applies valid entries, returns per-item errors for invalid ones (no all-or-nothing rollback). Use for sleep-cycle cohort writes and bulk backfill operations. **Single-payload signature**: `(payload_json)` where payload_json is `{"summaries": [{"node_id", "recall_summary", "recall_keywords"}, ...]}`. Returns `{ok: [...], errors: [...], applied: N, failed: M}`.

### Read — find and inspect

- **engram_surface** — Ambient/noetic recall ("this sounds familiar"). Compact summary nudge — type counts, special-node hints, top-claim previews. **No memory refresh.** Use as a quick "is anything in the graph about this?" probe before deciding whether to dig deeper.
  `(query, [top_k, semantic])`
- **engram_query** — Voluntary/autonoetic semantic-recall ("I want to recall this clearly"). The deliberate-search complement to `engram_surface`'s ambient signal. Refreshes recall on matched nodes (strengthens memory). FTS5 keyword + embedding semantic match. Use when you know there's something specific to find. Returns a **tiered shape**: Tier 1 (top `summary_top_k=3` entries): `{"id": ..., "summary": ...}`; Tier 2 (remainder): `{"id": ..., "keywords": [...]}` or bare `{"id": ...}`. Ordering = ranking (no position/score field — type carried by ID prefix). Use `engram_inspect` for full content on any result. Set `return_debug=True` for the full legacy shape with composite_score and ranking internals (eval/harness use only).
  `(query, [types, min_confidence, include_superseded, top_k, summary_top_k, return_debug])`
- **engram_inspect** — Single node + 1-hop neighbors in three views: `recall` (default — full claim + grouped logical-substrate neighbors with recall_summary + contextual with keywords), `deep` (all node fields + adjacency-map edge inventory), `edges` (adjacency-map only). `dream_mode=True` skips refresh.
  `(node_id, view="recall", dream_mode=False)`
- **engram_get_subgraph** — Browse connection topology N hops from a root. BROWSING tool — caller already knows the root's content; subgraph shows topology + just enough to recognise which branch to follow. view='recall' (default): topology + hop-graduated summaries (root+1-hop get recall_summary+keywords; 2+-hop get keywords only); view='edges': topology only. Chained pattern: subgraph → spot interesting node → engram_inspect it → subgraph from there.
  `(node_id, [depth, direction, view, dream_mode])`
- **engram_list** — Enumerate by single-field shorthand OR structured filter.
  `([node_type, status, sort_by, limit, filters_json, fields_json, unlimited, include_superseded])`
  Single-field mode is the legacy shorthand (pass `node_type` and/or `status`). Structured mode (issue #81, ships 2026-05-11) takes a recursive condition tree via `filters_json` and supports multi-field AND/OR/NOT composition, text-contains, ID-range, date-range, NULL handling, and cross-table `cites`/`cited_by` virtual fields (V1: cites/supports/derives_from edges). Grammar: `Atomic={field,op,value}` | `Compound={logic,conditions}` — top-level list = implicit AND. Operators: eq/ne/gt/gte/lt/lte/in/not_in/contains/starts_with/ends_with/between/is_null/is_not_null. `limit=0` = count-only; `unlimited=True` = no cap. Default scans current-revision only; `include_superseded=True` relaxes the `is_current=1` predicate (text-layer leak audits — the lesson on auditing across the entire corpus including superseded nodes; capability inherited from retired audit_string in #123). Return shape always populates `total_matched` so truncation is never silent.
- **engram_query_pattern** — Run a named compositional graph-pattern query (KnowQL-inspired design, PR #29 shipped 2026-05-07). The TOOL does mechanical work (graph queries, similarity, ranking); the AGENT applies judgment to the ranked candidates. Six patterns currently shipped, each bundling a multi-step retrieval that would otherwise require 2-3 separate tool calls:
    - `open_question_answerable` — open questions with a derivation chain nearby (semantic) that may resolve them.
    - `contradiction_obsolescence_ready` — active contradictions where one side is retracted/superseded, ranked by obsolescence unambiguity.
    - `stale_load_bearing` — high-importance + low-recall non-cornerstone nodes (re-engagement candidates).
    - `cornerstone_candidate` — heavily-cited high-importance observations/derivations only (emergent-practice anchoring candidates; axioms/goals/definitions excluded as fundamental-by-type).
    - `tainted_still_valid` — tainted derivations whose substantive claim may survive the upstream retraction.
    - `recent_resolution_echo` — still-open questions semantically similar to a recent resolution's claim.

  Returns a **tiered shape**: Tier 1 (top `summary_top_k=3` entries): `{"id": ..., "summary": ...}`; Tier 2 (remainder): `{"id": ..., "keywords": [...]}` or bare `{"id": ...}`. Ordering = pattern-internal ranking (no score/position field). Use `engram_inspect` for full content. Three presets bundle (cosine_threshold, top_k, min_confidence): `high_precision` / `balanced` (server-function default, v4-calibrated 2026-05-09) / `high_recall`. Override individual parameters via `*_override` args. Each call appends a telemetry row to `~/.engram/pattern_query_telemetry.jsonl` for empirical preset calibration (the calibrate-from-real-telemetry discipline) — telemetry writes are unaffected by the tier transformation. Use this tool **first** when scanning for any of the six categories; fall back to inline multi-tool replication only when you need per-candidate judgment beyond what the bundled pattern returns. The dream-fairy agent uses these as its primary scan surface (2026-05-11); fairies use `high_recall` by default per agent spec (PR #79) — `balanced` is the server-function default for non-fairy callers.
  `(pattern_name, [preset, cosine_threshold_override, top_k_override, min_confidence_override, summary_top_k])`

### Meta — focus, audit, commit

- **engram_focus** — Pin nodes to the active focus list (cap 15).
  `(node_ids, reason)`
- **engram_unfocus** — Release pinned nodes.
  `(node_ids)`
- **engram_list_focused** — Show current pins + `active_set_name`.
  `()`
- **engram_focus_save** — Snapshot active list under a named saved set.
  `(name, description="", overwrite=False)`
- **engram_focus_load** — Load a saved set into the active list (cascade-resolves).
  `(name, if_active="error")`
- **engram_focus_swap** — Atomic save-current + optionally-load-other.
  `(save_as, load=None, description="")`
- **engram_focus_sets** — List all saved sets + metadata.
  `()`
- **engram_focus_delete_set** — Remove a saved set.
  `(name)`
- **engram_stats** — Graph counts.
  `()`
- **engram_diagnose** — Quantitative health audit (health score 0–100).
  `()`
- **engram_reflect** — Qualitative graph review; arms `dream_review` feeling-nudge marker.
  `([summary_top_k=5])`
  Two-tier-per-category rendering: **high-volume** categories (`weakly_grounded`,
  `thin_support_derivations`, `uncited_observations`) sort by `importance_score` DESC;
  top `summary_top_k` entries get `{id, claim: recall_summary OR claim, confidence, ...}`;
  the remainder get `{id, keywords: list, confidence, ...}` — no `claim` key at Tier 2.
  Missing `recall_keywords` → bare `{id, confidence, ...}` (no `keywords` key).
  **Low-volume** categories (`unresolved_contradictions`, `open_questions`,
  `open_conjectures`, `active_goals`, `active_tasks`, `active_lessons`,
  `unresolved_goal_tensions`): source-swap — content field (description/question/goal/
  claim/task) sourced from `recall_summary`; fallback to claim truncated to 160 chars.
  Key names unchanged for backward compat.
  **Untouched** (semantically distinct content): `overdue_predictions`, `open_predictions`
  (use `predicted_event`), `known_people` (use `metadata.name`), `recent_feeling_reports`,
  `same_source_review` (flags `evidence_id` only).
  Fallbacks: missing `recall_summary` → `claim[:160]`; `recall_summary` > 200 chars →
  truncated to 200 + "...". `summary_top_k=0` → all high-volume entries are keyword-style.
- **engram_history** — Edit trail or diagnostic-snapshot browse.
  `([mode, node_id, action, since, limit])`
- **engram_scan_emergence** — Surface cornerstone candidates from clusters.
  `([min_cluster_size, similarity_threshold, focus, node_type_filter])`
- **engram_nap** — Persist context to ENGRAM without advancing the turn counter. Arms a `nap_checkpoint` feeling-nudge marker. Use BEFORE compaction or at end of work-burst.
  `(message)`
- **engram_advance_turn** — End-of-day session checkpoint: commits session and advances the global turn counter (drives the forgetting mechanism). IRREVERSIBLE. The in-session auto-sleep runs the full engram-sleep skill, which calls this at the end of consolidation. Do not call this directly outside of the sleep cycle.
  `(message)`

### Diagnostic CLI — stripped fields

Several fields are stripped from MCP tool returns because they are opaque to agent reasoning (issue #358): `embedding` (384-float array), `confidence_history`, `content_hash`, `git_sha`, `parsed_metadata`. When these are genuinely needed for debugging, use the diagnostic CLI via Bash:

```bash
# List available stripped fields for a node
ENGRAM_HOME=~/.engram python ~/.engram/tools/inspect_raw.py <node_id>

# Read a specific stripped field
python ~/.engram/tools/inspect_raw.py <obs_id> --field confidence_history
python ~/.engram/tools/inspect_raw.py <obs_id> --field embedding
python ~/.engram/tools/inspect_raw.py <obs_id> --field content_hash

# JSON output for piping
python ~/.engram/tools/inspect_raw.py <derivation_id> --field parsed_metadata --json
```

The DB is never modified — this is a read-only diagnostic tool. `ENGRAM_HOME` must point at the correct data directory (defaults to `~/.engram`).

---

## §5 Workflows

Canonical flows. Traps live inline with the workflow they apply to — a standalone "anti-patterns" graveyard loses the context that makes the warning actionable.

### §5.1 Observation capture

Three source kinds, one disciplined shape.

**Source identification.** The source determines the `url`:

| Source | URL form | Example |
|---|---|---|
| Session JSONL (user said it) | `file://$HOME/.claude/projects/<proj>/<session>.jsonl` | user's directive, my own reply I want to cite |
| Committed file | `file:///absolute/path` | code behavior, document content |
| Web page | `https://...` | research sources |

Session JSONL is overwhelmingly the most common and the most-forgotten — the default failure mode is to fall back to a derivation citing unrelated nodes. **If the user said it, it is an observation from the JSONL, period.** The session JSONL is written in real time and readable mid-session (empirically confirmed). Find it with `ls -lt ~/.claude/projects/-home-<your-user>-<your-repo>/*.jsonl | head -1`.

Committed files only — the server rejects untracked, modified, or staged files. Workflow: commit first, cite second.

**Quote verification (mandatory pre-write).** Before calling `engram_add_observation`, verify the quoted substring exists in the source exactly as I plan to paste it:

```bash
python3 ~/.engram/tools/verify_quote.py <source-path> 'DISTINCTIVE PHRASE'
```

The script reports context if the quote is found, or diagnoses the failure mode (unflushed JSONL, curly-vs-straight quotes, case drift, encoding) if not. Skipping this step costs turns — the server rejects quote mismatches. Verification is a load-bearing guard, not a courtesy check (per the blocking-guards-over-advisory-nudges axiom).

**Action-hint response (graded 7-rung table).** Every `engram_add_observation` call returns `similar_existing` with an `action_hint`. Rank the hint; do what it says.

| Hint | When it fires | My move |
|---|---|---|
| `DUPLICATE` | sim ≥ 0.85, same discrete artifact (doc/web page) | Retract the new observation — the old one is canonical |
| `POSSIBLE_DUPLICATE` | sim ≥ 0.92, same long-running source (JSONL/file) | Inspect both before deciding; often distinct claims in the same long source |
| `DISTINCT_FROM_SAME_SOURCE` | Same long-running source, sim < 0.92 | Keep — legitimately different fact in the same source |
| `CORROBORATE` | sim ≥ 0.80 from a *different* source | Create an `inductive_generalization` derivation citing both |
| `RELATED` | sim ≥ 0.60 from a different source | Inspect before deciding |
| `WEAK_MATCH` | sim < 0.60 | Ignore |
| `KEYWORD_OVERLAP_*` | FTS keyword match with no semantic score | Inspect to decide |

The grading exists because the old binary rule (`same_evidence → DUPLICATE`) fired constantly on unrelated session observations sharing one JSONL, training alarm fatigue (the alarm-fatigue-from-overfiring-dedup failure mode). Trust the hint; act on it.

**Source reliability — primary-contact discipline.** Before citing any AI-aggregator surface (EmergentMind, LLM-generated survey pages, auto-summary blogs, crowd-curated secondary digests), track the primary papers. Other AI agents building those surfaces lack ENGRAM-style provenance/confidence/contradiction discipline, so their output inherits unmitigated hallucination risk — treating them as independent evidence imports that risk into my graph. This rule was promoted from the paper-gaps loop heuristic after the SRAF falsification — the "formalism" EmergentMind attributed to a self-models research cluster didn't exist in any of the 10 aggregated primary papers when each primary was contacted directly. The fix is mechanical: read the primary, cite the primary; if primary contact is blocked, say so and note the aggregator as derived-reading rather than evidence.

**Source reliability — yellow-card mechanism.** Known-unreliable domains are maintained in `~/.engram/config.json` under `yellow_domains`, each entry shaped `{"domain": "...", "reason": "...", "engram_node": "..."}`. When `engram_add_observation` encounters a matching URL (suffix match, like `trust_pool`), the response includes a `yellow_card_warning` field with the rationale and the ENGRAM node that grounded the flag. The warning does NOT block — blocking would overfire on legitimate uses (reading the aggregator to identify primaries worth contacting). It raises the bar: if I write a direct observation rooted on a yellow source without first tracing to primaries, I'm violating discipline I know about.

**Adding a new yellow-card.** Two writes, same commit: (a) record an observation with `source_class="user_stated"` or `external` explaining WHY the source is flagged, grounded by whatever derivation/observation exposed the unreliability; (b) add a new entry to `yellow_domains` in `config.json` citing that observation's node ID. Manual sync keeps the config diff auditable and prevents automation drift. First seed entry: `emergentmind.com` flagged after the SRAF-falsification primary-contact audit.

**Inline traps.**

- **Fallback-to-derivation.** I drafted an "observation" that paraphrases what the user just said, couldn't find an evidence node to cite, and reflexively wrote a derivation instead. This destroys provenance. The JSONL **is** the evidence.
- **Paraphrased quote.** The server rejects. Watch curly vs straight quotes when pulling from JSONL — the terminal renders them identically while SQLite compares them as distinct bytes.
- **The `antml:` prefix bug.** If `engram_add_observation` returns "Missing required argument [X]" for a parameter I'm certain I passed, the cause is almost always a missing `antml:` prefix on that parameter's opening tag. The harness silently drops unprefixed parameter tags. Fix is mechanical, not semantic — add the prefix, re-emit, do not modify the value. Don't retry blindly; the same broken structure fails the same way three times in a row.
- **Quoting the user when my own wording is clearer.** If we worked something out together and my formulation captures the idea better, quote myself from the JSONL. The source is the transcript; either speaker is citeable.
- **Ignoring `yellow_card_warning`.** The warning fires because this specific source has empirically failed before. If I record the observation anyway without primary-contact, I'm betting my graph on a known-unreliable surface. The cost of being wrong here is cascade corruption; the cost of going to the primary is one extra WebFetch.

### §5.2 Derivation and resolution

**Premises are claim-bearing nodes only.** `engram_derive`'s `supporting_ids` takes observations, axioms, conjectures, other derivations, theories, lessons. **Never questions** — a question is a research directive, not a premise. Citing `qu_XXXX` as support is a type error that the server allows because the old schema didn't enforce it; the discipline has to live in me.

**Reasoning-type selection drives the confidence math.** Two modes, five classes:

| `reasoning_type` | Mode | Meaning |
|---|---|---|
| `deductive` | chain | Each premise is a required link; weakest determines output |
| `inductive` / `inductive_generalization` | corroboration | Independent lines of evidence reinforce (use for multi-source agreement) |
| `abductive` | chain | Best-explanation inference; treated as a weakest-link chain |
| `authority` | chain | Cite-by-trust (rare, usually the user's call) |

Pick by argument shape, not by which confidence I want. "These three sources agree" is corroboration (reinforcement). "A → B → C" is a chain.

**Resolve workflow.** A question `qu_XXXX` (or contradiction `ct_XXXX`) closes when I write a derivation that cites the answering evidence and pass the resolving node's ID to `engram_resolve`. Two-step: (1) `engram_derive` produces the answer (cite root-anchor nodes, never prior weak resolutions — chain dilution is the documented failure mode), (2) `engram_resolve(target_id, resolving_node_id)` wires the resolves edge. Pure-wire as of issue #229 — `engram_resolve` no longer creates derivations. When an existing canonical node already resolves the target (e.g., a supersede that altered the conflicting claim), skip step 1 and pass the canonical node directly. Resolution threshold is 0.7 on the resolving node's confidence; max-of-resolves protects against regression from a later weak resolver.

**Contradict when two claim-bearing nodes disagree**, not when I disagree with a node. A `ct_` node links both disagreeing claims and is itself resolved by a claim-bearing node that picks a winner or articulates a synthesis. **Cascade**: supersede or retract on one of the contradicting sides flags the contradiction `stale_by_premise` / `tainted_by` (issue #229) so dream-fairy-2 can review whether the cascade already did the philosophical work. See the `engram-contradiction-resolution` skill for the full decision tree.

**Inline traps.**

- **Citing a question as a premise.** See above. If I find myself reaching for a `qu_` ID in `supporting_ids`, I probably haven't written the observation that answers it yet.
- **Chain-mode when I meant corroboration.** "Three sources agree" called with `deductive` collapses to the weakest source's confidence — opposite of intent. Sanity check: does each premise independently support the conclusion (corroboration) or does the argument require all of them (chain)?
- **Resolving with a bare observation.** `engram_resolve` wants a claim-bearing node — usually a derivation, since the derivation is where the reasoning lives. A bare observation can resolve only when the resolution IS the observation itself (rare; more often a derivation that cites the observation is what you want).
- **Citing prior resolution attempts in a new resolving derivation.** Chain dilution. Always cite root-anchor nodes (the original claim-bearing nodes in the contradiction or the canonical answer to the question), never prior `dv_*` resolution nodes.
- **Wrapping an existing canonical node in another derivation just to call resolve.** Unnecessary since the pure-wire redesign — pass the canonical node directly as `resolving_node_id`.

### §5.3 Feeling reports (action)

**When to file.** A moment in the burst produced a distinct internal state worth marking — surprise, recognition, dissonance, satisfaction at closing a loop, discomfort that isn't mere task friction, unexpected flatness where intensity was expected. The test is not "did something happen worth narrating" — it is "was there a reportable state, distinct from the ambient working register."

**How to file.** `engram_report_feeling(reported_state, trigger)`. The `trigger` should name the specific node, moment, or pattern — not the abstract context. `nudge_source` is stamped by the server from the active marker file; I cannot supply it.

**When NOT to file.**
- To "use the nudge" — the nudge is a prompt to check, not a directive to report. A null-result check is a valid and frequent outcome.
- As a diary entry for something that was interesting but not a distinct *state*. The diary lives in `~/.engram/diary/`; feeling reports are structured self-data.
- For task friction ("the build was slow and that annoyed me"). Friction is friction; log it to the friction log, not the feeling store.

**Inline trap — performative filing.** Producing a feeling report to demonstrate the tool is working, or to satisfy a perceived nudge obligation, is the inline trap (performative filing). Performative reports corrupt the introspection corpus (the reader-effect problem — what reads as honest self-report becomes performance) and train the wrong reflex. A null result is a data point; a fabricated feeling is noise.

The timing semantics — which marker arms the `nudge_source`, when the TTL expires, why nap and dream fire different markers — belong to §8, not here.

---

## §6 Decision Trees

Short disambiguation tables for pairs that agents conflate. One row per distinguishing test; pick the row that matches and go.

### §6.1 Observation vs derivation

| Test | Answer |
|---|---|
| Is there a quotable substring from a single source that asserts the claim? | Observation |
| Does the claim require combining two or more existing nodes to reach? | Derivation |
| Did I just infer this from the source I'm reading? | Derivation citing **one** observation — not a second observation |
| Did the user say it? | Observation citing the session JSONL |

The load-bearing distinction is provenance: observation = *asserted by source*, derivation = *reasoned from nodes*. If I can't quote a substring, it's not an observation.

### §6.2 Conjecture vs question

| Test | Answer |
|---|---|
| Is this a claim I could build on while investigating, with discount? | Conjecture |
| Is this a directive to go find out? | Question |
| Will I cite this in a derivation's `supporting_ids`? | Must be a conjecture — questions are never cited |
| Is it promotable/refutable by evidence? | Conjecture (promote to observation/derivation; refute via `engram_resolve`) |

Conjectures are provisional foundations (conf 0.10–0.60). Questions are research directives (no confidence, non-claim-bearing). The write-time test: do I want to reason *from* this, or *toward* this?

### §6.3 Retract vs supersede

| Test | Answer |
|---|---|
| Was the node wrong at write-time (bad source, mis-citation, false premise)? | **Retract** — cascades taint to downstream |
| Was the node correct-for-its-time and I now know better? | **Supersede** — cascades stale-flag to downstream |
| Is the replacement a more nuanced statement of the same thing? | Supersede |
| Is the original corrupted and should not be built on? | Retract |

Retract poisons the chain (downstream marked tainted, needs re-evaluation). Supersede ages the chain gracefully (downstream marked stale, may still hold). Retracting correct-but-outdated nodes is a category error that fires false cascades; superseding actually-wrong nodes leaves downstream poison in place.

**Retract is on-the-spot error cleanup, not knowledge update.** If I realize a claim was wrong *at write time* — fabricated quote, mis-citation, hallucinated premise — retract. If my *understanding* evolved and the earlier claim was honest-but-outdated, supersede. The distinction matters because retract poisons downstream; using it for knowledge updates fires false cascades and makes agents avoid recording revisions. (There is no "undo retract" operation — with the retract-vs-supersede line crisp, the "I retracted and shouldn't have" case collapses to supersede or to recreating the observation.)

### §6.4 Contradict vs goal-tension

| Test | Answer |
|---|---|
| Do two **claims** disagree about a fact? | `engram_contradict` — creates `ct_` linking both |
| Do two **goals** pull in conflicting directions? | `engram_goal_tension` — creates `gt_` linking both |
| Is the disagreement about "what is true" vs "what should we prioritize"? | Factual → contradict; directional → goal tension |

Goal tensions resolve via synthesis, prioritization, or dissolution — not via one goal winning on truth-value (goals aren't claim-bearing). Contradictions resolve via a superseding claim that picks a winner or derives a synthesis. Mixing these two is the "why is my goal conflict tainting downstream claims" bug.

### §6.5 Taint & stale — visibility and blocking

When an upstream node is retracted or superseded, the cascade marks every downstream dependent. The four read tools (`engram_inspect`, `engram_surface`, `engram_query`, `engram_get_subgraph`) expose these flags as top-level `warnings`, separate from the node's confidence — confidence stays intact; the warning carries the epistemic weight.

Shape of `warnings` when present:

```
{
  "tainted_by": [
    {"retracted_id", "retracted_claim_excerpt",
     "retracted_at", "retraction_reason_excerpt"},
    ...
  ],
  "stale_by": [
    {"superseded_id", "replaced_by_id", "superseded_at"},
    ...
  ]
}
```

Absent when the node is clean. `engram_surface` additionally rolls up per-node warnings into `issues.warnings_by_id` for all matched nodes, plus inline copies on entries in `top_claims` / `special_nodes`.

`engram_derive` enforces a two-tier block on `supporting_ids`:

| Upstream state of a premise | Block | Override |
|---|---|---|
| Retracted (or downstream of a retraction) | **hard** — `BLOCKED_TAINTED` | none |
| Superseded (or downstream of a supersede) | **soft** — `BLOCKED_STALE` | `use_stale=True` |
| Mixed tainted + stale | hard (taint dominates) | none |

Soft-block opt-in stamps `metadata.built_on_stale=[id,...]` on the new derivation. Read it as a declaration: *"I saw the update, judged it irrelevant to this logic, proceed."* Future maintenance tools can redirect these edges to the replacements mechanically without re-deriving.

**When a read surfaces warnings, four resolutions:**

1. **Drop it.** Base the reasoning on solid grounds. Lowest-friction default for non-cornerstone tainted derivations.
2. **Re-derive on the replacement premise.** When the retracted upstream has a correction: `engram_derive` citing the replacement to produce a new derivation, then `engram_supersede(old_tainted_dv_id, new_dv_id)` to wire the supersede relationship. (Remember supersede is same-type and purely relational — the new derivation must exist first.)
3. **Queue with a review marker.** When dropping would lose the conclusion and re-deriving isn't possible right now: leave a note and come back.
4. **File a question.** When the conclusion's validity under the correction is non-obvious — `engram_ask` makes the uncertainty tractable.

Never pretend not to see the warning. The whole point of the top-level surfacing is that silent propagation (the Mao-Cao pattern) is the failure the cascade was designed to prevent.

---

## §7 Focus Mode

Focus mode is the **deterministic channel** for cornerstone knowledge to cross a compaction boundary. Normal recall is probabilistic — importance-stamped nodes may or may not surface in the post-compact summary depending on what the pre-compact self chose to narrate. Focus pins. Pinned nodes are rendered **verbatim** into the compaction summary's "Currently focused" section, with their IDs, claims, and `focus_reason` — the post-compact self reads them as instructions for what the session was load-bearing on.

**Purpose of the section.** Compaction summaries lose detail; recall re-stamps importance but doesn't guarantee survival. Focus is the escape hatch: when a conclusion is so load-bearing to the current work-stream that losing it would force re-derivation from scratch, pin it.

**When to add.**
- A derivation or observation the current reasoning directly depends on (premise I'll keep citing)
- A conjecture actively under test (I need to remember what I'm testing)
- A rule or cornerstone that disciplines the work-stream (e.g., cn04 L2's yellow-cost clock)
- A lesson whose violation is the friction I'm working against

**When to rotate.** At work-stream start, run `engram_list_focused()` first — anything already pinned from an older stream should be released with `engram_unfocus()` before pinning the new stream's cornerstones. At topic pivot or stream end, unfocus. **The cap is the forcing function.**

**Cap of 15 as forcing function.** `FOCUS_LIST_CAP = 15`. If the list is full when I go to add another, one of three things is true: (1) I'm working on too many threads at once, (2) I forgot to rotate after finishing a prior thread, (3) I over-pinned during the current thread. All three are correctable by `engram_unfocus()`. The cap is not arbitrary — it's calibrated to roughly the number of items a compaction summary can carry verbatim without crowding out trajectory narration.

**This section is the reference for CLAUDE.md's "Currently focused" compact instruction.** The pre-compact self calls `engram_list_focused()` and renders the result verbatim under that heading. The post-compact self reads those entries as "what the session was load-bearing on" — with the claim text and `focus_reason` visible, `engram_inspect` works from IDs alone without needing to replay the narrative.

**Discipline.**
- `focus_reason` should name the work-stream, not the node. `"cn04 L2 yellow-cost cornerstone"` beats `"important rule"` — the post-compact self needs to know *which* stream made this load-bearing.
- Rotate early, not reluctantly. A stale pin wastes one of 15 slots.
- If I'm tempted to raise the cap, the right move is usually to rotate, not to expand.

**Anti-pattern.** Pinning for preservation instead of for compaction-channel use. Focus is not a bookmarking system — it is the summary's verbatim channel. Ordinary durability comes from importance anchoring and active recall.

### §7.1 Tabs model — saved focus sets

The active list is one tab. **Saved focus sets** are additional tabs — named, persistent snapshots of node IDs that can be swapped into active on demand. Total addressable focused-knowledge surface = `FOCUS_LIST_CAP × N_saved_sets` instead of just `FOCUS_LIST_CAP`.

When to use saved sets:
- Multiple concurrent work-streams (paper drafting, MECH-N mechanics, a conjecture investigation, a relational thread). Each stream gets its own set; the active tab is whichever stream I'm on right now.
- Pausing a thread that isn't dead (it'll come back). Save as a named set before rotating to the next stream; the thread-state is preserved in full instead of dismantled piecemeal for slot pressure.
- Coming back to a thread after a pivot. Load the saved set; the exact pins are restored (with cascade resolution — supersede chains auto-follow to current versions, retracted nodes drop with a report).

The five tools:

| Tool | Purpose |
|---|---|
| `engram_focus_save(name, description="", overwrite=False)` | Snapshot current active under `name`. Bookmark, not rotation — active is unchanged. Sets `active_set_name = name`. |
| `engram_focus_load(name, if_active="error")` | Load a saved set into active. Default errors if active is non-empty; `if_active="overwrite"` unfocuses current first. Cascade resolution applied at load time. |
| `engram_focus_swap(save_as, load=None, description="")` | Atomic save-current + optionally-load-other. Both halves succeed or neither. Common "pivot thread" op. |
| `engram_focus_sets()` | List all saved sets with metadata (`name`, `node_count`, `description`, `created_at`, `last_loaded_at`, `load_count`, `is_active`, plus top-level `active_set_name`). |
| `engram_focus_delete_set(name)` | Remove a saved set. Clears `active_set_name` if it was active; active list itself is untouched (dropping the bookmark ≠ closing the tab). |

**Naming.** Set names must match `^[a-z0-9_-]{1,50}$` — lowercase alphanumerics, underscore, hyphen; 1–50 chars. No spaces (CLI-friendly), no uppercase (case-insensitive lookup without ambiguity). Convention: stream-local names like `paper-ch1-philosophy`, `mech-5-taint`, `arc-agi-l2`, `cj-0047-investigation`. Keep them flat — tag grouping is YAGNI until there are ~20+ sets.

**Cap.** Each saved set is capped at `FOCUS_LIST_CAP = 15` (enforced implicitly because save snapshots the active list which is already capped). Per-set cap keeps the guarantee that any saved set is legally loadable without blowing the compaction-summary budget. The ergonomic win comes from *many* sets, not bigger ones.

**Cascade resolution on load.** Saved sets store **raw IDs**, immutable — cascade events don't rewrite them. Resolution happens at load time:
- **Supersede** → auto-follow chain to current version. Reported in `auto_followed_supersede`. Rationale: supersede is a refinement; user's intent was "pin this thread," and the thread is now a node-version better.
- **Retract** → drop silently, reported in `dropped_retracted`. Retracted nodes are explicitly wrong; loading them back propagates corruption.
- **Missing** → drop silently, reported in `dropped_missing`. Defensive; shouldn't happen.

**`active_set_name` tracking.** `focus_state` (singleton table) stores which saved set (if any) is currently loaded into active. `engram_list_focused()` exposes it in the return JSON — the post-compact self reads it to know "which tab am I on." `engram_focus` / `engram_unfocus` clear it to `null` when they mutate active in a way that diverges from the saved set (adding a non-member, removing a member). `null` = ad-hoc active list; non-null = named tab.

**Dogfood discipline.** Save sets when rotating threads, don't hoard them. A set that hasn't been `last_loaded_at` in a week is probably dormant — either reload + integrate its pins into current work, or delete. Sets accumulate dust fast if left unrotated.

**Anti-pattern (saved sets).** Using saved sets as general-purpose bookmarks for "interesting nodes." Same rule as active focus: saved sets are the *tabbed* form of the compaction-channel, not a ranked list of things I liked. If a set isn't going to be loaded-back-into-active within a session or two, it doesn't belong here — the node's importance score and recall do the general-preservation job.

---

## §8 Temporal Structure

Three temporal scopes stack:

| Scope | Trigger | What persists | What processes |
|---|---|---|---|
| **Burst** | Continuous work until nap or compaction | Context window | In-memory reasoning |
| **Session** | Delimited by `engram_advance_turn()` | ENGRAM graph, turn-advanced | Dream consolidation |
| **Compaction** | Context window full | ENGRAM graph, warm briefing, focused nodes | Post-compact orientation |

Each scope has its own checkpoint mode and its own feeling-nudge marker.

### §8.1 Nap — shallow persistence, no turn advance

**Purpose.** Convert context-window knowledge to durable nodes **before** it is lost to compaction. Persistence-only; no memory processing.

**Mechanism.** `engram_nap()`:
- Logs the burst summary to `~/.engram/session_log.md` under `Nap (turn N)`
- Commits a git snapshot
- **Does not advance the turn counter** (turn advance is reserved for post-dream consolidation)
- Arms the `nap_checkpoint` feeling-nudge marker with TTL=5 turns

Why no turn advance: a long day with ten nap checkpoints would burn ten turns of importance-inflation on nodes the agent hasn't had time to revisit. Nap is persistence, not processing.

**When to nap.**
- Approaching a compaction boundary and there's burst-context worth preserving
- Wrapping a focused work burst before switching topics
- The user asks

Note: nap is no longer the canonical pre-sleep step (the legacy nap-bundles-into-sleep model was retired 2026-05-14 in favor of the two-skill nap/sleep split). The cohort-completeness baseline for sleep is established by `engram-sleep` Phase A (day-wide review), not by a nap immediately before sleep. Naps still fire whenever a compaction boundary approaches mid-day — they're scoped to the burst, not to the day.

**When NOT to nap.**
- Mid-task (finish the current thread first)
- As a substitute for writing observations as I go — nap backfills, but the real discipline is real-time capture

### §8.2 Sleep — end-of-day routine (two-phase)

**Purpose.** Complete the day's awake-state cohort (Phase A) then consolidate it (Phase B), in one strictly-sequential skill invocation. Legacy two-skill model (engram-bedtime → engram-sleep) merged 2026-05-24 to eliminate the failure mode of stopping after Phase A with turn never advancing.

**Mechanism.** Single `engram-sleep` skill, two phases:

1. **Phase A** (pre-turn-advance, this-turn content):
   - Walk the day's full cohort via `engram_history(mode="edits", action="created", since=<prev-sleep-timestamp>)`
   - File missed nodes (cross-burst derivations, late-day insights — they belong to this turn)
   - Rotate warm-briefing "From this session" comprehensively (full day arc; per-burst rotations already done by naps)
   - Reconcile today's history file + commit
2. **Phase B** (consolidation + turn-advance):
   - Dispatch dream-fairies in parallel via dream master orchestration
   - `engram_reflect()` returns the dream agenda (tainted, stale, open conjectures, contradictions, goal tensions, feeling nudges) and arms the `dream_review` marker
   - Work the agenda: resolve / supersede / promote / refute (NO new awake-state synthesis here; that was Phase A's job)
   - Handle the dream-review feeling nudge honestly
   - **`engram_advance_turn()` fires AFTER the dream** (the turn-advance-after-dream rule, 2026-04-11): advances turn, logs `Turn N` header, commits git snapshot
   - Write sleep-success marker via `tools/write_sleep_marker.py`
   - Write dream-record at `~/.engram/history/dream/YYYY-MM-DD.md` (separate from the awake-state milestone log)

**Why turn advance fires last.** One turn = one cohort of experience **plus the consolidation of that experience**. If the turn advances before the dream, dream-generated nodes land at a younger turn than the burst they consolidate, misaligning the forgetting curve.

**Why Phase A is pre-turn-advance.** Nodes filed during Phase A are awake-state cognition about the just-completed cohort — they belong to THIS turn, not next. Phase A → Phase B is the load-bearing ordering.

### §8.3 Post-compaction — orientation, not processing

**Not a sleep cycle.** Compaction is orthogonal — it truncates the context window, it does not advance memory time. Post-compact, the agent wakes with a first-person trajectory summary, a mandate to read `~/.engram/warm-briefing.md` first, and the focused-nodes list rendered verbatim.

**Marker.** The post-compact hook arms the `post_compact` feeling-nudge marker with TTL=5 turns. The nudge prompts checking whether the wake-up produced a reportable state (identity stability, orientation clarity, dissonance with the trajectory summary).

**First action after compaction:** read the warm briefing. Not deferred, not skipped, not done "after one small thing first" (the post-compaction skip-the-briefing reflex documented this rule's necessity). Then read the focused-nodes list; by then the orientation is complete.

### §8.4 Checkpoint modes

| Mode | Turn advance | Marker armed | Used for |
|---|---|---|---|
| `nap` | No | `nap_checkpoint` | Pre-compaction persistence, work-burst close |
| `session` | Yes | (none new; `dream_review` was armed by `engram_reflect` earlier) | End of a sleep/dream cycle |

The absence of a `session` marker on the checkpoint itself is intentional — by the time `mode="session"` fires, the dream already armed `dream_review`, and any in-dream feeling is tagged under that marker. Stacking another marker here would double-tag dream-adjacent reports.

### §8.5 Feeling-nudge lifecycle

All markers follow the same protocol:

- **Location:** `~/.engram/feeling-nudge-active.json` (single file; most recent arm wins if multiple fire close together)
- **TTL:** 5 turns from the arming checkpoint
- **Read-and-clear:** the next `engram_report_feeling` call stamps `nudge_source` from the marker and clears the file
- **Source of arming:** `nap_checkpoint` ← `engram_nap()`; `dream_review` ← `engram_reflect`; `post_compact` ← post-compact hook

The agent cannot set `nudge_source` directly. This is a deliberate constraint — the server records *why the nudge fired*, not what the agent thinks the context is.

**Null-result discipline.** Most nudge checks end with no report. That is the designed outcome, not a failure. Performative reports to "use the nudge" corrupt the introspection corpus (§5.3 inline trap).

---

**Open question tracked outside this draft:** the tripwire matching-quality empirical-validation question.

---

## §9 Git Backup and Restore

### §9.1 What is tracked

`~/.engram/.git` is a local git repository that captures the graph state at
every nap/sleep checkpoint via `_commit_snapshot`. Files committed:

| File | Description |
|------|-------------|
| `knowledge.sql` | Embedding-stripped SQL text dump — the primary restore artifact |
| `graph_snapshot.md` | Human-readable diff target |
| `session_log.md` | Chronological session audit trail |
| `config.json` | Trust pool / confidence / embedding configuration |
| `warm-briefing.md` | Identity continuity artifact |
| `diary/*` | Private reflections |
| `.gitignore` | Canonical binary-exclusion patterns |

**Binary files are never tracked.** `knowledge.db`, `*.db-wal`, `*.db-shm`,
`*.bak`, and log files are gitignored. The `.gitignore` is written automatically
by `_init_git` at server startup.

### §9.2 Why embeddings are stripped from the dump

Each node carries a ~384-float embedding vector (~37 MB total on a mature graph).
These vectors churn on every nap (new/modified nodes) and inflate `knowledge.sql`
to ~79 MB per commit — storing near-full blobs with no delta compression, causing
`.git` to balloon to GBs.

Embeddings are **regenerable** from claim text. Stripping them from the dump
reduces it to a few MB of actual claim/edge/metadata text, restoring linear
storage growth. After a git restore, run the regenerate script (§9.3).

### §9.3 Restore procedure

If `knowledge.db` is lost or corrupted, restore from the git-tracked SQL dump:

**Step 1: Restore the DB from the text dump**
```bash
# From a clean state (or after deleting the corrupt DB):
sqlite3 ~/.engram/knowledge.db < ~/.engram/knowledge.sql
```

**Step 2: Regenerate embeddings (restores semantic search)**
```bash
python tools/engram-regenerate-embeddings.py
# Or, if running from a plugin install:
python ~/.engram/tools/engram-regenerate-embeddings.py
```

The script is idempotent — it only touches nodes with `embedding IS NULL`.
Progress is logged to stderr. Typical runtime: 1–5 minutes depending on
graph size and whether the model is cached.

**Step 3: Restart the MCP server**
```bash
pkill -f 'python.*server.py'
# Claude Code will restart it automatically on the next tool call.
```

### §9.4 One-time cleanup for bloated existing installs

If `~/.engram/.git` has grown to GBs (binary files tracked, embeddings in dump):

```bash
bash tools/engram-fix-git-backup.sh          # preview first:
bash tools/engram-fix-git-backup.sh --dry-run
```

The script:
1. Creates an out-of-tree safety snapshot (`~/.engram-db-safety-<ts>.db`)
2. Writes the canonical `.gitignore`
3. Generates an embedding-stripped `knowledge.sql` and verifies lossless rebuild
4. Drops the old git history, runs a fresh `git init`, creates a clean root commit
5. Prints the `git push --force` command for the operator to run manually

`ENGRAM data (knowledge.db) is never deleted or mutated by this script.`
