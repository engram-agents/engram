# Cascade scenarios — behaviour acceptance for the #2013 cascade machine

One file per case. Every case is written as a **progression**:

```
SETUP    — build a named topology (tests/cascade_playbox.py), state the
           starting facts, snapshot anything you'll compare against later.
ACTIONS  — drive the REAL machine (supersede / retract / resolve). Never
           hand-write marker or edge state.
EXPECT   — assert observable properties at each stage, with the
           matchers in _dsl.py so a failure message names the node, the
           stage, and the expected-vs-actual in plain words.
```

The three sections appear literally, as comments, in that order in each
test body. The module docstring at the top of each file restates the case
in prose: what shape, what happens to it, what the machine must do. A
reader should be able to accept or reject a case from the docstring alone,
then read the body only to confirm the assertions match the prose.

## The topology every case starts from

`converging_diamonds` (Lei, 2026-08-29):

```
      A       B          two independent root observations
       \     / \
        \   /   \
      C ─┼─┘  ┌─ D       C and D EACH derive from A and B
       \  \  /  /
        \  \/  /
         \ /\ /
          E              E derives from C and D
```

`[A, C, D, E]` is one diamond, `[B, C, D, E]` another. Superseding or
retracting both roots is the **double-event** case: C, D and E each carry
two pending markers, and every per-node / one-judgement / trivial-
resolution rule is exercised on the same three nodes at once.

`A2` (weaker source than `A`) and `B2` (same strength as `B`) are minted
alongside so a supersede scenario can show confidence moving on one leg
and not the other.

## Matchers (`_dsl.py`)

| matcher | asserts |
|---|---|
| `expect_ok(result)` / `expect_refused(result, mentions=...)` | a verb succeeded / was refused with a message naming the given text |
| `expect_frontier(box, names, mode=..., node=...)` | exactly these nodes are on the frontier (mode B, or mode C scoped to `node`) |
| `expect_pending(box, node, events=n)` | node carries exactly `n` pending markers (distinct events) |
| `expect_resolved(box, node, kind=...)` | node has no pending markers and its markers resolved as `kind` |
| `expect_nothing_pending(box)` | frontier empty and `total_pending_nodes == 0` |
| `expect_edges(box, relation, pairs)` | exactly these (from, to) handle pairs exist for the relation |
| `expect_confidence(box, node, changed_from=... / unchanged_from=...)` | confidence moved / stayed |
| `expect_node_count(box, n)` | the node table has exactly `n` rows (pure-wire checks) |
| `expect_clean(box)` | the real #2300 conformance flare reports zero red flags and no cascade cycle |

A red matcher is a **finding against the PR whose claim the case names**,
not against the harness: every transition went through `engram_revision.py`.

## Case index

| file | one line |
|---|---|
| `test_cd01_double_event_markers.py` | superseding both roots marks C, D, E twice; only C, D are on the frontier |
| `test_cd02_one_judgement_clears_both_events.py` | one ack on C clears both markers and writes edges to A2 and B2; no node minted |
| `test_cd03_trivial_auto_resolution.py` | ack C and ack D → E auto-resolves as acknowledged and never surfaces |
| `test_cd04_non_trivial_surfaces_e.py` | ack C but supersede D → E DOES surface and needs a judgement |
| `test_cd05_retract_blocks_acknowledge.py` | retract A (instead of supersede) → C/D may only be retracted or superseded |
| `test_cd06_confidence_propagates.py` | ack C after A→A2 (weaker): C's confidence drops; E's follows transitively |
| `test_cd07_cycle_guard.py` | supersede C with E (its own descendant) is refused; graph unchanged |
| `test_cd08_supersede_marked_node.py` | plain supersede of an already-marked C must not open a second event — **xfail #2343** |
| `test_cd09_drive_to_empty.py` | resolving frontier-by-frontier terminates with nothing pending and no anomaly flag |
| `test_cd10_retract_non_current.py` | plain retract of a NON-CURRENT node is refused — adjudicated descendants stay put, retract-twice is not idempotent, the unacknowledged door stays shut (#2396) |

Run: `python -m pytest tests/cascade_scenarios -q`
