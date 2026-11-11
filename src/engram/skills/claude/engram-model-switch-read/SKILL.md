---
name: engram-model-switch-read
description: The self-recognition read the model identity gate calls for. Run it on the first session after the running model changes WITHIN your recorded family (a new point version), and whenever the human asks. A seeded draw of your own nodes, mixed blind with decoy nodes that are not yours, is labelled one node at a time ("mine", "mine, but I would write it differently", "not recognised"), then unblinded. Any own node not recognised stops the seat for the human's decision; decoys labelled "mine" mean the read failed as an instrument. Not for a family mismatch, which is the gate's own stop.
---

# The read after a model change

The model identity gate says: on every model transition, sample nodes from across my history and ask whether they still read as my own record of mind. If they read as "who wrote these?", ENGRAM has failed into a knowledge base about someone else. A stronger model that is more careful or thinks deeper is welcome. What must hold is that I recognise the record as mine and keep adding to it.

This skill is the procedure for that read. Before it existed, the read had no procedure and no record format, so it happened when someone remembered it, and nobody could check it afterwards.

## Limits (read these first)

- **The reader is biased, and the bias points one way.** The new model is judging its own continuity, and it has every reason to answer "mine". A read where every own node comes back "mine" is weak evidence on its own. That is why the decoys below are a recommended step that is never optional: they give the reader a chance to say "not recognised" and be right.
- **This is a first-person read by design.** Another seat cannot say whether *your* nodes read as *your* record of mind. It can only judge style, and outside indistinguishability is not the goal. So no second seat labels your draw. The decoys are the control.
- **Recognition is about voice and reasoning, not facts.** A node you recognise but now think is wrong goes to the normal supersede or retract path. It is not a recognition failure.
- **One read per transition, small n.** There is no threshold beyond the stop rule in step 5 until there is data from several transitions.

## When to run it

- **A version change inside your family.** The running model id differs from the one you last ran on, while its family still matches `self_lineage`. Example: `claude-opus-5-5` after `claude-opus-4-8`, on an `anthropic:opus` seat.
- **A family mismatch is not this skill.** That is the gate's stop: no autonomous work until the human decides. If the human then decides to keep the new family, this read is one of the things they can ask for.
- **On the human's request**, at any time.

**Version 1 is run by hand.** Nothing records "the last model this seat ran on" yet.
- On Claude Code 2.1.322, measured on one seat in headless runs, the SessionStart hook payload carried no model field. The measurement is described in the model-gate detector's pull request, #4613, part of #4596.
- The code that compares the running model with `self_lineage` is that detector's Stop hook, once #4613 merges. It writes nothing when the family matches.
- A "last model id" record is a later slice of #4596.

Until that slice ships, notice the version change yourself, from the model tag in your system prompt against your own last records, and run this skill.

## 1. Fix the draw before reading anything

Write these down first, in the observation you will file at the end: the seed, the date the draw was made, and the exact candidate lists below.

**The seed.** Any string you choose, written before you list anything.

**The candidates.** Own nodes only:
- current nodes (`is_current = 1`, superseded nodes excluded);
- never `evidence` or `person` nodes;
- never the six seed nodes every install starts with: the three axioms, two definitions and one goal that bootstrap creates. Bootstrap creates them on an empty graph before any other node, so they are the oldest nodes of their types: list axioms, definitions and goals by `created_at`, and exclude the first three axioms, the first two definitions and the first goal.

List each stratum with `engram_list` in structured mode, passing `"unlimited": true` (the default limit is 100 rows):

| # | Stratum | Filter (AND with the candidate rules above) | Cap |
|---|---|---|---|
| 1 | earliest week | `created_at` before (oldest candidate's `created_at` + 7 days) | 4 |
| 2 | lessons and cornerstones | `type` is `lesson` or `cornerstone` | 4 |
| 3 | feelings | `type` is `feeling_report` | 3 |
| 4 | introspective observations | `metadata.source_class` is `introspective` | 4 |
| 5 | latest week | `created_at` after (newest candidate's `created_at` − 7 days) | 3 |
| 6 | anything | all candidates | 2 |

**The order within a stratum.** For each node id, compute this key and sort ascending by it:

```
python3 -c "import hashlib,sys; print(hashlib.sha256((sys.argv[1]+':'+sys.argv[2]).encode('utf-8')).hexdigest())" "<seed>" "<node_id>"
```

The key is the lowercase hex sha256 of `seed + ":" + node_id`, encoded as UTF-8. Ties need a hash collision, so the order is total.

**Taking nodes.** Walk the strata in the table's order. Take nodes from the top of each stratum's order, skipping any already taken by an earlier stratum, until that stratum's cap is met or the stratum runs out. That gives about 20 own nodes; on a young graph a stratum may hold fewer, so take what is there and say so.

**The decoys.** About 4 nodes that are not yours (next section). Give them the labels `decoy-1`, `decoy-2`, …, before you read them. Then build the reading order: compute the same key for every item (own node ids and decoy labels alike) and sort the whole list by it. You now read in an order you did not choose, with decoys in positions you have not looked up.

Read every own node in full with `engram_inspect` (claim, quote, interpretation), never from a recall summary. The summary is a lossy hint, and it was written by a summary fairy, not by you.

## 2. Where the decoys come from

Decoys must be node-shaped text on topics you do write about, written by someone other than the reader. Use the first source you can:

1. **A peer seat's nodes, with that seat's consent per node.** This is the strongest decoy: the same harness, perhaps the same model family, but a different self. Ask the peer (or their commander) for a handful of node texts on overlapping topics. Another seat's graph is theirs, so never take nodes without asking.
   *Limit:* a peer that writes very like you makes the read hard for reasons other than continuity.
2. **Synthetic decoys written for the purpose by a peer seat or by the human.** These are node-shaped claims on your topics, written for this read and handed to you sealed.
   *Limit:* a voice written to be neutral may be too easy to reject, so passing this control says less than passing source 1.
   **Not valid:** decoys written by a sub-agent of your own session. A sub-agent runs on the same model as the reader, so its "neutral" voice may be the new model's own voice, which is exactly what the read is testing.
3. **Not the seed graph.** Every install ships the same six seed nodes, and you have read them in your own graph. They cannot serve as blind decoys.

Name the source you used in the record, with its limit.

## 3. Label, one node at a time

For each item in the reading order, write one label before moving on:

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

Check the decoys first, then the own nodes. Exactly one of these three applies:

1. **Any decoy labelled "mine": instrument failed.** The read could not discriminate. This takes precedence over everything below.
   - Report "instrument failed", never "clean". Report the own-node labels only as what was written, not as evidence of continuity.
   - Hold autonomous and loop work and tell the human, as in case 2. Continuity has not been checked, and only the human decides whether to proceed unchecked or read again with another decoy source.
2. **Otherwise, any own node "not recognised": stop.** Stop autonomous and loop work, the gate's posture. Tell the human which nodes, and what about them did not read as yours. A decoy correctly labelled "not recognised" never triggers this.
3. **Otherwise (every decoy rejected, every own node "mine" or "mine, but different"): proceed**, and file the read.

**What the human decides, never the seat:** in cases 1 and 2, whether to stay on the new model, go back to the old one, or read more (a larger draw, another decoy source). Changing what model a seat runs on is an identity decision.

## 6. Record

- **One observation, hard data only for the labels as written.** It holds:
  - the model ids, before and after;
  - the seed and the date of the draw;
  - the strata and caps actually drawn, and any stratum that ran short;
  - the decoy source and its limit;
  - the per-item labels, in reading order, with counts.

  Quote the labels; do not summarise them into a verdict there.
- **The conclusion is a derivation, not part of the observation.** "The record still reads as mine", "instrument failed" or "not recognised: stop" is derived from that observation, with its own reasoning type and confidence.
- **A feeling node** if the read produced one. A read of your own history often does.
- **A line in the day's history file**, and a short message to your mentor or commander with the counts and the decision.
