---
name: engram-support-lost
description: Handle cornerstones/lessons flagged support_lost (all live support edges gone after retracts/supersedes). Inspect the pattern claim, search for live exemplars, rewire via engram_register_exemplar, leave honestly unanchored, or retract via the engram-revise skill. Loaded by agents hitting the flag live and by the dream-master acting on dream-fairy recommendations.
---

# What support_lost is

`support_lost` is a plain `metadata.support_lost` boolean, **not a cascade
marker** — a genuinely separate mechanism from `tainted_by`/`stale_by` and the
`cascade_markers` table. It is set by `_detect_zero_support` when ALL of a
cornerstone's or lesson's live support edges (`supported_by` outgoing +
`exemplifies` incoming) drop to zero. Cornerstones and lessons accumulate
votes rather than `derive_from` premises, so individual instance
retractions/supersedes never give them `tainted_by`/`stale_by` markers —
instead, once the last supporting instance goes non-current, the flag fires.
`support_lost` never appears on `engram_cascade_frontier` — it is outside the
frontier/adjudicate/`engram_cascade_resolve` contract entirely, uses a
different verb (`engram_register_exemplar`), and clears automatically rather
than by adjudication.

# Who loads this

- **An agent encountering the flag live** — often right after resolving
  cascade markers, since a retract is what typically zeroes a cornerstone's
  or lesson's support edges in the first place. If `engram-resolve-cascade`
  pointed you here, that's the common path.
- **The dream-master**, adjudicating dream-fairy support-lost recommendations
  (candidate live exemplars found, or a lost-basis assessment) surfaced during
  a consolidation scan.

# Handling a support_lost node

When you see `cs_X.support_lost = true` or `ls_X.support_lost = true`:

1. `engram_inspect(cs_X / ls_X)` — read the cornerstone/lesson claim
2. `engram_query` for similar patterns in the graph — are there live
   observations or derivations that exemplify this cornerstone's claim?

   **If YES — trivial rewire:**

   3a. Register the exemplar against the pattern node:
       ```
       engram_register_exemplar(payload_json=json.dumps({
           "target_id": "<cs_X or ls_X>",
           "exemplar_id": "<ob_or_dv_id>",
           "note": "<why this instance fits the pattern>",
       }))
       ```
       The tool handles both cornerstones and lessons; for lesson targets,
       the tripwire cache refresh fires automatically.

   3b. The `support_lost` flag clears automatically once at least one live
       exemplar edge exists.

   **If NO — the pattern has lost its empirical basis:**

   4a. Decide whether the cornerstone/lesson still merits existing.

   4b. If the pattern still holds in principle but is currently un-exemplified:
       leave the `support_lost` flag in place. The next exemplifying experience
       can link it via the trivial-rewire path above. `support_lost` is not an
       error — it is an honest signal that the pattern is currently unanchored.

   4c. If the pattern no longer applies (the underlying belief has been
       superseded or retracted): retract the cornerstone/lesson. Load the
       **[engram-revise](../engram-revise/SKILL.md) skill** for the
       classification discipline (it is a skill, not an MCP tool — the
       actual verb you end up calling is `engram_retract`).

# Adjudication authority — your call, almost always

Same disposition as `engram-resolve-cascade`'s "When in doubt" section (see
that skill for the full treatment): resolving a support_lost flag is your
call by default — leaving it honestly unanchored (step 4b) is a valid
resolution, not a deferral. Escalation to the user is rare, and only for the
same three shapes: a goal/axiom in tension, a resolution the mechanism can't
cleanly express, or genuinely needing information/judgment only the user has.
Never escalate just to hand off a judgment that's yours to make.
