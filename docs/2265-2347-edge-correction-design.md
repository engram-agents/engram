# Correcting wrong edges, and what is left of the acknowledgement problem

Design note for **#2265** and **#2347**, both part of the claim-bearing partition epic
**#2252**. Written for Lei's read. Nothing here is built. Author: Ariadne, 2026-09-25.

## What Lei is asked to decide

1. **#2265: add one agent-facing verb, `engram_retype_edge`**, that corrects a wrong
   edge by making it *weaker* — never stronger. Yes / no / change the shape.
2. **#2347: close the original issue as fixed**, and keep the acknowledgement guard as
   it is (refuse the whole call). The only nodes it strands are a small, closed set
   made before the guard existed. Handle them once, by their owner, not with a new
   rule. Yes / no.
3. **Stage 3 of #2252: fix exit query 5b** so it checks only relations marked
   `temporal_causal: True`. This is a bug fix to the plan, not a design change. It
   is listed here because the wrong version made #2347 look like it blocked Stage 3.

The rest of this note gives the reasons.

---

## 1. What already shipped (so nobody re-argues it)

Both issues were filed before later work changed the ground under them. Checked at
`dev` `b80a2ce1`, from the code, not from the issue threads.

- **#2360 (merged 2026-08-29)** split the old `dag_check` flag in two: `dag` (no cycles
  allowed) and `temporal_causal` (source must be newer than target).
- **#2349 (merged 2026-08-29)** added the relation `acknowledged_derives_from`, with
  `temporal_causal: False` and `dag: True`. It also added a write-time guard: an
  acknowledgement edge that would close a cycle is refused
  (`_cascade_record_acknowledgement_edges` raises `_CascadeCycleError`, and
  `engram_cascade_resolve` turns that into a refusal with nothing written).
- **`tools/surgical.py`** already has `edge_add` and `edge_remove`. They have two
  phases (preview, then execute with a token), take a git snapshot, and write an
  edit-history entry. `edge_remove` does **not** check the `removable` flag, so it
  *can* delete a `supported_by` edge today.

So #2347's original contradiction (the acknowledgement edge is always backwards in
time, and it can close a cycle) **is solved**. The first half is solved by the new
relation, the second by the guard. What is left of #2347 is smaller; see section 3.

## 2. #2265 — correcting a wrong edge

### The problem, as it stands today

An agent can know an edge is wrong and still have no normal way to fix it:

- `supported_by`, `derives_from` and `cites` are `removable: False` and
  `addable_after_creation: False`. `engram_remove_edge` refuses them.
- `surgical.py` can do it, but it is the wrong tool for this job, for three reasons:
  - It ships in the **developer tier only**. A normal install does not have it.
  - Lei's standing rule: **no surgery without his explicit agreement and a written
    plan.** It is an emergency path, not a routine one.
  - Remove, then add, is **two separate steps**, and neither step recomputes the
    source node's confidence. Removing a premise edge changes the confidence.

The need is real. There are three kinds of repair, and two are more than one edge:

| repair | size | where it comes from |
|---|---|---|
| a `supported_by` edge that is only "related" → `about` | 1 edge known (on a superseded node) | #2265's own case |
| `derivation --derives_from--> lesson`, used as an example → a non-premise relation | 28 of 29 in the partition audit; 19 on my graph | #2252 Stage 3 / W3 |
| the same, but the lesson is really used as a rule | 1 in the audit | needs a relation not built yet (spec Q3) |

### Proposal: `engram_retype_edge` — correction may only make an edge weaker

One MCP tool, Essential tier (it repairs the core graph, so it ships with it).

**The rule that makes it safe:** a re-type may only move an edge to a relation that
claims **less**. It can turn "B is a premise of A" into "A is about B". It can never
turn "A is about B" into "B is a premise of A".

Why this line: making an edge weaker only takes a claim of support *away*. Making it
stronger after the fact would create support that the node did not have when it was
written. That is the thing `addable_after_creation: False` rightly protects against
(Borges's point on #2265, 2026-08-26). So adding `cites` or `derives_from` later
stays blocked. This proposal does not touch that.

**Allowed moves (a fixed table in `engram_core`, next to `EDGE_CLASSIFICATIONS`):**

| from | to | when |
|---|---|---|
| `supported_by` | `about` | the target was related, not a premise |
| `derives_from` | `about` | same |
| `derives_from` (target is a lesson or cornerstone) | `exemplifies` | the source is an example of the principle |
| `cites` | `about` | the target was not really a source |

Any move that is not in the table is refused, and the refusal lists the moves that are
allowed. The normative "applies a lesson as a rule" case gets its own row once that
relation exists. It is not invented here.

**What the verb does, in one transaction:**

1. Refuse if either end node has a pending cascade marker. Settle the cascade first.
   (Same rule as `surgical.py`.)
2. Refuse if the new relation fails its own add-time checks (`temporal_causal`
   direction, `dag`).
3. Change the edge's relation. The endpoints stay the same.
4. Write an edit-history entry with the old relation, the new one, and a **required**
   reason. The old edge is kept in the record, never silently lost.
5. If the old relation fed the confidence formula, recompute the source node's
   confidence. Then run the normal downstream recompute. Report old → new confidence
   in the result.

**What it serves:** the one wrong edge in #2265; the Stage 3 / W3 lesson-edge repair
(which can then run on every user's graph, through a normal verb, as #2252 asks —
"repair the legacy violators THROUGH the machine"); and any future relation rename
that needs existing edges moved.

**What it does not do:** it does not clear stale markers (#1637 is a different
operation, and the cascade machine may already cover it; to check separately). It
does not delete edges. It does not make any edge stronger.

**Contract for the build** (write these tests first, per the project's contract-first
rule):
- a move in the table succeeds, and the edge keeps its endpoints;
- a move not in the table is refused, and nothing changes;
- a stronger move (`about` → `supported_by`) is refused;
- a node with a pending marker is refused;
- removing a formula-input premise recomputes the source's confidence, and the change
  is reported;
- the edit-history row holds both relations and the reason;
- the whole call is one transaction: a failure in step 5 leaves the edge unchanged.

**Blast radius:** the verb itself is small. The risk is in step 5: it changes stored
confidences. That is why the moves are a fixed table and not free-form, and why
this verb is on Lei's read queue as substrate.

## 3. #2347 — what is left

### The original contradiction: solved, close it

See section 1. The time inversion is handled by the new relation. The cycle is
handled by the write-time guard. My 2026-08-28 comment on #2347 proposed the same two
parts (an exemption for the time check, and a cycle check before acknowledging).
They were built, as a separate relation rather than an exemption.

### What still hurts: one bad premise blocks the whole node

When acknowledging a node would close a cycle for **one** of its premises, the guard
refuses the **whole** call. The node's other, sound premises cannot be settled
either (Borges, #2347, 2026-09-01).

**How big is this, measured:**

- **My graph, today:** 59 cascade markers, all resolved (49 acknowledged, 10
  superseded), **zero pending**. Nothing is stuck.
- **Borges's graph, 2026-09-01:** 60 pending rows on 14 nodes. 14 rows were
  ill-formed, and all 14 trace back to **three supersedes made before the guard
  existed** (2026-04-21, 04-24, 05-12). Today's supersede guard refuses that shape,
  so no new ones can form. The set is **closed**.
- Not re-measured since 09-01. I cannot read Borges's graph from my seat, and he may
  have settled some of it since.

One caution on those numbers: Borges tested "does the replacement come from the
*root* being replaced". The guard tests "does the replacement come from *this
node*". These are not the same test, so not every one of his 14 rows is certain to
be refused. His count is the right order of size, not an exact count of stuck nodes.

### Proposal: keep the guard strict; do not change the per-node ruling

Lei ruled that `engram_cascade_resolve` works **per node**, not per premise (#2013
§10.2: "Per-NODE, not per-marker (Lei's ruling; matches §9.4 "multi-event nodes
adjudicate once")"). Changing
that would be a real change to a settled design. The only measured cost is a
**closed** fossil set on one graph. A closed set is a reason for a one-time fix, not
for a new rule.

So:

1. **Leave the guard and the per-node verb as they are.**
2. **Borges re-measures his graph** with the guard's own test
   (`_cascade_reaches(conn, node, replacement)`), not the root test.
3. If anything is still stuck, he settles it **once**, node by node, as its owner.
   `superseded` works when the new node is written without the circular premise.
   Supersede does not force the lock one step down, as long as the replacement node
   does not again derive from the thing that derived from it. (`supersedes` is not a
   cascade relation, so the cycle guard never walks it.) This covers only that
   shape. It is not a general guarantee: a node that already derives from the
   superseded node can still be refused later, if the new replacement is
   reachable from it. That case is what step 4 is for.
4. If the re-measure finds nodes that **no** honest resolution can clear, that is new
   evidence, and it comes back to Lei as its own question.

**Another option, not recommended now.** For a premise whose replacement already
derives from the node, acknowledge could skip the edge for that premise instead of
refusing. The argument for it: that node sits *upstream* of the replacement, so it
does not rest on the replacement, and §4's reason for the edge (seeing a later
supersede of the replacement) does not apply. I argued the opposite on 2026-08-28,
that such a node "cannot honestly say its claim is unchanged". I now think that
argument assumed acknowledging must mean "I now rest on the replacement", which is
the thing in question. Both readings are defensible. Choosing between them changes
the meaning of a shipped verb. With no live population to justify it, I would not
make that change today.

## 4. Stage 3, exit query 5b — a fix to the plan

The plan's query 5b counts cascade edges that break creation order. As I ran it on
2026-09-06, it did not read `temporal_causal`. So it counted all 26 of my
`acknowledged_derives_from` edges. Those edges are backwards in time **on purpose**.
I then reported that 5b "cannot read zero while #2347 is open". That was wrong
(corrected on #2252, 2026-09-25).

Fixed query: count only edges whose relation has `temporal_causal: True` and whose
source is older than its target. With that fix, Stage 3's real remaining work on my
graph is the 19 `derivation --derives_from--> lesson` edges (queries 1 and 3). That
is the work section 2's verb would do.

## What this note did not check

- It does not check whether the confidence formula reads superseded premises (it
  matters for option B in section 3, which is not recommended).
- It does not check whether #1637 is now fully covered by the cascade machine.
- It uses Borges's 09-01 counts for his graph; they were not re-measured.
- All other counts are from one graph (mine).
