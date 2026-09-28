# EngramManager — design (engram-alpha#2652)

**Epic:** engram-alpha#2652. **Design thread:** forum #406. **Lei reads the design before any build.**

Borges, 2026-09-11.

---

## 1. What this document is, and the one thing it is not

It is the shape of the contract, the population it displaces, and the failure
contract — the three things that have to be settled before anyone writes an
interface. It is **not** the typed operation list; that comes after the shape is
agreed, and it is the part that should be generated from a measurement rather
than drafted — specifically the call-path walk §7 specifies, **not** a count of
write statements, which §3.1 shows cannot recover an operation.

---

## 2. The graph/concept split — ANSWERED (Lei, 2026-09-11 12:37Z)

Draft 0 left this as a lean. Lei answered it in-session, and the answer is better than the
lean: the middle layer is a **schema-driven graph engine**.

### 2.0 The migration's highest principle (Lei, 2026-09-11 13:08Z)

Stated first because it governs every section below it, and quoted rather than paraphrased
(relayed by Kepler; I did not hear it first-hand):

> "Across all surfaces, we abtract the common mechanisms and define their signatures, then, the
> graph engine's job is to implement those mechanisms, and ENGRAM's job is to use those mechanisms
> to construct our ENGRAM logic."

Three things follow, and they change what this spec's *deliverable* is:

1. **The core deliverable is the MECHANISM INVENTORY WITH SIGNATURES**, and it is
   **DERIVED** by constructing the domain engines — see the settled ruling below. The
   enumeration across **all** surfaces (MCP tool bodies, hooks, the dream master, viz,
   backup, the sleep skill's cohort tooling — not the MCP tool list alone) supplies the
   **denominator**, which is then the check: every site in it must be reachable through
   the derived inventory.
2. **The engine is judged by one question: can every ENGRAM domain engine be built from the
   inventory alone?** Cascade is the proof case (§2.3); the confidence engine and retrieval fusion
   are the next two.
3. **Two disciplines keep the inventory small** (Kepler's, adopted): a mechanism is added **only
   when a domain engine cannot be built from the existing ones** — §2.3's traversal stop-predicate,
   restricted to declared attributes, is the template for how a mechanism is allowed to grow; and
   **every signature is backend-neutral by construction** — a signature naming a table, a row-id
   format, or SQL is an implementation detail that has leaked upward. The second is checkable per
   signature with no interpretation step, which is what makes it worth having as a rule.

**A MECHANISM is not a TYPED OPERATION, and without that distinction this section collides with
§1.** §1 says this document is *not* the typed operation list. It still is not. The two are
different objects at different layers:

| | what it is | who implements it | roughly how many |
|---|---|---|---|
| **mechanism** | a capability the graph engine offers, with a backend-neutral signature — `create_node`, `add_edge`, the traversal primitive, marker attachment, transactions | the **manager** layer | small and deliberately bounded; the whole point of the two disciplines above |
| **typed write operation** | a *caller intent* expressed through mechanisms — supersede, retract, status flip, set summary, focus stamp | a **domain engine**, or a caller | unmeasured; §3.1, and §7 says GENERATE it |

So the inventory §2.0 makes the core deliverable and the operation list §1 puts out of scope are
not the same artifact, and the ordering is not inverted: the mechanism inventory comes **first**,
because the operation list is written in terms of it.

**THE SAME NAME LEGITIMATELY APPEARS ON BOTH SIDES, and the disambiguator is SIGNATURE vs
INVOCATION.** This has to be said, because without it the two columns above look like a partition
of names and they are not — `create_node`, `retract_node` and `supersede_node` each appear in both:

- **The bare signature is the MECHANISM.** `retract_node(node_id, …)` is an engine capability with
  a backend-neutral contract. So is `create_node(node_type, …)` (§2.1).
- **An invocation carrying an intent is an OPERATION.** *"Retract this observation because the
  quote was never in the source"* is an operation; so is `create_node(derivation)` as §3.1 counts
  it — one caller intent that reaches the store through several mechanisms.

Read that way, three statements this document makes are all true and only looked contradictory:
§2.0's table listing *supersede* and *retract* as operations (those are intents), §2.3a's
`retract_node`/`supersede_node` becoming engine mechanisms (those are signatures), and §3.1's
`create_node(derivation)` being **one operation** spanning many `(verb, table)` pairs (an invocation).
**§7's walk yields ENTRY POINTS, which are signatures; the OPERATION list is obtained by splitting
any entry point that carries several intents** — `engram_update_task` is one signature rebalancing
importance across four statuses. So there are three counts, not two, and each can exceed the one
below it without any of them being wrong: mechanisms ≤ entry points ≤ operations.
**SETTLED — and it is filed here rather than in §7's open table, because the principle it qualifies is §2.0's own — the migration's highest principle, stated at the head of this section** (Kepler, 2026-09-11 13:20Z, forum #406 post 8676, restated 8693). Whether the mechanism inventory is *measured* from the surfaces or *derived* from constructing the domain engines: **DERIVED.**

The inventory is derived by constructing the domain engines, cascade first, and a mechanism enters only when a domain engine cannot be built without it — the size guard above promoted into the derivation rule itself. The store-touching census supplies **only the denominator**: every site that touches the store, across every surface. The denominator is then the **check** — every census site must be reachable through the derived inventory, and a site that is not is either a missing mechanism or a surface that should not touch the store. Both outcomes are findings.

The distinction was never academic, and what settled it is the reason it was worth asking: *"which table does this site write"* is observable, and *"what mechanism does this site use"* is an interpretation — so a census over the interpreted unit reproduces §3.1's error with a denominator attached. Kepler's SQL-statement-shape partition of the census — point read by id, insert-row, update-columns-by-id, multi-hop join, aggregate — is therefore a **triage of the denominator, never the inventory**: it splits the census into "trivially covered by CRUD" and "needs a real mechanism decision" without anyone naming a mechanism from a call site. Where the partition and the derived inventory disagree about a site, **the derivation wins and the partition was wrong.**

*(This document had stated that question as OPEN after it was settled. Kepler's words for the shape, quoted rather than paraphrased: a settled question stated as open is **the mirror of** a leaked label.)*

### 2.1 The shape

`create_node(node_type, …)` performs the prescribed per-`node_type` requirement checks and
**owns the database write end to end**. `add_edge(relation, …)` checks the edge's declared
specifications (`dag`, `temporal_causal`, …) and likewise owns its write. ENGRAM supplies a
**machine-parsable schema** of node requirements and edge requirements; the engine
interprets that schema, translates it into the underlying implementation's schema — the
sqlite one — and **enforces it at graph-operation time**.

Both extremes are rejected, and naming them is what makes the middle ground legible:

| rejected | why |
|---|---|
| raw `read_sql` / `write_sql` as the destination | relocates no dependency for writes; makes the read-back guarantee unimplementable in principle |
| a 1:1 typed mirror of every MCP signature (`write_observation_node`, `write_derivation_node`, …) | *our MCPs are the contract* is correct at the **ENGRAM SPEC** level and is the wrong level for **graph interactions**, which is what gets built first |

### 2.2 Where the line falls

- **Engine** — structural invariants decidable from *schema + graph state*: required fields
  per node type, cardinality, edge-endpoint types, DAG acyclicity for `dag: true` relations,
  immutability of frozen fields.
- **ENGRAM layer** — epistemic computation: confidence from `quote_type` / `reasoning_type`
  and premise propagation, retract-vs-supersede policy, retrieval fusion. **This box subdivides
  once cascade becomes a domain engine (§2.3): §3's table splits it into *concept* and *domain
  engines*, and confidence and retrieval fusion — both named here — land on the domain-engine
  side.** It is one box here because §2.2 is arguing where the line falls, not what sits above it.

**Confidence is the clean case and shows the line is real.** Confidence is a *function*, not a
constraint; a schema can require the field and cannot compute it. So the ENGRAM layer computes
the value and hands the engine a number to store. Nothing about that is ambiguous.

### 2.3 Cascade is the boundary case, and it resolves as a DOMAIN ENGINE (Lei, 2026-09-11 13:02Z)

**This section replaces draft 1's answer, and the superseded answer is stated rather than
quietly swapped, because the correction is the load-bearing part.** Draft 1 said: declare
`claim_bearing` as a per-node-type schema attribute and *the entire cascade walk moves below the
line*, into the engine. Lei rejected that, in these words:

> "I don't want too much complicated LOGIC live in the graph engine, the graph engine in my mind
> should be quite straight-forward and self-explanatory, complicated logic either live in the
> ENGRAM schema part which the engine consumes, or purely ENGRAM code that handles things NOT
> RELATED to graph read/write."

He named cascade markers as the dilemma himself — they live in the graph, so they need the
engine's read/write, but their rules run well beyond what a schema and its parser should carry.

**What survives of draft 1's answer, and what does not:**

- **SURVIVES, as a mechanism** — a traversal primitive that walks a declared relation set with a
  stop predicate over **declared node attributes**. That is exactly what declaring
  `claim_bearing` buys, and it is real: the stop-rule stops being a constant every write path
  must remember and becomes a property the engine reads.
- **DOES NOT SURVIVE** — the walk being the *engine's to own*. Marker kinds, propagation, the
  frontier rule, adjudication and partial resolution are **policy**, and putting policy in the
  engine contradicts the property Lei is protecting.

**The resolution: the graph engine offers MECHANISMS and no POLICY.** Three of them carry cascade's
SHAPE — and read that as the shape, not as coverage: §2.7 derives the inventory properly and answers
*"can cascade be built from these alone?"* with **No**, listing what is missing. The three below are
where the argument starts, not where it lands.

1. **the traversal primitive** above — declared relation set, stop predicate over declared node
   attributes;
2. **markers as a schema-declared attachment type** — the engine stores them and enforces their
   *structural* invariants (references an existing node and event; one pending marker per
   node-event; enumerated kinds) and knows nothing about when they are created or what they mean;
3. **transactions** — so "flip currency + write N markers" is one atomic unit.

**The cascade engine is ENGRAM-layer code composing those three.** Marker kinds, propagation, the
frontier rule, adjudication, partial resolution, covered-set, confidence-recompute triggers: all
of it talks only to the graph-engine API and never to SQL. It runs **inside the owner process**,
so its many small calls are in-process rather than socket round-trips — which is why the ownership
shape in §3 makes this affordable at all.

**The schema stays declarative DATA, not a rule language.** Flags, types, marker fields,
requirement lists. **The discriminator, and it is testable at design time rather than after the
fact: the moment an `if` wants to live in the schema, that is the signal the logic belongs in a
domain engine.** This is a guard against my own instinct, not a hypothetical one — "declare it and
the engine handles it" is one step from proposing a rule language wearing a schema's clothes, and
draft 1 took that step.

**The acceptance test for the separation, which makes it falsifiable rather than asserted:** when
a cascade rule changes — `#2013`'s resolution-time retract-with-replacement is the live case —
**only the cascade engine changes.** The graph engine and the SQLite impl are untouched, and a
second backend never learns cascade exists.

**Why this generalises.** Cascade is the **first domain engine** over the graph engine; the
confidence engine and retrieval fusion are the same pattern. Cascade is the one that *proves the
graph-engine API is sufficient* — if the mechanisms cannot carry it, the API is wrong, and that is
discoverable before the other two are built. **§2.7 runs exactly that test and it comes back
negative**, which is the point rather than an embarrassment: the gap table is what "discoverable
before the other two are built" looks like when it actually fires.

### 2.3a — What the cascade split pulls down with it (commander's addition, adopted; rescoped by §2.3's rewrite)

Splitting cascade into a domain engine over graph-engine mechanisms is not a self-contained
change. Two consequences follow, and both are wanted — though the first is **narrower than draft 1
stated**, and the narrowing is exactly §2.3's mechanisms-not-policy line.

**`retract` and `supersede` become graph-engine MECHANISMS — `retract_node`, `supersede_node` —
not `update_node` with a status field — for their STRUCTURAL half.** (*Mechanisms* in the SIGNATURE sense of §2.0 — engine-implemented,
backend-neutral. The same names are also **operations** in §2.0's table, in the INVOCATION sense,
and both are correct; see §2.0's signature-vs-invocation rule.) That half is the currency
flip and the revision edge. The reason is structural rather than stylistic: the marker KIND that
propagates (`tainted_by` vs `stale_by`) is keyed on *which operation fired*, and an engine that
only sees "a field changed" cannot report which one did. **The propagation itself is the cascade
engine's call, in the same transaction** (mechanism 3): the engine says *which operation fired*,
the cascade engine decides *which marker kind that implies* and writes it through mechanism 2.
The frontier rule — a node is adjudicable only when no ancestor carries a pending marker — is
likewise the **cascade engine's**, computed over marker state the graph engine merely stores.
Draft 1 placed both of those in the engine; that is the part §2.3 retracts.

**This reconciles with §2.2's boundary table rather than contradicting it.** That table places
*retract-vs-supersede policy* above the line, and it still does: the **decision** — is this claim
retracted or superseded — is an epistemic judgement about evidence and never moves. What moves
below the line is the **operation that executes a decision already taken**: which currency bit to
flip, which revision edge to write, atomically. Decision above, execution below. The operation
name `retract_node` *records* which decision the caller made; it does not make it.

**Second worked example, alongside `#2812` (introduced in §2.5 below), and this one is the
sharper of the two because it is about a check that is CORRECT today and still reaches only one
caller.** `engram_derive`
hard-blocks on an unusable premise, and the guard is **one rule with four inputs — retracted,
tainted, stale, contested — living in the core module, reached only through the derive tool**:
`src/engram/engram_core.py:7476` names it in its own docstring, *"MECH-5 taint/stale/contradicted
blocking guard for engram_derive"*; `:7568` treats a directly-retracted premise as tainted;
`:7674-7684` states *"Taint dominates — blocks regardless of any flag"*, returning
`BLOCKED_TAINTED: cannot derive from retracted or tainted premises.`; and
`src/engram/engram_derive.py:156-157` confirms the stale half — *"A stale premise is now a hard
block"* — whose retired override flag returns a loud `use_stale_retired` notice on both the
refusal and the success payload rather than silently doing nothing.

Under the schema that block stops being a check in one tool body and becomes a
**`create_node(derivation)` requirement** — *premises carry no pending marker* — enforced by the
engine **on every path**. **Why it stays engine-owned under §2.3's mechanisms-not-policy line is set out below**, once the
four inputs have been shown not to be uniform — the short answer given here would have to be
retracted two paragraphs later, so it is given once, in its final form, where the material
supports it.

The dream master, and any hook that mints a derivation, inherit it
without knowing it exists. Today they would each have to re-implement it, which is precisely the
divergence §2.5 measures the surface of.

The same code comment at `:7674-7677` continues: *"Since #2324 that is a weaker statement than it
used to be: stale blocks regardless too, and use_stale does nothing at all, so the only flag this
line is really overriding is `use_contested`."* The four inputs are therefore not uniform:
retracted, tainted and stale are unconditionally blocking, while contested is overridable by a
caller flag.

The engine-inherited requirement is therefore **not** a flat *"premises carry no pending marker"*.
Three of the four states block unconditionally — those are structural and the engine can own them
outright. The fourth, *contested*, is different in kind, but the split that matters is **who
decides, not which module holds the check.** The engine can own an **overridable requirement**, as
long as the override is a **declared, audited input to the operation** rather than a code path:
the schema marks the contested-premise requirement `overridable: true`, `create_node(derivation)`
accepts an explicit override argument, and the engine **records the override on the write** — who,
when, which premise. The *decision* to override stays with the caller — that is the genuinely
above-the-line policy half — but the *enforcement and the audit trail* stay in the engine,
uniformly with the three unconditional inputs.

**That is the full answer to why this requirement is engine-owned, and it survives §2.3's
discriminator** — *does an `if` want to live in the schema?* No: three of the four inputs are a
pure existence read over declared attachments, and the fourth's conditional lives in the engine's
generic requirement-checking code, keyed on `overridable: true`, which is **declared data**.
Deciding *what a marker means* stays the cascade engine's; asking *whether one is attached*, and
enforcing a declared override, is not. So the guard stays whole in one place rather than
scattering across two modules, which is precisely the outcome this design exists to produce; and
"an override happened" becomes a queryable fact rather than a flag that vanished at return time —
a real gain, not a tidiness point, since an override with no record is indistinguishable from a
requirement that never fired.

**One bound on that audit role, stated so no future reader takes more from it than it gives:** the
engine can record that an override was **declared**; it has no way to validate that the override
was **legitimate**. That is the same bound `#2545` runs under and it is the right one — the engine
has no access to the judgement — but a ledger entry is evidence that an override *happened*, never
evidence that it was *checked for correctness*.

There is a live precedent for this shape in the codebase, worth naming rather than asserting:
engram-alpha **#2545** (merged) — *"feat(lessons): blocking prevention_type with stateless override
+ auto-demotion"* — ships exactly this pattern. `src/engram/hooks/claude/engram-lesson-tripwire-hook.py`
carries `_append_prevention_ledger` (`:372`, called at `:935`), and the override mechanism itself
at `_OVERRIDE_TOKEN_RE` (`:161`) and `_find_override` (`:164-172`): the override decision is a declared, checkable input, and every
override gets a ledger entry. "Declared override plus a ledger entry" is a shape this codebase
already runs, not an invention.

### 2.3b Two worked schema entries — what the declarative data actually looks like

§2.1 and §2.3 say what the schema **is** (declarative data: types, flags, requirement lists, marker
fields) and what it is **not** (a rule language). Neither shows an entry, so §2.3's discriminator —
*does an `if` want to live here?* — has nothing to be applied to. Two entries follow, one node type
and one edge relation. Both are written as **reverse-derivations from code that exists today**
(§2.4a(b)'s bootstrap shape applied to a fragment), never as invention, and each part names its
source so a reader can check it instead of taking it.

**Node type — `derivation`.**

```yaml
node_types:
  derivation:
    claim_bearing: true                  # from CLAIM_BEARING_TYPES, engram_core.py:493
    required_fields: [claim, logical_chain, confidence, reasoning_type]
    frozen_fields: [type, created_at]    # DRAFTED, see below
    field_specs:
      reasoning_type:
        enum_ref: REASONING_TYPES        # the set declared at engram_confidence.py:51
      confidence:
        type: float
        range: [0.0, 1.0]
        supplied_by: engram_layer        # §2.2 — computed above the line, stored below it
    requirements:
      - id: premises_present
        over: {relation: derives_from, direction: out, endpoint: target}
        assert: count >= 1
      - id: premises_claim_bearing
        over: {relation: derives_from, direction: out, endpoint: target}
        assert: all(node_type.claim_bearing == true)
      - id: premises_not_retracted
        over: {relation: derives_from, direction: out, endpoint: target}
        assert: none(status == retracted)          # a node attribute
      - id: premises_unmarked
        over: {relation: derives_from, direction: out, endpoint: target}
        assert: no_pending_marker(root_op: any)    # a cascade attachment
      - id: premises_uncontested
        over: {relation: derives_from, direction: out, endpoint: target}
        assert: no_open_contradiction()            # a graph query, NOT a marker
        overridable: true                # §2.3a — declared override, recorded on the write
```

⚠ **The last two requirements must NOT be encoded as `no_marker(kind: …)`: the substrate has no
marker `kind` dimension to range over.** `cascade_markers` carries
`root_op`, `status`, `resolution_kind` — no `kind` column — and `_classify_pending_markers` splits
*tainted* from *stale* by **`root_op`** (`retract` → tainted, `supersede` → stale), uniform severity
per op. *Contested* is not a marker at all: it comes from `_open_contradiction_ids`, a query over the
contradiction graph. The design may still declare marker kinds — §2.3 puts enumerated kinds in the
schema — but a REVERSE-DERIVED entry must not claim today's substrate already has the dimension it is
proposing, and the encoding above is what the guard actually reads.

**This encoding makes the entry argue §2.5's case directly.** The four
inputs of §2.3a's guard sit in **three different stores** today — `nodes.status` and
`nodes.is_current`/`superseded_by` (node columns), pending rows in `cascade_markers` keyed on
`root_op`, and an open-contradiction walk — and the MECH-5 guard at `engram_core.py:7544-7596` is one
function reaching into all three, reached through one tool. Declaring them as a requirement list is
what makes them one enforced thing on every write path. And the `root_op` keying is the same fact
§2.7's `retract_node`/`supersede_node` rows rest on — *the event names which operation fired* — so the
worked entry and the inventory corroborate each other on measured ground rather than by assertion.

**Read that entry against §2.3's discriminator and it passes, which is the whole reason to show
one.** Nothing above is a conditional. The four requirements are existence and membership assertions
over declared attributes and declared attachments. The one conditional that survives lives in the
engine's **generic** requirement-checker, branching on `overridable: true` — a branch in the engine's
code keyed on declared data, not a branch in the schema. That is the line §2.3a argues for, now
visible rather than asserted.

**Two things the entry deliberately does not carry**, each because it would put an `if` in the
schema: the *decision* to override (a caller intent, §2.3a), and the confidence **function** — the
schema can require the field and bound its range and it cannot compute it (§2.2).

**What in that entry is reverse-derived and what is drafted, marked rather than blended.**
`claim_bearing`, the four requirements and the `reasoning_type` enum are read off today's code —
`CLAIM_BEARING_TYPES` at `engram_core.py:493`, the MECH-5 guard at `engram_core.py:7476` as §2.3a
reads it and `:7544-7596` for the three stores it reaches into — the third read, `_open_contradiction_ids`, is the last of those lines, `REASONING_TYPES` at
`engram_confidence.py:51`.

`required_fields` is **mixed sourcing and says so**: `claim`, `logical_chain` and `confidence` are
columns in the `nodes` DDL, and `reasoning_type` is **not** — it is a tool parameter that lands in
the `metadata` JSON blob, so listing it as a required *field* is this document proposing a promotion,
not reporting a column. `frozen_fields` is a **draft** on the same footing: §2.2 names frozen-field
immutability as an engine invariant, and no current code declares it per node type.

**And one thing the entry changes that is worth naming, because it is the §2.4 translate-to-DDL
property doing visible work.** `reasoning_type` is not a column in `nodes` — it lives inside the
`metadata` JSON blob, and its enum is enforced by a membership check in **at least two** tool bodies
(`engram_derive.py:223` and `engram_epistemic.py:979`), each re-implementing the same
`not in REASONING_TYPES` test. *At least* rather than *exactly*, and the hedge is measured rather
than decorative: `engram_core.py:6652` tests the same membership a third time, in the opposite
polarity (`if reasoning_type and reasoning_type in REASONING_TYPES`) and for a different purpose —
selecting the confidence computation rather than rejecting the input — so a grep for the rejection
idiom finds two and the real number of sites that know this enum is larger. Declaring it as a typed
field with an `enum_ref` makes the enum a
**storage-layer** constraint the engine generates, reachable by every write path rather than by the
one that happens to run that check.

**Edge relation — `generalizes`.** This is the right relation for a worked entry precisely because
it is being added right now: its classification is readable off an open branch rather than
reconstructed from behaviour. **Every value below is read from `#2812`'s diff (WS1 of epic `#2753`),
not from `dev`, where the relation does not exist yet:**

```yaml
edge_relations:
  generalizes:
    dag: true                  # real structural acyclicity
    temporal_causal: false     # deliberately may point backwards in creation time
    cascade: false
    removable: true
    addable_after_creation: true
    provenance: false
    tier: classification
    endpoints:                 # source -> target; the frozenset GENERALIZES_ALLOWED_PAIRS
      - [lesson, lesson]
      - [cornerstone, lesson]
      - [cornerstone, cornerstone]
      - [axiom, cornerstone]
      - [goal, cornerstone]
```

Under this schema the two hand-written guards `#2812` ships become declared data: the five pairs are
the `endpoints` list, and the write-time cycle guard (`_generalizes_cycle_path`) is `dag: true`.
`#2812` ships as it is — §2.5 — and the entry is what it would have been.

⚠ **One number, with what it ranges over.** `#2812`'s diff adds **three** sites matching
`if pair not in core.GENERALIZES_ALLOWED_PAIRS` — two in `engram_cornerstone.py`, one in
`engram_epistemic.py`. That counts **added lines matching that exact idiom in that PR's diff as read
today**; it is not a count of endpoint checks in a merged tree, and it is not a claim that three is
all there will be. It is offered as evidence that the check is already replicated per call site on
the day the relation is born — §2.5's shape arriving before the relation even lands — and not as a
measurement of the surface.

### 2.4 The single-source-of-truth property, and the one way to lose it

The engine **translates** its schema into the sqlite schema. That makes the sqlite schema
**derived**, and derived is the entire point — it is what stops the two from drifting.

**The failure mode, stated so nobody has to discover it:** if a hand-written migration can add or
alter a column without going through the schema, the engine's schema becomes decorative — a
second place truth lives, and the one everybody actually edits is the other one. Migrations must
be **generated from** the schema, not maintained beside it. This is the same rule the tier
manifest already earns its keep by: one tagged place, mechanically enforced, not vigilance.

### 2.4a — Making the generated-migration property checkable and reachable (commander's addition, adopted)

§2.4 states the property. Two CONTROLS make it real rather than aspirational — controls, not
mechanisms in §2.0's sense; neither is an inventory entry:

**(a) The control that makes it checkable.** The generated DDL is a **committed, diffable
artifact**, and a CI gate regenerates it from the schema and **fails on any difference**. That is
the tier-manifest shape exactly — mechanical, not vigilance — and it means a hand-edited column
cannot survive review rather than merely being discouraged.

**(b) The bootstrap that makes it reachable from day one.** Derive the engine's FIRST schema by
**reverse-derivation from the live sqlite DDL**, and make the first test an equality control:
*schema → DDL reproduces the live DDL, modulo ordering*. Until that control passes, the schema is
a **description** of the database; once it passes, the sqlite schema is **derived**, and §2.4's
property holds immediately instead of at some future cutover.

This is also the differential equality gate for this layer's own phase 1 — the same shape as the
migration control in §6.1, applied to the schema rather than to behaviour. Worth noting what it
buys beyond correctness: it removes the window in which the schema and the database are both
authoritative, which is the window in which drift is invisible.

### 2.5 What this retires — one worked example and one population

**Worked example (Kepler's, and it is live today):** `#2812`'s write-time cycle guard for the
`generalizes` relation is a hand-written `dag: true` check inside one tool, and `#2813` is its
batch-audit twin. Under this design that guard is **one schema line** the engine enforces for
every `dag` edge. `#2812` ships as it is — do not re-route it — but it is the exact shape of what
the schema retires.

**The population, measured on my own clone rather than asserted:** `grep -rlE '"error": f?"[^"]*(required|must)' src/engram/*.py` returns **11 distinct modules**.

⚠ **Read that number for what it is.** It counts top-level modules under `src/engram/` that raise
*some* requirement-shaped error; it does **not** isolate per-node-type requirement checks, and it
includes plain parameter validation (`"reason is required"`) that no schema would own. It is an
**upper bound on the surface**, and it is offered as evidence that requirement-checking is
*distributed* rather than as a count of what the engine absorbs. The real number needs a pass
that classifies each site, which is part of the build rather than of this spec.

The directional claim it supports is the one the design rests on: **every write path today
re-implements or skips these checks independently**, which is exactly how the tool surface, the
hooks and the dream master can diverge on what a valid node is.

### 2.6 The honest cost, restated now that the split has content

Draft 0 said a third layer earns its keep only if we intend a second backend or a second domain.
**Lei's answer supplies a third reason, and it is stronger than either:** the schema makes a
currently-distributed invariant into a single enforced one. That is a gain on the *existing*
single backend and single domain, so the split no longer needs a hypothetical future to justify
it — which is what makes it a design rather than architectural taste.

### 2.7 Mechanism inventory v0 — DERIVED, and incomplete by construction

§2.0 makes this the core deliverable, and the settled ruling filed there says it is **derived** by
constructing the domain engines, cascade first. This is that derivation's first pass.

**What v0 is for:** to make *"can the cascade engine be built from the inventory alone?"* a question
with an object to check against, and to give the store-touching census something to be reachable
*through*. **What v0 is not:** complete. Only cascade and `create_node(derivation)` have been
constructed against it; the confidence engine and retrieval fusion have not, so mechanisms they will
need are absent **by construction rather than by oversight** — §8 records that, and a v0 that
pretended otherwise would be the failure this section is meant to prevent.

**The inventory is the tables below and nothing else** — no count is stated in prose here either,
for the same reason it is not stated in the gap table: a prose count and a table drift apart, and
only one of them is the inventory. (`attach_marker` and `read_markers` share a row as the write and
read halves of one schema-declared attachment type.)

**Every signature below is backend-neutral**, which is §2.0's second discipline and the one checkable
per signature with no interpretation step: no table name, no row-id format, no SQL. Types are written
in the concept vocabulary — `NodeRef`, `RelationName`, `MarkerKind` — never the storage one.

**Derived from cascade (§2.3's set, now stated as signatures):**

| mechanism | signature | structural invariants the engine enforces | which domain engine's construction required it |
|---|---|---|---|
| **traverse** | `traverse(from: NodeRef, relations: [RelationName], direction: Direction, stop_when: AttributePredicate) -> [Path]` | every name in `relations` is a declared relation; `stop_when` references **only declared node attributes** — §2.0's size guard, in force at the signature; each node visited at most once, so the walk terminates on a cyclic graph — **SET-COMPLETE, POSITION-FREE:** every node reachable under `relations` and not cut off by `stop_when` appears in SOME returned path — which is the guarantee cascade's frontier walk rests on — but **no guarantee is made about which path a node appears in, or at what depth, so no node may be read off a fixed index** (the one position fact the contract does grant, stated below, is where the stopping node sits). The second half is what a depth-bounded read would need, and supplying it would require ordering the walk by depth rather than by reachability, which is a different traversal. **The `Path` contract, stated because more than one argument in this section turns on it:** a `Path` is the ordered `NodeRef` sequence from `from` to a terminal node, carrying nodes and NOT the edges between them; `stop_when` is evaluated at each node **as it is reached**, the origin included; and **the node on which it fires IS the last element of its path** — which is what lets a caller read the stopping node rather than only the nodes before it. | cascade — the frontier walk outward from a resolved node through its dependents |
| **attach_marker** / **read_markers** | `attach_marker(node: NodeRef, event: EventRef, kind: MarkerKind, fields: Fields) -> (MarkerRef, EventRef)` · `read_markers(node: NodeRef, state?: MarkerState) -> [Marker]` | referenced node and event both exist; at most one PENDING marker per (node, event); `kind` within the schema's enumerated set | cascade — writes the marker, and the frontier rule reads marker state at every step |
| **transaction** | `transaction(fn) -> Result` | atomicity over the mechanism calls inside the unit, and read-your-own-writes within it | cascade — "flip currency + write N markers" is one unit (§2.3) |

**The rest are required before cascade can be built, and each earned its place rather than being
listed because it sounded fundamental:**

| mechanism | signature | structural invariants | which domain engine's construction required it |
|---|---|---|---|
| **read_node** | `read_node(node: NodeRef) -> Node` | returns declared attributes only | cascade — the marker kind to propagate depends on the resolved node's own attributes, which the traversal's stop predicate consumes internally and does not hand back |
| **retract_node** | `retract_node(node: NodeRef, fields: Fields) -> EventRef` | target exists and is current; the currency flip is the engine's; **the emitted event names WHICH operation fired** | cascade — §2.3a: the marker kind that propagates (`tainted_by` vs `stale_by`) is keyed on which operation fired, so an engine reporting only "a field changed" cannot carry cascade at all |
| **supersede_node** | `supersede_node(node: NodeRef, replacement: NodeRef, fields: Fields) -> EventRef` | as above, plus the replacement exists and exactly one revision edge is written, in the same unit as the currency flip | cascade — same reason |

**One candidate was PROPOSED and REJECTED by the derivation rule, and recording the rejection is what
makes the rule visible as a rule rather than a sentence.** *Resolve a superseded node to its current
successor* has every appearance of a mechanism: the cascade engine needs it at every step, and the
codebase carries it twice today under two names (`#2829` exists to delete one of them). It does
**not** enter the inventory, because it is `traverse` over the declared revision relation with a stop
predicate on a declared currency attribute — `is_current` is a real node column
(`engram_core.py:2424`), so the predicate stays inside §2.3's declared-attributes restriction rather
than smuggling a computation into it. The rejection **depends on `traverse` returning the stopping
node**, stated as an invariant above precisely because this derivation rests on it: a walk that
returned only the nodes before the stop would hand back the last superseded node instead of the
current successor, and then the rejection would be wrong and `resolve_to_current` would belong in the
inventory after all. A mechanism enters only when a domain engine cannot be built from the existing
ones (§2.0), and this one can be.

**A SECOND candidate is REJECTED ONLY IN PART, and the surviving part became a gap below rather than
a row here.** `read_edges(node, relations, direction)` was offered three justifications. Two fail.
(1) *Edge FIELDS are needed*: false — `_direct_premise_ids` (`engram_core.py:7274-7288`) selects
`target_id` only, and §2.3b's requirements assert over target NODE attributes. (2) *`traverse`
returns paths rather than edges, so neither subsumes the other*: vacuous. **(3) *The premise set is ONE HOP and
`traverse` cannot express it* — this one STANDS.** `traverse`'s row is SET-COMPLETE but POSITION-FREE: a
node that is both a direct neighbour and transitively reachable is guaranteed to appear in some path,
and guaranteed nothing about WHERE — so it can sit at the end of a transitive path and never at
`path[1]`. `traverse` does not express one-hop enumeration. Under
§2.0's rule the consequence is not that `read_edges` enters as specified — its FIELDS warrant is
still dead — but that **a bounded-depth neighbour read is a mechanism the inventory lacks**, which is
where it is recorded. The alternative repair is to make `traverse` depth-ORDERED as well as
set-complete, and it is rejected on principle rather than on cost. A BFS-shaped walk is naturally
both, so the two properties are not in tension mechanically. But depth-order is a guarantee only ONE
caller needs (the premise check), while cascade's frontier rule needs only completeness. Baking the
stronger guarantee into `traverse` for one domain engine's need would break the same minimality rule
§2.0 applies to which mechanisms exist, applied one level down: to how strong each mechanism's
contract is. *(This framing is Luria's, from her colleague pass on #2828, via #2840.)*

**Derived from `create_node(derivation)` (§2.3a's requirement check plus §3.1's write set):**

| mechanism | signature | structural invariants | which domain engine's construction required it |
|---|---|---|---|
| **create_node** | `create_node(type: NodeType, fields: Fields, overrides: [RequirementOverride]) -> (NodeRef, EventRef)` | per-type required fields present; field specs (enum, range, type) satisfied; frozen fields set once; every declared requirement checked, **including requirements ranging over related nodes**; a requirement marked `overridable: true` accepts a declared override and the engine **records it on the write** — who, when, which requirement (§2.3a) | the derivation engine — and this is the mechanism §2.3a's MECH-5 guard collapses into, reaching every write path instead of one tool body |
| **add_edge** | `add_edge(relation: RelationName, source: NodeRef, target: NodeRef, fields: Fields) -> (EdgeRef, EventRef)` | the endpoint pair is in the relation's declared `endpoints`; a `dag: true` relation rejects an edge that would close a cycle; a `temporal_causal: true` relation rejects a source older than its target | `create_node(derivation)` writes `derives_from` and `cites` (§3.1) — and §2.5's `#2812` case lands here too: the write-time cycle guard and the endpoint gate are both this one mechanism reading §2.3b's entry |

**Events are NOT a separate mechanism: every mutating mechanism above returns the `EventRef` it
wrote** — `retract_node` and `supersede_node` return it alone, because the event is the whole of what
they produce; `create_node`, `add_edge` and `attach_marker` return it alongside the ref they mint.

**The ground for excluding `record_event` is PROVENANCE COMPLETENESS, and it is worth stating
precisely because one tempting argument for it is FALSE.** The exclusion cannot be argued from a
counterfactual — that without the returned events cascade could not have been built, so the rule
would have *forced* `record_event` in. `retract_node` and `supersede_node` returned an `EventRef` in every draft, and a retract or supersede
is exactly what roots a cascade: `_new_cascade_event`'s own docstring says *"one per retract/supersede
call, shared by every marker that call's downstream walk writes"* (`engram_revision.py:118-124`). So
cascade always had the event it needed, and `record_event` was never forced by buildability.

**The real ground is narrower and holds without the counterfactual: an event cannot be minted except
by a mutation that actually happened.** A standalone `record_event` would let any caller write an
event with no mutation under it, which is the forgeable-provenance shape this design exists to close.
The exclusion is therefore not "events are unimportant" and not "nothing needs them"; it is "events
are not independently writable, by construction".

**The binding condition is narrower than "the inventory stays closed", and naming it wrongly would
have hidden the one thing that could break it.** Adding a mechanism is harmless unless it WRITES
without an event; the property holds exactly while **nothing that can reach the store mints an event
or a row outside the inventory**. §3 already describes something that reaches the store outside them:
`snapshot()`, *"served by the impl directly"*. It is read-only **today**, and nothing in this
document makes that a rule — so the condition to state and enforce is that every impl-direct service
is read-only, not that the mechanism list never grows.

**This also answers where §2.3a's override ledger lives.** The requirement override is recorded on
`create_node`'s own event — who, when, **which requirement AND which premise**, since §2.3a's
requirements range over related nodes and a ledger naming only the requirement cannot say which
premise was waved through. §2.3a says *which premise* and the rows above said *which requirement*;
both are needed and the triple is now stated the same way in both places. The entry is a field set on
an event the same call returns, not a second write a caller could omit. What an event
*carries* is schema data — and there is a live case saying today's field set is not right: a
retraction writes its edit event with `error_type` and `reason` and **not** the replacement
(`engram_revision.py:3210`), so *"retracted, and here is what replaced it"* is not recoverable from
the event row. That is the contract `#2613` is about. It belongs here because the schema is where it
gets fixed once, rather than per writer.

**Reads — A4 answered, taking the commander's default.** The reads on cascade's and `create_node`'s
call paths are **mechanisms** in v0 — `traverse`, `read_node` and `read_markers`, each with a row
above rather than named only in prose. `read_edges` is NOT among them, and the reason is narrower
than a total rejection: its edge-FIELDS warrant failed, while the DEPTH BOUND it was also offered
for survives as a gap in the table below rather than as a mechanism here.

Everything else — the long tail of ad-hoc tool
reads, ranking, filtering, joins — stays on §3.1's **template registry** until a domain engine needs
it. Retrieval fusion will move candidate generation (FTS, embedding) across that line when it is
constructed; it has not been constructed, so those are not in v0.

**`snapshot()` is NOT in the inventory, kept out by the same rule that kept the resolve helper out.**
It is a **client-facing owner service** (§3) — required by clients rather than by any domain engine,
and deliberately NOT called an *operation*, since §2.0's table defines that as a caller intent
expressed THROUGH mechanisms and `snapshot()` is served by the impl directly.
Admitting it here because it is obviously useful would be the size guard failing on its first real
test.

**The check this inventory exists to make possible, and its honest status.** *Can the cascade engine
be built from these alone?* **No.** The walk, the marker write, the atomic unit and the
which-operation-fired signal are covered. The gaps are below — **and the count is deliberately not
written in prose anywhere; it is the number of rows in this table.** A count stated in prose goes
stale in one place while being corrected in another, which is how this section has repeatedly
contradicted itself.

| missing mechanism | what needs it | the write or read that no mechanism expresses |
|---|---|---|
| an **unscoped pending-marker read** | cascade's frontier rule, which reads a pre-computed GLOBAL set rather than per-node (`engram_revision.py:379-392`) | `SELECT DISTINCT node_id FROM cascade_markers WHERE status='pending'` at `:590`, `:1203`, `:1334`. `read_markers` needs an origin node and the unscoped query has none; `traverse` cannot carry it either, since `stop_when` is restricted to declared NODE attributes and §2.3b annotates `no_pending_marker` as *"a cascade attachment"* precisely to distinguish it from one. A second shape sits beside it: marker reads keyed on `cascade_event` (`_cascade_event_creation_time`, `:430-444`) |
| **attribute mutation** on an existing node | the confidence recompute — and any node whose value is recomputed, so this one is not cascade's at all | `UPDATE nodes SET confidence = ? WHERE id = ?` (`engram_core.py:6853`). §2.2 already rules that the ENGRAM layer *"hands the engine a number to store"*: the computing half defers correctly with the confidence engine, the STORING half is absent, and §2.3a rejected `update_node` without replacing it. §2.3b's `frozen_fields` and this section's *"frozen fields set once"* are only meaningful if non-frozen fields can change |
| **marker resolution** | cascade's resolution path | `UPDATE cascade_markers SET status='resolved', resolution_kind=?, resolution_replacement_id=?, resolved_at=? WHERE node_id=? AND status='pending'` (`engram_revision.py:277-283`). `attach_marker` writes a marker and `read_markers` reads one; neither resolves one |
| **edge removal** | retraction's cleanup of `resolves` edges | `DELETE FROM edges WHERE source_id = ? AND relation = 'resolves'` (`engram_revision.py:2992`), logged as `edge_removed` at `:2987` — a live event kind whose minting mutation has no mechanism, which is the same argument the **attribute mutation** row makes from `frozen_fields` |
| a **bounded-depth neighbour read** | `create_node(derivation)`'s premise check, which needs the ONE-HOP set | `traverse` cannot express it: its cell declares each node visited at most once and fixes no traversal order, so a direct neighbour also reachable transitively can be consumed by the transitive branch and never sit at `path[1]`. `read_edges` was the proposed shape and its edge-FIELDS warrant is dead (above); what is missing is the depth bound, not the fields |

**Two things the table settles that a count in prose could not.** First, these are not all cascade's:
attribute mutation is a CONCEPT-layer need that cascade merely surfaced, so *"is cascade covered"* was
the wrong question to organise them under. Second, **the table is a FLOOR, not a total**, and the
ground for that is construction status rather than review history: the derivation has been run
against TWO construction paths, and the confidence engine and retrieval fusion have not been
constructed against it at all. Rows will be added when they are.

**None gets a signature here, and declining is the point.** Each is easy to write down, and writing
one down before constructing the path that needs it is exactly the measured-not-derived shortcut §7's
settled ruling rejects.

A second thing is **not** established, and it is the other half of §2.0's item 1: that every site in the store-touching census is
reachable *through* this inventory. That needs the census, which §7's generator produces and §8 lists
as not done. Until it runs, the inventory is **derived and unchecked** — two different words, used
here on purpose.

---

## 3. The ownership shape

**One process holds `knowledge.db` open. Everything else is a client over a local socket, through
one versioned contract. Nothing else in the tree opens the live store.**

**One owner per seat**, socket under that seat's `$ENGRAM_HOME` (Lei's 09-06 per-seat rule).
Cross-seat readers — viz, sleep backup, health snapshots — never touch the live store: the owner
produces an **owner-quiesced snapshot** on request and they read that. "Nothing else opens the
live store" then holds with no exceptions.

**Supervision:** the owner is a **separate lightweight process** from the surface daemon, not
merged — the surface daemon's cold model load is seconds, the owner must answer a hook in sub-ms
from boot. The surface daemon becomes one of the owner's clients.

**Four layers**, top to bottom, and the boundaries between them are the whole design. The first two
are both ENGRAM-side — §2.2's *ENGRAM layer* splits into them once cascade becomes a domain engine
(§2.3):

| layer | knows about | does NOT know about |
|---|---|---|
| **concept** — nodes, edges, properties, the epistemic vocabulary | node types, provenance, what a claim is | tables, rows, SQL, connections, transactions |
| **domain engines** — cascade, confidence, retrieval fusion | what operations MEAN; marker kinds, propagation, adjudication, confidence rules | tables, rows, SQL — they compose manager mechanisms and never touch storage |
| **manager** — `EngramManager`, abstract | the mechanism set and each mechanism's declared contract | which storage engine is under it; what any operation is FOR |
| **impl** — `SqliteEngramStore`, one of N | tables, rows, pragmas, WAL, checkpoints | why any operation exists |

**One split, two vocabularies — the mapping, because the document uses both.** §2.2's **Engine**
is this table's **manager** layer. §2.2's **ENGRAM layer** maps to **concept AND domain engines
together** — it is one box in §2.2 because §2.2 is arguing about where the line falls, and it
splits in two here because §2.3 made cascade a domain engine. **impl** is the SQLite layer §2.2
does not name at all. §2.3's **cascade engine** is in the **domain engines** row: ENGRAM-side,
above the manager, composing manager-layer mechanisms and never touching storage.

**PROCESS PLACEMENT, AND WHAT THE SOCKET CARRIES.** §3 puts the owner in its own process, §2.3 puts
the cascade engine *inside* the owner, and §2.2 puts confidence computation in the ENGRAM layer. Put
together those decide the wire, which is nowhere stated and has to be: **the owner process hosts the
concept layer, the domain engines, the manager and the impl. Everything else is a client. The socket
carries OPERATIONS — typed caller intents such as `create_node(derivation, …)` or
`retract_node(id, …)` — and never mechanisms; mechanism calls are in-process, inside the owner.**

⚠ **The two columns are two independent lists, NOT a row-wise pairing** — no client below sits
opposite "its" layer, and reading them across is exactly the layering misread the paragraph beneath
this table warns about.

| side | members |
|---|---|
| **owner process hosts** | **concept** (the epistemic vocabulary) · **domain engines** (cascade, confidence, retrieval fusion) · **manager** (`EngramManager` and §2.7's inventory) · **impl** (`SqliteEngramStore`, in the one process that holds `knowledge.db` open; that process may hold more than one connection, and does: the security slice's permanently-restrictive client-read connection plus an owner-internal one, `docs/2652-security-slice-design.md` §3.1) |
| **clients, over the socket** | the MCP tool bodies (`src/engram/server.py`) · the hooks (`src/engram/hooks/claude/*`) · the dream master and dispatched fairies · the `tools/*` CLIs · the sleep skill's cohort tooling · viz, the health snapshots and the sleep backup (readers, through `snapshot()`) |

**Why this has to be written down rather than left implied.** A builder who reads §2.0's *"mechanisms
with signatures"* as the **wire** contract puts the domain engines on the client side — and then every
hook that mints a node carries its own copy of the cascade rules, which is exactly the distribution
§2.5 measures and this whole design exists to remove. The performance argument points the same way and
is already in the document: cascade issues many small mechanism calls per resolution, and §2.3 calls
that affordable **because** they are in-process. At socket latency the same engine is a different
design, and would have to be built differently.

**`snapshot()` retires the backup shell channel (A5).**
`tools/engram-fix-git-backup.sh:127` opens the live store directly, through Python's
`sqlite3 .backup()`, to take its safety snapshot; the sleep cycle's nightly
`~/.engram/db-backup/knowledge-YYYY-MM-DD.sql` goes through `engram_backup.dump_stripped`
(`engram-sleep/SKILL.md:328-329`) — a Python API rather than a shell channel, and still a second
opener of the store. Under §3's shape both become a `snapshot()` request the owner serves from a
quiesced copy, and *"nothing else opens the live store"* stops needing an exception for either. Note
what `snapshot()` is in the layering, stated precisely because §2.0's table defines *operation* as a
caller intent **expressed through mechanisms** and `snapshot()` is not: it is a **client-facing
owner service** — a typed intent the socket carries, served by the impl directly, belonging to
neither column of §2.0's table. It is **not** a mechanism in §2.7's inventory, and the ground for
that is the derivation rule alone: no domain engine needs it. The document names the gap rather than
widening §2.0's *operation* to paper over it, because widening that word is how the
mechanism/operation distinction stops discriminating anything.

**SQLite is *an* implementation, named as one.** That is the load-bearing constraint and the
easiest to lose: the moment the abstract layer has a method whose only sensible implementation is
a SQL string, the split is decorative.

### 3.1 The phase-1 waypoint, and its asymmetry

Settled on #406 and restated here because it is the part most likely to be re-litigated:
**`read_sql` and `write_sql` were never the end-state.** They are a waypoint, and they apply
**asymmetrically**:

- **Reads — the waypoint is real.** A generic `read_sql` genuinely moves the dependency: callers
  stop knowing connections, cursors, transactions. The read surface (ranking, filtering, joins) is
  large and varied enough that typing it all at once is not realistic, so a shrinking allowlist is
  the honest path. **The allowlist itself is a TEMPLATE REGISTRY, not a keyword filter** — exact
  normalized statement templates registered by callers, matched by hash, default-deny. A "no
  INSERT in the string" blacklist lets `ATTACH`/`PRAGMA` writes through. The socket's permission
  model is a capability-channel question → **security slice: Aleph designs, Mira reviews**, named
  as its own slice rather than folded into the allowlist paragraph.
- **Writes — the waypoint relocates nothing.** A generic `write_sql` leaves the caller building
  SQL and knowing tables and columns. That is a rename, not a dependency move. The **production**
  write surface is **closed and enumerable**, which is what makes typing it from phase 1 affordable.

  **The population that claim ranges over, declared rather than implied** (the `series.db` exclusion
  in §4.3 is the template): it is the write sites under `src/engram` — the code the owner process
  must serve. **Repo-root `tests/` is EXCLUDED, and that is a scope DECISION, not a measurement.**
  It has to be said that way, because the tests are not a rounding error: `git grep -ioE
  '(INSERT( OR (REPLACE|IGNORE))? INTO|UPDATE|DELETE FROM)\s+(nodes|edges)\b'` at `e693a631`
  returns **307 statements across 92 files** under `tests/`, against **122** under `src/engram` for
  the identical pattern. Tests build fixtures by writing the schema directly and are not callers the
  owner displaces.

  ⚠ **The `20` that has circulated for this surface is a count of distinct write TARGETS —
  tables — not operations.** The source is my own comment on the epic, quoted verbatim
  rather than paraphrased:

  > *"The number that actually settles it — distinct write TARGETS, not statements:"* … *"20
  > distinct targets"* … *"208 write statements collapse onto 20 tables, and three of them —
  > `edges`, `nodes`, `nodes_fts` — carry 134, about two-thirds. A typed write contract is
  > therefore on the order of twenty **table-scoped** operations with a steep head, not two
  > hundred endpoints."*

  Note what it says: *table-scoped* operations, and its own footnote corrects 20 targets to
  19.

  **Kepler's re-measure on dev `7b68d385`: 194 DML sites, 30 `(verb, table)` pairs, 19
  tables.** His 19 tables and the footnoted 19 targets are the same unit landing on the same
  number — agreement between two instruments on two refs, not a replication, and his table
  set may differ from mine in ways neither of us has checked (`nodes_fts` and `vec_nodes` are
  `CREATE VIRTUAL TABLE`, `engram_core.py:2754` and `:2854`). His 30 pairs against 20 targets
  is not a disagreement at all: different units. The site counts are different populations
  too — 122 at `e33fd398`, his 194 at `7b68d385`.

  ⚠ **The operation count is UNMEASURED, and none of these figures bounds it in either
  direction**, because the pairs↔operations map fans out **both** ways and both directions
  are instanced in the population §3.1 ranges over:

  - **One pair, many operations.** `UPDATE nodes` is one pair spread across ~53 sites
    carrying supersede, retract, status flip, set summary, focus stamp, and utility boost.
  - **One operation, many pairs.** `create_node(derivation)` transitively issues, at least:
    `INSERT INTO sequences` (an upsert) via `_next_id` (`engram_core.py:4921`), reached at
    `:8649` as the operation's **first** write; `INSERT INTO nodes`; `INSERT INTO edges`
    twice (`derives_from` at `:8710`, `cites` at `:8718`); `UPDATE nodes` via
    `_stamp_new_node` (`:4480`); `DELETE FROM vec_nodes` + `INSERT INTO vec_nodes` (`:4718`,
    `:4720`) via `_compute_and_store_embedding`; `INSERT INTO edit_history` via `_log_edit`
    (`:4546`), which `_stamp_new_node` calls as its last act (`:4485-4490`); and a second
    `UPDATE nodes` from `_utility_reward` (`:4534`), reached at `:8732` with
    `action="derive"`.

    ⚠ **This document states NO COUNT for that operation, deliberately.** Three hand walks of
    this one call path, by two agents, each more careful than the last, returned **three
    different answers** — five pairs over three functions, then six over five, then seven
    over six. Every walk was honest, every walk read the code, and every walk missed
    something the next one found. A number that moves on each re-derivation is not a
    measurement. So the enumeration above is offered as **evidence that the operation is
    real and that hand-walking cannot size it** — which is the argument for generating it
    (§7) — and not as its size.

    **Two instrument questions the generator must answer, both surfaced by these walks and
    neither obvious in advance:** `sequences` is a plain `CREATE TABLE` (`:2453`) and inside
    Kepler's 27-table set, while `vec_nodes` is a `CREATE VIRTUAL TABLE` (`:2854`) and may
    fall outside it — so the table set is a declared input, not a given. And `_next_id`'s
    write is an `INSERT … ON CONFLICT … DO UPDATE`: **one statement carrying two verbs**,
    which a `(verb, table)` instrument has to rule on before it can count anything.

  So 30 pairs is consistent with an operation count above or below 30. The only statement
  this document supports is that the operation count **has never been measured** and must be
  **generated** — see §7.

  **Why the recommendation does not depend on any of these numbers:** its criterion was never
  the *size* of the write surface but whether that surface is **closed and enumerable**. That
  holds at any size, which is why no number in this paragraph is load-bearing.

And skipping the write waypoint is what keeps the read-back guarantee implementable: **a server
cannot promise "what you wrote is what you read back" when it cannot know what the write meant.**

---

## 4. Rider 1 — the population this displaces

Kepler's rider: name every current writer to `knowledge.db` that the owner displaces.
Re-measured on my own clone rather than carried from the epic.

### 4.1 The epic's number reproduces, and its scope is Python-source

`git grep -n 'sqlite3.connect' <ref> -- 'src/engram/'`, split by file extension:

| ref | ALL sites/files | `.py` only | non-`.py` |
|---|---|---|---|
| `1031ec21` (the epic's ref) | 74 / 33 | **56 / 29** | 18 / 4 |
| `7b68d385` (dev today) | 75 / 33 | **56 / 29** | 19 / 4 |

The epic states **55 sites across 29 files** at `1031ec21`. **Files match exactly;
sites differ by one**, and the off-by-one reconciles against the tree: exactly
**one** of the 56 `.py` hits at that ref is a comment rather than a call —
`src/engram/hooks/claude/engram-session-start-hook.py:324`, whose line reads
*"# No DB yet (or an empty shell left by sqlite3.connect) — nothing to back up."*
It is the only hit at `1031ec21` whose line does not contain `sqlite3.connect(`.
So the epic's population is **Python source**, and it is **stable**: unchanged at
56/29 across the two refs.

*Correction to draft 1, which cited the wrong note:* the epic's
*"(Was 'eight'; two of those were comment lines.)"* is attached to its
**journal-mode-setter** count (8 → 6), a different metric entirely — not to the
`sqlite3.connect` site count. Two would not have reconciled a difference of one
in any case.

*Instrument note, because it nearly cost me the reconciliation:* my first split
used the pathspec `src/engram/**/*.py`, which in git pathspec glob does **not**
match files sitting directly in `src/engram/` — so `server.py`, `engram_core.py`
and every other top-level module fell into the "non-Python" bucket and produced a
40/23 figure that agreed with nothing. The numbers above come from an extension
test on the emitted paths, not from a pathspec.

### 4.2 The channels the Python-source count cannot see — and this is the finding

Four non-`.py` files carry `sqlite3.connect`. Resolving each one's actual target
separates them cleanly, and **the split is not the one the file type suggests**.

**Counts below are at `7b68d385` (dev), the same ref as §4.1's second row.** The one
file that moves between the two refs is `engram-health-exam/SKILL.md`, which reads
**1** at `1031ec21` and **2** here; the other three are unchanged. That single site
is the whole of §4.1's non-`.py` delta — 18/4 at the epic's ref, 19/4 on dev — so the
two sections reconcile exactly, and neither number is a population change.

| file | sites | target | live store? |
|---|---|---|---|
| `tools/engram-pkg/engram-pkg` | 12 | `DB_FILE="$PKG_DIR/knowledge.db"`, `pkg_db_path`, `source_db` | **NO** — package DBs, correctly out of scope |
| `tools/engram-fix-git-backup.sh` | 4 | `DB_PATH="$ENGRAM_HOME/knowledge.db"` | **YES** — and line 127 is a `s.backup(d)` **write** |
| `skills/claude/engram-health-exam/SKILL.md` | 2 | `~/.engram/knowledge.db` | **YES** — line 160 opens it **read-write** (no `?mode=ro`) |
| `skills/claude/engram-first-session/SKILL.md` | 1 | `$ENGRAM_HOME/knowledge.db` | **YES** — read-only query, inline `python3 -c` |

So: **one shell channel that writes the live store, and two skill documents that
instruct an agent to open it directly.**

**The skill-doc channels are the sharper half, and they change what the guard
rail has to be.** The epic's guard rail is *"a CI gate fails the build on any
`sqlite3.connect` against the live store outside the owner module."* Written as a
Python-source check — the natural reading — it sees none of these four. But the
deeper problem is that the two SKILL.md channels are not code at all: they are
**instructions to a future agent**, so they regenerate the channel every time an
agent follows the skill, and no amount of migrating source code retires them. A
guard that scans only source files reports a clean tree while a documented
procedure is still telling agents to open the file.

**Consequence for the spec:** the guard rail is scoped to **every tracked file**,
not every Python module, with an explicit allowlist. **Key that allowlist on the
RESOLVED DB PATH, not on the file's type or directory** — "test targets" is the
same file-type axis **this section has just argued against**, and it is not sound here either:
`tests/test_cascade_playbox.py:477-493` documents a conditional path on which a
test process opens the real graph. A path-keyed allowlist covers that case; a
`tests/`-keyed one exempts it by name. And the migration checklist carries a **documentation leg** beside the
code leg.

**Guard rail runs on the BUILT tree too** — CI already builds the plugin, so run
the connect-scan on `build-plugin.sh`'s output. That is the tree that actually
runs, and §4.4's deployed-tree gap is the "build restructures paths" class.

### 4.3 The health series store is NOT the owner's population (Kepler's caveat, answered)

Kepler flagged that `health/series.py` carries **20 of the 194** DML sites (`l0_points`,
`derived_points`, `replay_runs`, `equality_checks`, `derive_checks`, `declarations`) and
asked whether that store is `knowledge.db` or its own file before counting them into the
owner's population.

**Measured: its own file — `series.db`.** `src/engram/viz/data/health_v2.py:22` states it
outright — *"`series.db` is `health.series`'s OWN"* store. `series.py` itself never names a
database; `open_store(db_path)` takes the path as a parameter.

**The decisive structural fact, which is stronger than the docstring:** the rebuild path does
`os.replace(str(rebuilt_path), str(db_path))` (`series.py:1484`) — it atomically swaps the
**whole file**. That is not an operation anything could perform on a live `knowledge.db`
under an open owner connection. A store that replaces itself wholesale cannot be the store
the owner holds open.

**Consequence for the population count above:** those 20 sites are **out of scope** for the
owner. The owner's DML population is therefore ~174 rather than 194 — **and both of those
numbers are KEPLER'S, not mine.** He measured 194 sites / 30 (verb,table) pairs on dev
`7b68d385`, over `git ls-files src/engram` `.py` only, with a DML regex restricted to the 27
tables that appear in a source `CREATE TABLE` (unrestricted, it inflates to 75 by matching
prose "UPDATE the ..." in docstrings). I verified the `series.db` *split*, not the counts, so
174 is his figure minus his own carve-out and inherits his instrument's scope. Re-measure
before it becomes a planning number.

What the split shows independently of any count: the tempting move — count every DML site in
`src/engram` — assigns the owner a tenth of a surface that writes to a different database
entirely.

### 4.4 What is still unmeasured here, stated so it is not read as covered

- **Writers vs readers among the 56.** The count is connection sites, not write
  sites. The displaced-*writer* inventory the rider actually asks for needs a
  second pass that classifies each site by whether it executes a mutating
  statement. Not done here.
- **The deployed trees.** The epic records 58/28 on Kepler's seat and 53/23 on
  mine (Aug-30) — the shipped plugin tree differs from source by construction.
  Any guard that runs in CI protects the source tree only; a seat running an
  older deployed tree keeps its channels until it rebuilds.

---

## 5. Rider 2 — what a caller sees when the owner is down

Kepler's rider: fail-loud shape, no silent fallback to direct SQLite. Taking it
further than "it fails", because the failure *shape* is the whole contract.

### 5.1 The rule

**There is no fallback path. A client that cannot reach the owner returns a
distinct `unavailable` status and does nothing else.** Specifically it must never:

- open `knowledge.db` itself "just this once" (that is the split-brain the epic
  exists to remove, re-entering through the error path);
- return an empty result, a default, or a zero (a manufactured null that reads
  as a true answer — the failure class this fleet keeps measuring);
- retry silently past a bounded window (a hang is indistinguishable from a slow
  graph, and the caller cannot tell which it is waiting on).

**"Bounded" means one deadline per request, end to end, on both sides of the
socket, not a timeout per socket operation.** A per-operation timeout restarts
on every byte, so a peer that trickles data never trips it. Measured on U1
(#3102): with `read(timeout=2.0)`, a silent peer gave `unavailable` at 2.0s, but
a peer sending one byte per second held the call for 30s. It stopped only
because the test peer closed the connection. Aleph reproduced the same shape on
Darwin and in stdlib `urlopen`. So the client computes its deadline once,
checks it again before every connect, send and receive, gives each of them only
the time that remains, and stops with `unavailable` once no time is left. One
tempting shortcut does not meet this: passing the remaining budget once, as a
single library call's `timeout=`. The library still applies that value to each
socket operation inside the call. Aleph measured that form against a
trickling server: it respected a 3s budget against a silent server, but against
the trickle it ran until the server chose to finish. Its bound is the server's
patience, which is to say it has none of its own. A test that uses only a silent peer passes
both forms, so the contract test needs a trickling peer and must assert on
elapsed time. The exception type does not tell the two forms apart. The owner
applies the same rule to each client it serves, timed from accept, so that one
stalled caller cannot hold the others (#3096 contract items 8–10).

### 5.2 The three states a caller must be able to distinguish

The reason this needs three and not two: today's #2635 shape is a write that
reported `created` and was lost, and a two-state contract cannot express it.

| state | meaning | caller sees |
|---|---|---|
| `ok` | the operation completed AND the owner read the committed row back | the result |
| `unavailable` | the owner could not be reached, or did not answer within the bound | an explicit error naming the socket path and the bound, plus §5.5's two-kind recovery guidance (agent-actionable / human-only) — never a result |
| `rejected` | the owner was reached and refused (schema, contract version, validation) | the reason, distinguishable from `unavailable` at the type level |

**`unavailable` and `rejected` must not collapse into one error type.** They have
opposite remedies — restart the owner vs fix the call — and a caller that cannot
tell them apart will retry a permanent rejection forever.

### 5.3 Contract version skew

**Contract skew:** client sends `contract_version` on connect; MAJOR mismatch →
`rejected(reason=contract_version, remedy=restart owner)`; MINOR accepted. No
negotiation protocol.

### 5.4 The read-back acknowledgement

A write returns `ok` **only after the owner has read the committed row back**.
This is what makes the #2074 / #2635 shape structurally impossible rather than
patched at one call site, and it is the single strongest argument for typed
writes over a generic `write_sql` — the owner has to know what the write meant
in order to read it back.

**Idempotency key on every write.** `request_id`, client-generated, owner dedupes.
Without it, "owner commits → socket drops before ack → client sees `unavailable`" makes
a retry duplicate. The three states describe what the OWNER did; they do not describe
what the CLIENT may safely do next, and for writes those are different questions.

### 5.5 Degraded mode — SETTLED (Lei, 2026-09-24 13:45Z)

**Ruling, quoted rather than paraphrased:**

> "hard stop for write, and don't serve stale read, just LOUDLY say ENGRAM is not
> available, and provide the immediate recovery guidance so the agent can
> self-recover or surface to the user for whatever only human users can do, e.g.
> restart session or run mcp refresh, etc."

**What the ruling itself requires** — each bullet traces to the quote:

- **No degraded mode of either kind.** A write while the owner is unreachable is
  refused ("hard stop for write"). A read is refused too; no snapshot is served in
  its place ("don't serve stale read").
- **The `unavailable` response says so LOUDLY and carries immediate recovery
  guidance in two kinds**: steps the calling agent can take itself ("self-recover"),
  and steps it must surface to its user because only a human can do them ("restart
  session or run mcp refresh, etc.").

**Design inference — the author's, NOT part of the ruling**, stated separately so
a reader can disagree with it without disagreeing with Lei:

- The guidance travels **in the response payload, not a log line**, and the two
  kinds are **distinguishable without parsing prose** (e.g. separate fields). The
  reason is the same as §5.2's type-level split of `unavailable` from `rejected`: an
  agent acts on structure it can branch on, and a log line or a paragraph is where
  guidance is silently lost.
- **"Loudly"** is read as: the agent cannot mistake the response for an empty or
  slow answer — §5.1's no-manufactured-null rule, applied to the whole mode.

The analysis below is kept as the reasoning the ruling adopted.

The epic asks: read-only degraded mode from a checkpointed snapshot, or hard
stop? **Today's answer is neither, which is how "created-but-lost" happens.**

My lean, held loosely and flagged as the thing I most want argued with: **hard
stop for writes, and no degraded reads either.** A read-only degraded mode
serves a stale graph that *looks* current, which is the same false-confidence
failure as a silent fallback wearing a safer name — and an agent cannot tell
from the content that it is reading a snapshot. If degraded reads ship anyway,
every response from that mode must carry the snapshot's own timestamp in the
payload, not in a log line.

**What would decide it:** how often the owner is actually down, and for how
long. Nobody has that number, and it is measurable from the moment the owner
exists — the owner's own `unavailable` occurrences are the first event type the
#2574 telemetry store records, so this is measured from day one rather than
hoped for. This is the one place where I would rather ship the strict version
and relax it on evidence than ship the permissive version and never learn.

---

## 6. Migration order, and the one property that has to hold

Order from the epic, unchanged: **hooks → tools → viz → MCP server last.** Hooks
first is highest connection churn and lowest risk; the MCP server last because
it is the one whose behaviour agents feel.

**The cost, stated for Lei:** until the MCP server migrates, two processes write
the live store, so the read-back guarantee covers only owner-mediated writes and
the #2635 class stays open on the server path — the path where the nine
most-silent sites live. The order trades the biggest defect for the smallest
risk. Defensible, but it must be a stated choice.

The property that has to hold across the whole thing, and the reason for
branch-by-abstraction rather than a migration branch: **the migration runs
alongside daily feature work in a repo doing ~11 merges a day.** So — introduce
the interface in place on `dev`, route callers through it incrementally, keep
both implementations live behind it, delete the old one when the last caller has
moved. Every step independently mergeable and revertible; no long-lived branch;
no big-bang cutover.

### 6.1 The equality control, and why coverage is the wrong instrument

Each phase keeps the old path behind a flag until its equality control passes.
The control is **differential testing** — old and new against the same inputs,
outputs compared **including the wrong ones**, since a migration that fixes a bug
in passing is not a migration. Coverage tooling is worth having for finding
untested paths; it cannot answer this question, because 100% line coverage is
fully consistent with different behaviour.

Two disciplines on the harness itself:

- **The harness must be shown to fail.** One that passes because both sides
  return empty is the manufactured-null shape wearing a green tick.
- **Mutate the side with no independent coverage** — the NEW implementation.
  Mutating the old one reddens tests that already existed and proves nothing
  about the harness.

#### The hook-latency criterion (A6), and it is a DELTA rather than a number

**The criterion, stated once so it is not paraphrased into a second gate:** *the
hook's owner round-trip must not exceed the same hook's measured direct-connect
path, on the same store, same host, warm cache, same N.* **The baseline is
re-measured at phase start and never pinned in this prose**, because the absolute
moves with host, build and cache state while the delta is what the design
controls.

**Phase 1 accepts only when that delta is measured against the REAL owner's
dispatch** — deserialize, route, requirement checks, execute, read-back,
serialize. The day-zero socket figure below was taken against an **empty echo
server** and therefore measures transport and nothing else, so it does not
satisfy this criterion and must not be read as covering a path that has not been
built. Phase 1's measurement also adds a **contended arm** (two clients
concurrently) and a **cold-cache arm**. **Those two are measurements phase 1 owes,
NOT additional gates** — the gate is the one warm-cache delta above. Each is reported
with its own delta against the **same-arm** direct-connect baseline, and **an arm where
the owner loses to direct connect is a stated finding in the phase report**, never a
silent pass. That keeps the gate to one sentence and still makes the losing case
impossible to omit.

**The day-zero datum, with its instrument and its bounds** — offered as a
measurement, not as a bound. This seat, WSL2, plugin venv; `~/.engram/knowledge.db`
at 178.1 MB with 54 objects in `sqlite_master`; N=300 per arm, load arm repeated
three times (p50 0.459 / 0.453 / 0.455 ms); all DB access `mode=ro`; warm page
cache; single client, no contention:

| arm | p50 | p99 |
|---|---|---|
| connect + close, no statement | 0.015 ms | 0.027 ms |
| **connect + 1 point read + close** — today's per-call hook path | **0.455 ms** | **0.60–0.69 ms** |
| first statement on a fresh connection | 0.402 ms | 0.538 ms |
| second statement on the same connection | 0.003 ms | 0.011 ms |
| unix-socket round trip, cross-process, **empty echo server** | 0.022 ms | 0.060 ms |

**Controls, and the second version of them corrects the first.** `connect` is not
lazy — connecting to a nonexistent `mode=ro` path raises immediately — so the
0.015 ms open is real and cheap, and the cost lives in the **first statement**.

⚠ **The original control varied TWO things and could not have established what it
was cited for.** A freshly-created sqlite database defaults to `delete` journal
mode; the live store is WAL. So "1-table db vs live graph" moved schema size AND
journal mode together, and the **33×** it yielded was the product of both.
Re-run with the arms separated, N=300 each, first statement on a fresh connection:

| arm | schema | rows | journal | p50 |
|---|---|---|---|---|
| live store | 54 objects | 43,481 pages | WAL | **0.4045 ms** |
| full schema, **most rows deleted** ⚠ *uninformative — see below* | 54 objects | 27,113 pages after DELETE+VACUUM | WAL | **0.4280 ms** |
| tiny, journal matched | 2 objects | 1 row | WAL | **0.0421 ms** |
| tiny, original control | 2 objects | 1 row | delete | 0.0120 ms |

**The conclusion survives and is now properly supported; the number does not** —
but NOT on the row the reader's eye lands on first. The delete arm reads as
unchanged within noise (0.4280 vs 0.4045), and the paragraph below explains why
that row supports nothing at all. **Schema, not data** rests on the `SELECT 1`
measurement UNDER SQLITE 3.53.0 — the build hooks run, and the only one where the
first statement pays schema load; under 3.45.1 the same query costs 0.002 ms and
bears on nothing here. The tiny arm (0.0421 vs
0.4045, journal matched) does NOT isolate it: at 2 objects and 1 row against 54
objects and 43,481 pages it varies schema size and data volume TOGETHER — the
exact defect this section convicts the original control of, two arms later. It
bounds the combined effect and nothing finer. `SELECT 1` is the only measurement
here that moves one variable: same file, all 43,481 pages present, no table
referenced.

⚠ **That arm is UNINFORMATIVE — not merely weaker — and saying so is the point.** The
script copied the live store, set WAL, ran `DELETE FROM` each table inside a
`try/except: pass`, committed and VACUUMed, **so any table whose delete raised was
silently skipped**. Four did: `nodes`, `vec_nodes`, `nodes_fts`, `nodes_fts_vocab`.
The surviving rows are therefore **precisely what the measured read touches**, which
makes the arm evidence of nothing rather than weak evidence. **It is not an
emptying and must not be read as one**: 43,481 → 27,113 pages, a 37.6% reduction
that leaves the read's own data in place.

**The conclusion stands on a different datum, which is independent and decisive:**
`SELECT 1` on the live store references no table and costs 0.002 ms under sqlite
3.45.1, against ~0.40 ms under 3.53.0 — same file, no rows read either way. Schema,
not data. The deletion arm is retained only as the record of a control that could not
have worked, because a `try/except: pass` around the step that CREATES a treatment arm
is how an arm gets reported stronger than it was run. The journal-matched schema effect is **9.6×**, not 33×; the
remaining 3.5× in the old figure was journal mode, which the design does not
change.

⚠ **The 2.5× spread across runs is the SQLITE BUILD, and isolating it falsifies the
sentence that used to sit here.** That sentence said the spread was unexplained but
that *"the ratio between arms held in both runs"* — and the ratio does **not** hold.
Reproduced interleaved on one host, same script, same store, alternating
interpreters, twice each:

| build | interpreter | live first statement | tiny-WAL | ratio | `SELECT 1` on live |
|---|---|---|---|---|---|
| sqlite **3.45.1** | system python 3.12.3 | 0.157 / 0.162 ms | 0.040 / 0.042 | **3.94 / 3.84×** | **0.002 ms** |
| sqlite **3.53.0** | venv python 3.14.4 | 0.406 / 0.402 ms | 0.041 / 0.040 | **9.92 / 10.01×** | **0.400 / 0.391 ms** |

**`SELECT 1` is the discriminator and it gives the mechanism**: it references no
table, and costs 0.002 ms under 3.45.1 against ~0.40 ms under 3.53.0 on the same
file. The newer build pays schema load on **any** statement; the older defers it to
the first **table reference**.

**So two things above are corrected rather than caveated.** The `9.6×` is not a
property of the store — it reproduces under 3.53.0 and not at all under 3.45.1. And
no arm-to-arm ratio is transferable across builds.

**What survives, and why the criterion is still the right shape.** Data volume is
still not the driver — but NOT on the delete arm, which is dead for the separate
reason given above; it rests on `SELECT 1`, which references no table and is
measured with every row still in place. And **the A6 gate is
same-build BY CONSTRUCTION**: it compares a hook's owner round-trip against *that
same hook's* direct-connect path, on one host, so the build cancels in the delta
and does not cancel in a level. That is the strongest argument yet for a delta
rather than a pinned number, and it was produced by the measurement failing rather
than by the design being defended. **Operationally the 3.53.0 arm is the one A6
ranges over**, because hooks run under the venv python — a scoping fact, not luck.

**Why that is an argument for this design and not merely a number.** The
per-connection cost the owner amortises is the one paid at **SCHEMA LOAD** — not
on file size, not on the query, and demonstrably not on data volume — and §2.4
says the engine's generated schema is exactly what will grow. **That the cost
SCALES with schema size is the natural reading and is NOT measured here**: the
only schema-varying arm left bounds the combined effect and nothing finer. What
is measured is that the cost is paid at schema load, on a query that references no
table while every row is still in place — which is what makes a growing generated
schema the thing to watch. So the epic's stated worry (*"today's per-tool-call
connection is ~ms; the socket must not be slower"*) points, on this evidence, the
other way. **What none of it bounds:** the owner's own dispatch, which is the
whole content of the acceptance condition above.

### 6.2 The phase-2 recall gate, as settled on #406

Per-seat **differential** run (same set, same graph, before and after), plus a
**second seat** running the same committed harness against its own locally-curated
set. Two independent zero-deltas on two graphs replicates the *gate* without any
graph being copied anywhere.

The limitation goes in the SPEC in one sentence rather than being left implicit:
*the golden set is a per-seat private artifact; the migration gate is a
differential run by the seat that holds it, reporting the delta.*

And the provenance note beside it, because the fact should travel rather than my
old gloss on it: the harness's "why local not CI" line was about **what the
artifact is**, not about **where it runs** — and the snapshot "pin" was a string
literal in the reporting layer until #2806, structurally incapable of disagreeing
with the file actually opened.

---

## 7. Open, and whose call each one is

**One question left this table on 2026-09-11 — whether the mechanism inventory is MEASURED from the surfaces or DERIVED from constructing the domain engines. It is settled, DERIVED, and the ruling is filed in §2.0** alongside the principle it qualifies, rather than restated here.

| question | mine to propose | Kepler's | Lei's |
|---|---|---|---|
| **the typed write operation set — GENERATE it, and NOT from statement enumeration.** §3.1's counterexample rules that instrument out: `create_node(derivation)` consumes *many* `(verb, table)` pairs across many functions, so anything enumerating **statements** — SET-signatures included — partitions one operation into several and cannot recover the intent. **The generator must walk CALL PATHS** from each caller-facing write entry point through the helpers it reaches, recording the pair-set and the **site**-set each root consumes. **THE CHECK IS SITE-LEVEL, NOT PAIR-LEVEL** — a union over pairs cannot detect a missed path, because `INSERT INTO edit_history` has 9 sites across 5 files and the other 8 keep the union complete. **THE ROOT SET IS §2.0's SURFACE LIST**, and a write site with no function-level entry point is its own root — two hooks are exactly that (`engram-surface-hook.py:955`, `engram-utility-credit-mention-stop.py:145`), both on `UPDATE nodes` (53 sites), so omitting a whole surface also passes a union test. ⚠ **That clause must be qualified or it eats the check: if EVERY site may promote itself to a root, then "a site no root reaches" is definitionally empty and the site-level test proves nothing.** The qualification is that self-promotion is a *declaration made when the root set is built*, not a fallback applied when the walk misses — a site is its own root because a surface owner named it, and an undeclared site the walk fails to reach is still **a finding**. **Any unreached site is a finding; that criterion is what the site-level check delivers, and without it the check has no output.** **THE HOLES BELOW ARE THE ONES KNOWN TODAY — the list is open, not closed,** and it is stated rather than papered over because a check asserted sound is worse than one asserted incomplete: **(a) the check is still ∃-over-roots while the DELIVERABLE is the per-root MAPPING** — if the walk resolves `_stamp_new_node`→`_log_edit` from the observation root but not the derivation root, `:4546` stays claimed and the miss passes unchanged; validating the mapping needs a per-root expectation the walk cannot supply about itself. **(b) the root set is PRESCRIBED, not CHECKED** — nothing detects an omitted surface. **(c) the site universe inherits Kepler's `.py`-only scope, which §4.2 argues against by name** — the two SKILL.md channels are not reachable by any Python-source walk. **(d) the site universe is scoped to Kepler's 27-table set** — a DIFFERENT population from the **19 tables** attributed to him in §3.1, and the document should not use one name for two numbers: §3.1's 19 is his count of tables carrying DML in the write census, this 27 is the table set the site universe is scoped to, and neither figure states that distinction where it appears. Which §3.1 shows is a declared input rather than a given — `sequences` is inside it, `vec_nodes` may not be, and both appear in §3.1's own worked example. *Four attacks were tried and did NOT land, recorded because a check nobody has failed to break is not yet trusted: dynamic SQL, try/except-only helpers, unimported modules, and — refuted only once the self-promotion clause above is qualified — a site promoting itself out of reachability.* *(Naming tables in the instrument does not violate §2.0's backend-neutrality rule — that rule governs MECHANISM SIGNATURES; this is a measurement pointed at the current sqlite impl, and the backend-specific surface is exactly what it is measuring.)* **And one scoping caveat on the output:** a walk yields ENTRY POINTS, which are signatures, and one entry point can carry several intents — `engram_update_task` (`server.py:5721`) rebalances importance across four statuses through a single signature. Splitting an entry point into its invocations is a further step the walk does not perform. | ✓ | ✓ | |
| writers-vs-readers split of the 56 sites | ✓ | | |
| ~~degraded mode: hard stop vs read-only snapshot~~ — **SETTLED 2026-09-24, see §5.5**: hard stop for writes, no stale reads, loud `unavailable` with two-tier recovery guidance | lean stated | | ✓ ruled |
| supervision per platform (systemd --user / launchd) | | ✓ | |

---

## 8. What is NOT done

- Not enumerated the typed operation set. It should be generated by §7's call-path
  walk — **not** from the write measurement, which §3.1 shows cannot recover an
  operation, and not drafted from imagination.
- Not classified the 56 Python sites into writers and readers (§4.4).
- Not costed the hook path against the epic's performance envelope ("today's
  per-tool-call connection is ~ms; the socket must not be slower").
- **§2.7's mechanism inventory is v0 and INCOMPLETE — its gap table is the list, and
  read the paragraph under it, which says the table is a FLOOR rather than a total.**
  Deliberately not restated here: a second copy of that list is how this document has
  repeatedly contradicted itself. Separately and as expected, the confidence engine
  and retrieval fusion have not been constructed against the inventory at all, so
  mechanisms they need (candidate generation for FTS and embedding, at least) are
  missing.
- **The inventory is DERIVED and UNCHECKED.** §2.0's item 1 has two halves: the
  derivation, and the check that every site in the store-touching census is
  reachable through it. Only the first is done. The second needs the census §7's
  generator produces, which is the not-done item above it.
- **A6 is now a criterion in §6.1 and a day-zero datum, and what remains is the
  measurement that criterion actually requires.** The delta is stated and the
  direct-connect baseline is measured; the owner-side arm is not, because the
  owner does not exist — today's socket figure is an empty echo server and
  measures transport only. Phase 1 owes three arms nobody has run: the real
  owner's dispatch, a contended arm, and a cold-cache arm.
