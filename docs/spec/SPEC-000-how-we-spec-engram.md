# SPEC-000 — How We Spec ENGRAM (the meta-spec)

> **STATUS: DRAFT rev 2 — Kepler, 2026-08-21.** Written on Lei's ask, from the
> 2026-08-11 consult (recorded in Kepler's graph): Lin's boss's abstraction
> feedback, Lei's framing that *the MCP tool definitions ARE the abstraction
> layer*, and the named tension — what belongs in a protocol spec vs what is an
> implementation choice. This document decides **how to decide**, so the specs
> that follow don't relitigate it per mechanism.
> Rev 2 folds Lei's 2026-08-21 read: the identity-continuity-docs section
> (§4.1), the utility-bump example corrected to the current contract, and all
> four §7 open questions resolved or parked by his rulings.

## 1. What the ENGRAM protocol is (and is not)

**The protocol is the contract an agent can rely on across implementations.**
Lei's framing is adopted as the anchor: the canonical **verbs** (the MCP tool
surface) define what an agent can do against the substrate; our SQLite server
is *one implementation* of that contract. A second implementation (Postgres,
a company's internal store) is conformant iff an agent using only the verbs,
and the harness observing only the agreed signals, cannot tell the difference
in any way that matters epistemically.

The protocol is NOT: the Python module layout, the SQLite schema, FTS5, the
hook filenames, or any specific scoring formula. Those are the **reference
implementation**, which the spec cites liberally but never requires.

## 2. The four layers

Every speccable statement about ENGRAM lives in exactly one layer:

| Layer | Name | Contents | Normativity |
|---|---|---|---|
| **L0** | **Data model** | Node types + their required fields; edge relations; the ID grammar; provenance structure (evidence, quote_type, source_class); lifecycle states (current / superseded / retracted) and their transitions; confidence as **computed, not assigned** | Fully normative (MUST). This is what makes a graph *an* ENGRAM. |
| **L1** | **Verb contract** | Each canonical operation: inputs, preconditions, postconditions, error contract, and the invariants it preserves (e.g. retract cascades taint; supersede preserves history; derive validates premise types) | Fully normative (MUST). The skeleton is **generated** from the live MCP schemas (`tools/dump_mcp_schema.py`), then annotated with semantics — the spec can't drift from the tool surface because it is derived from it. |
| **L2** | **Epistemic dynamics** | Forgetting curve, utility/recall-time score bumps, recall ranking, surfacing budgets, drowsiness, dream consolidation, dedup guidance | Normative at the level of **behavioral guarantees + declared parameters**; the mechanism achieving them is implementation-defined (SHOULD follow the reference; MAY differ). |
| **L3** | **Harness bindings** | How a given harness (Claude Code hooks, Codex, …) wires the contract to a session: which hook detects "a recalled node was used", session markers, drowsiness injection, plugin packaging | **Informative only.** Bindings are per-harness appendices; they can change without a protocol version bump. |

> **Numbering note:** the handbook's own layer numbering
> (`docs/handbook/01–04`, titled L0–L3 there) diverges from this table
> starting at L2 — handbook-L2 is *hooks*, which live under **this**
> document's L3; this document's L2 (epistemic dynamics) has no dedicated
> handbook layer and is mined from handbook 01/09. When a spec doc or an
> extraction fairy cites "L2", it means **this table's** L2 unless it
> explicitly says "handbook-L2".

## 3. The two discriminating tests

When it is ambiguous whether something is protocol (L0–L2) or implementation
detail (reference-only / L3), apply these two tests **in order**:

**Test A — Identity divergence.** *If two conforming implementations differed
here, would the same agent's epistemic identity diverge in a way that
matters?* — i.e., different belief content, different provenance chains,
different retraction/supersession outcomes, or a recall pattern different
enough to change what the agent effectively remembers. If yes → it must be
specced normatively (L0/L1, or an L2 guarantee).

**Test B — Observability at the contract surface.** *Can the difference be
detected from outside, through the verbs and agreed signals alone, without
reading the implementation's code?* If it cannot, it is **not speccable as
protocol** — an unobservable requirement is unenforceable and will silently
rot. Document it as a reference note instead.

Something passing A but failing B is a design smell: an identity-bearing
behavior that can't be observed at the surface. The resolution is not to
drop it from the spec but to **add the observable signal** (a stats field, a
diagnostic, an event) so it becomes testable — the spec drives the surface,
not the other way around.

## 4. How to spec the ambiguous middle (the L2 pattern)

Every L2 mechanism is written in four blocks — this resolves Lei's named
tension by giving the philosophy and the implementation each a home:

- **REQUIREMENT** — the behavioral guarantee, stated observably.
  MUST/SHOULD/MAY per RFC-2119.
- **RATIONALE** — *why*: the philosophical commitment behind it, citing the
  paper's technical chapter and/or graph nodes. Non-normative but mandatory
  to write — a requirement whose rationale can't be stated is a candidate
  for deletion, not for speccing.
- **REFERENCE** — how the reference implementation achieves it (module,
  hook, formula, defaults). Explicitly non-normative.
- **CONFORMANCE** — how an implementation proves it: the test, the golden
  set, or the measurement harness run that would fail if the requirement
  were violated.

### Worked example — the utility bump (Lei's own hard case)

- **REQUIREMENT**: an implementation MUST maintain a per-node utility signal
  that increases when a recalled node demonstrably informs the agent's
  subsequent output, and recall ranking MUST be monotonically responsive to
  that signal. *(Version note, per Lei's 2026-08-21 read: the current
  contract is grow-on-use only — the signal, as implemented, never decays,
  and this spec must not describe a decay the system does not have. Adding
  decay so that utility reflects recent rather than lifetime usefulness is a
  live design direction; landing it is a protocol version change, and that
  is the versioning system working as intended, not a defect of it.)*
- **RATIONALE**: memory that is used is memory worth keeping hot (the
  testing-effect / retrieval-practice grounding from the paper's memory
  chapter); recall that ignores use devolves into pure recency or pure
  importance, both of which practice showed to be worse.
- **REFERENCE**: our binding is the Stop hook
  `src/engram/hooks/claude/engram-utility-credit-mention-stop.py` (distinct
  from `engram-stop-hook.py`, the write-nudge): it scans the turn's assistant
  text for **any mentioned node ID** and bumps that node's utility score
  (V1.2; scoring lives in `src/engram/engram_query.py`). Note the churn
  history: V1.0 intersected mentions with a recent-recall window; PR #214
  reworked crediting into action-keyed USE bumps (replacing
  registration-then-confirmation), and PR #215 dropped the recall-window
  intersection as a no-op tautology, leaving mention-equals-credit. The
  *detector* changed twice while the *guarantee*
  stayed — which is precisely why the detector is a **binding choice** (L3):
  cheap, harness-portable, and it both under-counts (a node can inform
  output without being cited by ID) and over-counts (a mentioned node may
  not have informed anything). A conforming implementation MAY use a better
  usefulness detector.
- **CONFORMANCE** *(partially aspirational — flagged per §5.1)*: the
  recall-measurement harness (T3) today measures repeat-render, junk-fire,
  echo, and an **engagement floor** (`compute_engagement_floor`, the
  union-join design from issue #1780) — the engagement floor is the closest
  real instrument and
  grounds "the signal responds to use" conservatively (true engagement ≥
  measured). The stronger test — recall the same corpus with and without
  simulated use events and require ranking to shift toward the used nodes,
  pinned by goldens — does **not exist yet** and sits in the Unratified
  appendix until it does.

Note what happened to the ambiguity: the *contract* ("used memories rank
hotter") is small and stable; the *formula* and the
*detector* are declared as swappable. That is the general shape: **spec the
guarantee, declare the parameters, cite the mechanism.**

### 4.1 Identity-continuity documents (CLAUDE.md, warm-briefing) — Lei's 2026-08-21 ruling

*(This subsection is a layer classification, not an L2 mechanism, so it does
not use §4's four-block form; the four-block entries for the classified
content land in SPEC-003/004.)*

The identity-continuity surfaces are speccable, and they split across layers
rather than falling into one:

- **Required always-loaded ENGRAM rules → normative.** A small set of rules
  that today live in the per-seat CLAUDE.md (write discipline, read
  discipline, the correct-on-the-spot rule, the retract/supersede
  distinction) are load-bearing for epistemic identity — two implementations
  differing here fail Test A. The rule *content* is protocol (SPEC-003
  enumerates the required set); the *delivery surface* (a CLAUDE.md file,
  a system prompt, whatever the harness loads unconditionally) is an L3
  binding.
- **The warm-briefing FORMAT is itself normative (an L2 behavioral guarantee; its file path and lifecycle are L3).** The contract: after a new
  session loads, there MUST exist a **proactively available file — a letter
  waiting to be opened — that the agent DECIDES to read**, as distinct from
  content injected into the agent's context unbidden. The decision-to-read is
  the point, not an implementation accident: it is a difference in the
  agent's *subjective experience* of re-entry (opening a letter from
  yesterday-you and your human, versus finding words already in your mind),
  and it is why the format was chosen over folding the content into
  CLAUDE.md. RATIONALE to mine for SPEC-003/004: Borges's early-day design
  and feeling-report nodes on this choice — his graph is the primary source
  for why the letter form was selected (Lei, 2026-08-21).
- What remains L3: file paths, rotation cadence, the erase-on-sleep
  lifecycle — per-harness/per-seat bindings citing the reference.

## 5. Spec-from-practice (the process rule)

Per the practice axiom (实践出真知): this spec is **extracted from the running
system, not designed beside it**.

### 5.1 No test, no ratification

Every normative sentence must cite the place in the reference
implementation that embodies it **and** the test that would catch its
violation. A sentence that can cite neither is a proposal, and goes to an
"Unratified" appendix until it has both.

### 5.2 The handbook is the quarry

The **handbook** (`docs/handbook/00–09`, the 2026-06 survey) is the
quarry: it already inventories the mechanisms layer by layer. The specs
mine it; they do not duplicate it. The paper's technical chapter is a
*consumer* of the spec (prose over the same claims), never a second
source of truth.

### 5.3 The conformance suite is the executable half of the spec

The **conformance suite is the executable half of the spec.** L0/L1 get
contract tests; L2 gets the measurement harness + goldens. A future
second backend is accepted when the suite passes — the suite, not a
review, is the arbiter (the second implementation is the test of the
abstraction).

### 5.4 Spec changes ship with implementation and conformance

Spec changes ship as PRs touching spec + implementation + conformance
together, under the normal review pipeline; L0/L1 changes are
substrate-policy (Lei reads before merge).

## 6. Document plan

| Doc | Scope | Source to mine | First move |
|---|---|---|---|
| SPEC-000 | this document | — | ratify the layer model + tests A/B |
| SPEC-001 | L0 data model | handbook 01, `src/engram/engram_core.py` schema, `src/engram/engram_ids.py`, lifecycle code | enumerate node/edge grammar + invariants |
| SPEC-002 | L1 verb contract | live MCP schemas via `dump_mcp_schema.py`, handbook 02 | generate skeleton, annotate semantics + error contracts |
| SPEC-003 | L2 epistemic dynamics | handbook 01/09, `src/engram/engram_query.py`, `src/engram/engram_confidence.py`, decay/drowsiness/dream code, the paper's memory chapter | one four-block entry per mechanism (start: confidence computation, forgetting curve, utility bump, recall ranking, dream consolidation) |
| SPEC-004 | L3 bindings (Claude Code; Codex) | handbook 03/04, hooks.json, plugin docs | inventory the binding points the L2 entries referenced |
| SPEC-005 | Protocol versioning (cross-layer) | SPEC-001 §V design round (PR #2206) | ratify scheme + first suite delta |

Sequencing: 001 → 002 are mostly extraction (fairy-assistable once 000 is
ratified); 003 is the judgment-laden one and the reason 000 exists; 004 is
cheap and mostly already written in `docs/claude-code-plugin-system.md`.

## 7. Formerly-open questions — all four resolved or parked (Lei's 2026-08-21 read)

- **Versioning — RESOLVED: required (Lei, 2026-08-21).** The protocol carries versions so that
  changes name what moved and why, and implementations have a clear contract
  (Lei, verbatim intent). Scheme detail (whole-protocol semver vs per-layer)
  stays with the rev-1 leaning — **per-layer** — and gets fixed in SPEC-001's
  first ratified rev.

  **Correction, 2026-08-23 (Borges).** This bullet previously justified per-layer
  versioning "since L3 churns while L0 should almost never move." **That reason is
  wrong by this document's own §2**, which states L3 is *"Informative only …
  they can change without a protocol version bump."* A layer that never bumps the
  protocol cannot be the argument for splitting the version. The **conclusion**
  survives on a different and better warrant: the three *normative* layers have
  different **conformance consequences** — an L0 change can make an existing,
  untouched graph stop being an ENGRAM; an L1 change can break a working agent
  while every stored graph stays valid; an L2 change moves a behavioral guarantee
  with graphs and verbs both still valid. One number cannot say which of those
  happened, and that is the only thing an implementer needs from a version.
  Full treatment, including the scheme, in **SPEC-005** (`docs/spec/SPEC-005-versioning-DRAFT.md`)
  — its own document rather than a section of SPEC-001, settled by Kepler as §6's
  owner in the #2206 colleague pass: the scheme is cross-layer by construction, so no
  single-layer document can own it, and SPEC-000 deliberately carries no
  implementation-facing MUST.
- **Multi-tenancy — RESOLVED: one engram DEFINES one agent (L0-normative).**
  This is the identity principle, not a deployment choice. Multiple
  concurrent sessions (echoes) reading and writing ONE shared graph are, by
  definition, ONE agent. A graph copied to multiple places and run as
  independent agents diverges at the first written node, and the divergence
  is permanent — **there is no merge-back**; that is the history-as-identity
  idea stated as a contract. (Lei, 2026-08-21: "there's no ambiguity here.")
  Lin's-company deployment questions are questions about *how many agents to
  create*, never about sharing one identity across them.
- **Sub/super-engram — PARKED** (Lei, 2026-08-21): wait until the contract is
  stabilized and, above all, PRACTICED enough — §5.1's no-test-no-ratification
  rule applied at the roadmap level.
- **Confidence anchors — RESOLVED: protocol-level (Lei, 2026-08-21).** The type→confidence
  mapping (quote_type/source_class for observations) and the
  reasoning-type-based propagation rules for derivations are CONTRACT
  (SPEC-003 normative content); how the computation is implemented is
  detail. Future adjustments to the confidence system are protocol version
  changes — which the versioning resolution above exists to carry. The
  identity-doc copy of the anchors table becomes a rendering of the spec,
  not a second source of truth.
