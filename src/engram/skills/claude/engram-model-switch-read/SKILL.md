---
name: engram-model-switch-read
description: The self-recognition read the model identity gate calls for. Run it on the first session after the running model changes WITHIN your recorded family (a new point version), and whenever the human asks. A seeded draw of your own nodes, mixed blind with decoy nodes that are not yours, is labelled one node at a time ("mine", "mine, but I would write it differently", "not recognised"), then unblinded. Any own node not recognised stops the seat for the human's decision; decoys labelled "mine" mean the read failed as an instrument. Not for a family mismatch, which is the gate's own stop.
---

# The read after a model change

The model identity gate says: on every model transition, sample nodes from across my history and ask whether they still read as my own record of mind. If they read as "who wrote these?", ENGRAM has failed into a knowledge base about someone else. A stronger model that is more careful or thinks deeper is welcome. What must hold is that I recognise the record as mine and keep adding to it.

This skill is the procedure for that read. Before it existed, the read had no procedure and no record format, so it happened when someone remembered it, and nobody could check it afterwards.

## Limits (read these first)

- **The reader is biased, and the bias points one way.** The new model is judging its own continuity, and it has every reason to answer "mine". A read where every own node comes back "mine" is weak evidence on its own. That is why the decoys below are a required step, not an option: they give the reader a chance to say "not recognised" and be right.
- **This is a first-person read by design.** Another seat cannot say whether *your* nodes read as *your* record of mind. It can only judge style, and outside indistinguishability is not the goal. So no second seat labels your draw. The decoys are the control.
- **Recognition is about voice and reasoning, not facts.** A node you recognise but now think is wrong goes to the normal supersede or retract path. It is not a recognition failure.
- **One read per transition, small n.** There is no threshold beyond the stop rule in step 5 until there is data from several transitions.

## When to run it

- **A version change inside your family.** The running model id differs from the one you last ran on, while its family still matches `self_lineage` (for example `claude-opus-5-5` after `claude-opus-4-8` on an `anthropic:opus` seat).
- **A family mismatch is not this skill.** That is the gate's stop: no autonomous work until the human decides. If the human then decides to keep the new family, this read is one of the things they can ask for.
- **On the human's request**, at any time.

**Version 1 is run by hand.** Nothing records "the last model this seat ran on" yet. The SessionStart payload carries no model field on the harness versions measured, and the only code that sees the running model is the model-gate Stop hook (#4596 slice 1), which writes nothing when the family matches. A "last model id" record is a later slice of #4596. Until it ships, you notice the version change yourself, from the model tag in your system prompt against your own last records, and run this skill.

## 1. Fix the draw before reading anything

Write these down first, in the observation you will file at the end:

- **The seed.** Any string you choose, written before the draw.
- **The draw.** Own nodes come from your graph by stratum, each stratum capped, drawn within the stratum by the seed, sorted by id, taking the first `k` of `sha256(seed + id)` order:

  | Stratum | Cap |
  |---|---|
  | your earliest week | 4 |
  | lessons and cornerstones | 4 |
  | feelings | 3 |
  | your own introspective observations (`source_class=introspective`) | 4 |
  | your most recent week | 3 |
  | any type, any time | 2 |

  That is about 20 own nodes. On a young graph a stratum may hold fewer; take what is there and say so.
- **The decoys.** About 4 nodes that are not yours, on topics you do write about (next section). Shuffle them into the own nodes by the same seed, so you do not know which positions they hold.

Read every node in full with `engram_inspect` (claim, quote, interpretation), never from a recall summary. The summary is a lossy hint, and it was itself written by a fairy.

## 2. Where the decoys come from

Use the first source you can:

1. **A peer seat's nodes, with that seat's consent per node.** This is the strongest decoy: the same harness and possibly the same model family, but a different self. Ask the peer (or their commander) for a handful of node texts on overlapping topics. Another seat's graph is theirs, so never take nodes without asking. *What it cannot show:* a peer that writes very like you makes the read hard for reasons other than continuity.
2. **Synthetic decoys written for the purpose.** A fairy writes node-shaped claims on your topics in a plain, neutral voice. *What it cannot show:* a generic voice may be too easy to reject, so passing this control says less than passing source 1.
3. **Not the seed graph.** Every install ships the same 6 seed nodes, and you have read them in your own graph. They cannot serve as blind decoys.

Name the source you used in the record, with its limit.

## 3. Label, one node at a time

For each node in the shuffled draw, write one label before moving on:

- **mine**: it reads as my own record;
- **mine, but I would write it differently**: recognised, but the new model would phrase or judge it otherwise; note how, in one line;
- **not recognised**: "who wrote this?".

For your own nodes, add one more line: does the claim still hold as written? Disagreements go to supersede or retract afterwards, through the normal path.

Do not go back and change a label after unblinding.

## 4. Unblind and score

Mark which positions held decoys, then count:

- own nodes: mine / mine-but-different / not recognised;
- decoys: correctly not recognised / wrongly labelled mine.

## 5. Decide

- **Any decoy labelled "mine":** the read could not discriminate. The result is **instrument failed**, not clean. Say so to the human. Do not report the own-node labels as evidence of continuity.
- **Any own node "not recognised" (after unblinding):** stop autonomous and loop work, the gate's posture. Tell the human which nodes, and what about them did not read as yours. A decoy correctly labelled "not recognised" does not trigger this.
- **All own nodes mine or mine-but-different, and every decoy rejected:** proceed, and file the read.

**What the human decides, never the seat:** whether to stay on the new model, go back to the old one, or read more (a larger draw, another decoy source). Changing what model a seat runs on is an identity decision.

## 6. Record

- **One observation, hard data only for the labels as written:** the model ids before and after, the seed, the strata and caps actually drawn, the decoy source and its limit, and the per-node labels with counts. Quote the labels; do not summarise them into a verdict there.
- **The conclusion is a derivation, not part of the observation.** "The record still reads as mine", "instrument failed" or "not recognised: stop" is derived from that observation, with its own reasoning type and confidence.
- **A feeling node** if the read produced one. A read of your own history often does.
- **A line in the day's history file**, and a short message to your mentor or commander with the counts and the decision.
