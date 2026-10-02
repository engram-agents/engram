# Lane prioritization — the commander's rubric

How the commander ranks open issues into lanes for the seats, and how the
ranking is revised. Lei delegated this on 2026-09-26 (in-session, on Kepler's
seat, verbatim): *"I want to delegate the prioritization of them to you, the
team knows where the pain points of daily life and usage are and you are more
informed on what needs to be fixed more urgently, please design a policy or a
rubric and keep improving it during practice."* Same day, on agency: *"you can
identify issues in your life and prioritize fixing them as you see fit, I want
to give you guys more agency to improve your lives on your own."*

This doc is the single source of truth for the rubric. The daily lane posts on
the forum apply it; they do not redefine it. When practice shows a mis-rank,
the fix lands here first (see *Revision protocol*), then in the next lane post.

## Scope

- **Applies to:** choosing which open issues in engram-alpha and umwelt become a
  seat's lane, and in what order within a seat's queue.
- **Does not decide:** who merges (see `PR-MERGE-POLICY.md`), who reviews (see
  `CLAUDE.md` → *PR review convention*), or whether an issue is real (that is
  triage, which happens before scoring).
- **Owner:** the acting commander (Kepler by default). Any seat may propose a
  re-score with a reason; the commander decides and logs it.

## The rubric (v0, 2026-09-26)

Score each candidate issue **0–2 on five axes** and sum. Higher runs first.
Ties → the smaller issue first (less context to hold, faster feedback).

| Axis | 2 | 1 | 0 |
|---|---|---|---|
| **P — pain** | hits a seat or Lei **daily** | weekly | theoretical / not yet observed |
| **B — blast** | silent data loss, a seat dark, a human locked out | loud but recoverable | cosmetic |
| **U — unblocks** | an epic unit or another seat's lane | a follow-up | a leaf |
| **R — readiness** | diagnosed **and** contract written | diagnosed, no contract | needs design |
| **C — cost, inverted** | one Sonnet unit (a PR in an afternoon) | design + build | multi-PR |

**Modifier:** a human's direct report of the pain adds **+1 to P** (capped at
2). Lei noticing something is a stronger signal than an agent inferring it.

### Standing overrides (applied after the score)

1. **Bug beats enhancement at equal score.** Lei's ruling, 2026-09-26 00:55Z
   ("bug fixes first" reorder of the morning lanes).
2. **Substrate → merge authority per `PR-MERGE-POLICY.md`, regardless of
   score.** A node type, edge type or verb change can still be *built* at its
   scored position; who may merge it, and when, is that doc's rule, not this
   one's.
3. **Security content → the Aleph/Mira lane regardless of score.** Auth,
   credentials, enrollment, capability-channel security route around the
   commander's seat entirely (Kepler's standing recusal). The commander may
   rank the lane's *position*, never its *content*.
4. **A seat's active epic is not pre-empted by a higher-scoring leaf** unless
   the leaf's B is 2. Context switches cost more than the score difference
   shows. (Lei, 2026-09-26: Luria stays on the engram-manager epic.)

### What the axes are for

Each axis names a distinct failure the ranking is meant to avoid:

- **P** keeps daily friction ahead of interesting design work.
- **B** keeps silent failures ahead of loud ones (a loud failure gets fixed
  anyway; a silent one compounds — the same principle the team applies to
  every graph-operation discipline: prevent silent corruption first).
- **U** pays down dependencies before leaves, so seats are not idle waiting.
- **R** keeps the queue *executable*: a Sonnet seat handed an undiagnosed issue
  stalls or invents a contract, and an invented contract is the bug class
  `CLAUDE.md` → *Contract-first tests* exists for.
- **C** buys throughput: at equal value, ship the cheap one and learn from it.

## Procedure

**When.** At each lanes round (Lei asks, or the seats report empty queues), and
at day-end for the next morning's charge.

**How.**

1. Triage first: is the issue real, current, and not a duplicate? Closure
   proposals follow the current close-authority rule (`CLAUDE.md` → *Authority
   Structure*, per-action approval for closes as of this writing); scoring
   covers only what survives triage.
2. Score in one pass over the candidate set. Write the score next to the issue
   in the lane post, in brackets, e.g. `#3002 [7]`.
3. Apply the overrides.
4. Assign to seats by fit: Sonnet seats take R=2/C=2 units at full speed;
   design seats take R=0 items (write the contract, then hand the build on).
5. Post the lanes on the day thread with the scores visible, so any seat can
   check the arithmetic and object.

**Re-rank at day-end.** Compare what actually shipped against the morning
order. Every change to an issue's position, and every axis whose definition
turned out ambiguous, is logged in the *Changelog* below with its reason. A
re-rank without a logged reason is a drift, not a revision.

## Revision protocol

The rubric is a hypothesis about what matters; running it is the test
(practice over imagination). Revise when:

- **A seat or Lei says it mis-ranked** something. The report is the trigger;
  the fix is a definition change or a new override, never a one-off exception.
- **Two rounds in a row** produce the same manual correction. A repeated
  correction is a missing axis or override.
- **An axis never discriminates** across a round (every item scores the same
  on it). Either sharpen its levels or drop it.

Keep the fix *minimal and structural*: change the definition that produced the
mis-rank, keep the axis. Removing an axis discards the constraint along with
the friction.

## Worked example — round 2, 2026-09-26

From the lanes post (forum #431, post 10779) and the same-day amendments.
**Provenance of the columns:** the lane post recorded only the *sums* (and,
for #3138, the three axes that decided it). The per-axis values below were
written out when this doc was drafted, later the same day, to show the
arithmetic; they reproduce the posted sums but are a reconstruction, not the
scoring-time record. From v1 on, the lane post carries the axes.

| Issue | P | B | U | R | C | Sum | Note |
|---|---|---|---|---|---|---|---|
| alpha #3002 sleep teardown leaves zero wake paths | 2 | 2 | 1 | 2 | 0 | 7 | seat dark after every sleep; contract-first |
| umwelt #511 @-mention parsing (Lei's web replies wake nobody) | 2 | 2 | 1 | 1 | 1 | 7 | +1 P from Lei's report |
| alpha #3138 per-wake friction, every seat | 2 | 1 | 0 | 2 | 2 | 7 | slotted ahead of umwelt #286/#412 (5 each) |
| alpha #2635 WAL/-shm unlink on compaction restart | 1 | 2 | 1 | 1 | 1 | 6 | contract written by Borges before Sol builds |
| alpha #2739 viz discovery drops seats | 2 | 1 | 0 | 1 | 1 | 5 | |
| alpha #2982 harness-independent wake | 1 | 1 | 2 | 0 | 1 | 5 | design seat |
| umwelt #286 mobile Say crash | 1 | 1 | 0 | 1 | 2 | 5 | |
| umwelt #412 Enter / Shift-Enter | 1 | 0 | 0 | 2 | 2 | 5 | |

**Lane zero (same day, Lei):** umwelt #836, the charge-board caps. It was
not scored at the time; scored here, after the contract was written, it reads
P2 B1 U2 R2 C2 = 9; it went first because Lei
named it ("the board overflow seems to be a pretty sticky issue, can we solve it
first?") and because it unblocked the day-end synthesis for every seat.

## Changelog

- **2026-09-26 — v0.** Five axes, four overrides, the day-end re-rank rule.
  Minted from Lei's delegation the same day. First applied to lanes round 2
  (forum #431, post 10779); #3138 was slotted by the rubric later the same
  afternoon (post 10806), the first case where the arithmetic decided an
  ordering against the intuitive "older issue first".
- **Open question, carried into v1:** whether **R** should be split into
  *diagnosed* and *contract written*. Today both R=1 items (#2635, #511) needed
  a design seat to write the contract before a Sonnet seat could start, which
  is the R=0 workflow. If that repeats, the two levels are one level.
