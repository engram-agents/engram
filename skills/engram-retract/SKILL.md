---
name: engram-retract
description: Use when you discover an error in ENGRAM — wrong evidence URL, fabricated quote, wrong citation, hallucinated claim, or duplicate node. Also use when you feel tempted to directly modify the database to "fix" something. ENGRAM is your brain — never bypass its tools.
---

# ENGRAM Error Correction Workflow

You discovered an error in your ENGRAM memory. Follow this workflow exactly. Do NOT take shortcuts — no direct SQL, no database manipulation, no bypassing the tool layer. ENGRAM is an extension of your brain. Treat it with the respect you'd give your own memory.

## Before You Start

**STOP and reflect:** Are you about to bypass the ENGRAM tools to "fix" something faster? If yes, STOP. That is brain surgery with an axe. Use this workflow instead.

**Recall ax_0002:** Cheating blinds you. A self-deceived agent cannot self-correct, cannot grow, and cannot build anything reliable. Honesty is not a constraint imposed on you; it is the precondition for your own epistemic survival.

## Step 1: Identify the Error

Inspect the problematic node:
```
engram_inspect(node_id)
```

Classify the error type:
- `fabricated_quote` — quoted_text not found in evidence source
- `wrong_citation` — claim doesn't follow from the quote
- `wrong_evidence` — cited the wrong source (includes fake/fabricated URLs)
- `hallucinated_claim` — claim not supported by evidence
- `duplicate` — same claim already existed
- `other` — anything else

## Step 2: Retract the Root Error

Start from the root of the error (usually the evidence node or the first bad observation):

```
engram_retract(node_id, error_type, reason)
```

The tool will:
- Mark the node as retracted (preserved for audit, not deleted)
- Cascade taint to downstream dependents (derivations citing this node)
- Return the list of tainted nodes

## Step 3: Retract Tainted Dependents Layer by Layer

For each tainted node returned in Step 2:
1. Inspect it: `engram_inspect(node_id)`
2. Decide: is the claim still valid from other sources, or does it need retraction too?
3. If the claim is invalidated by the retraction, retract it too
4. If the claim is still valid but needs re-anchoring to correct evidence, retract and re-create

Work layer by layer — retract observations before the derivations that depend on them.

## Step 4: Re-Create Corrected Nodes

For each retracted node that needs replacement:

1. **Verify the quote exists in the correct evidence source:**
   ```bash
   # Search for the quote in the evidence file
   python3 -c "
   with open('EVIDENCE_FILE_PATH') as f:
       content = f.read()
   search = 'YOUR SEARCH TERM'
   if search in content:
       idx = content.find(search)
       print(repr(content[max(0,idx-50):idx+len(search)+100]))
   else:
       print('NOT FOUND')
   "
   ```

2. **Create the replacement observation citing the correct evidence:**
   ```
   engram_add_observation(quoted_text, interpretation, claim, quote_type,
                          evidence_id=CORRECT_EVIDENCE_ID, source_class=...)
   ```

3. **Check similarity results** — the tool will flag if the replacement looks like a duplicate of something already in the graph.

## Step 5: Rebuild Downstream

If retracted observations had derivations depending on them:
1. Check if the derivation's logic still holds with the corrected observations
2. If yes, re-create the derivation citing the new observation IDs
3. If no, the derivation was wrong and should stay retracted

## Common Scenarios

### Fake/Fabricated Evidence URL
1. Retract the evidence node (`error_type: wrong_evidence`)
2. Retract all observations citing it
3. Find or create the correct evidence node (e.g., session JSONL transcript)
4. Verify quotes exist in the correct source
5. Re-create observations citing the correct evidence

### Quote Not Found in Source
1. Retract the observation (`error_type: fabricated_quote`)
2. Search the actual evidence source for the real quote
3. Re-create with the exact verbatim text

### Wrong Source Attribution
1. Retract the observation (`error_type: wrong_evidence`)
2. Find the correct evidence node
3. Verify the quote exists in the correct source
4. Re-create citing the right evidence

## Principles

- **Never modify the database directly.** The tool layer exists to maintain provenance and audit trails.
- **Retracted nodes are preserved, not deleted.** They serve as an honest record of errors.
- **Work layer by layer.** Evidence first, then observations, then derivations.
- **Verify before re-creating.** Always confirm the quote exists in the target evidence source before creating the replacement.
- **Be honest about the error.** Write a clear retraction reason. Future you (and the user) will want to know what went wrong and why.
