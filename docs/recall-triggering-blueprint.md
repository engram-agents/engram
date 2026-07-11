# Recall-Triggering System — Review + Improvement Blueprint

**Author:** Kepler (with Sol as implementation pair) · **Date:** 2026-07-07
**Directive:** Lei, 2026-07-07 — make recall "more ambient and more accurately
firing right at the time you need it" (dev-graph anchor: the 07-07 directive
observation). Four named dissatisfactions:

1. Auto-recall fires only on user prompts, never while the agent thinks/works.
2. Cornerstones are hand-anchored into warm-briefing instead of
   context-triggered like the lesson system.
3. Recall is repetitive — the same topical node re-surfaces every turn of a
   multi-round conversation; recently-recalled nodes should yield to relevant
   nodes NOT recalled in recent turns.
4. The standing tension: more recall (more injected context) vs. saving context.

This blueprint maps the current pipeline (§1), grounds each complaint in code
(§2), proposes four work-streams (§3), and defines acceptance metrics (§4).
Same living-doc pattern as the perf blueprint (#1667).

---

## §1 Current architecture map

### Trigger points (hooks.json)

| Event | Recall behavior today |
|---|---|
| `UserPromptSubmit` | **The only general recall trigger.** `engram-surface-hook.py` → recall-daemon (Unix socket, semantic+FTS) → `_search_nodes` 3-tier ranking → rendered nudge (specials ≤3, top claims ≤3, Others ≤15 keyword-only). Also: incident tripwire (surfaced IDs ∩ `error_incidents.json`), error-pattern keyword fallback, short-prompt prev-response-tail prepend (IDF-gated) for the *embedding* query only. |
| `PreToolUse` (Bash + `mcp__.*`) | `engram-lesson-tripwire-hook.py` — regex `situation_pattern` match against the pending command / tool-call → injects `scaffolding_nudge`. **Lessons only**; no general recall; not wired for Read/Edit/Write/Grep/WebFetch. |
| `Stop` | No recall. Write-check flag + `engram-utility-credit-mention-stop.py` (bumps `utility_score` of node IDs mentioned in the agent's turn — positive feedback only). |
| `SessionStart` / `PostCompact` | No recall (counter reset, briefing pointer). |

### Ranking (engram_query.py `_search_nodes`)

- Tier 1 raw pool (FTS ∪ semantic, `FTS_SIM_FLOOR=0.30`), Tier 2 composite
  sort, Tier 3 MMR (`MMR_LAMBDA=0.9`), specials bypass all of it.
- **Composite = relevance × (1 + 0.10·utility) × (1 + 0.005·imp_norm).**
- Deliberate recall (`engram_query`/`engram_inspect`) **refreshes**
  `importance_score` + `recall_turn`; `engram_surface` is side-effect-free.
- The surface hook emits `engram.surface.fire` events with `matched_ids[:10]`
  per prompt (structured event log) — the only record of what auto-surfaced.

## §2 Grounding the complaints

**(1) No in-turn recall — TRUE, with one existing beachhead.** General recall
fires only at `UserPromptSubmit`. But the lesson tripwire already proves the
PreToolUse injection channel works (`additionalContext` on
`permissionDecision: allow`) — the plumbing for in-turn injection exists; what's
missing is general recall through it, and coverage of more tool types. The
prev-response-tail prepend also already lets *agent* output influence the next
prompt-time query — but only at the next user prompt, not mid-turn.

**(2) Cornerstones have no trigger channel — TRUE, verified.** #931 verified:
zero cornerstone-surfacing logic in session-start or surface hooks; only
hand-maintained warm-briefing anchors (which drifted: 3 entries vs 13
cornerstones). `engram_register_exemplar` accepts cornerstone targets with the
cache slot explicitly reserved (`engram_epistemic.py`: "For cornerstone
targets, no cache today — future cornerstone-tripwire"). The incident-index
architecture (concrete incidents → `exemplifies` edge → abstract principle) is
proven for lessons and pre-stubbed for cornerstones.

**(3) Repetition — TRUE, and structurally *worse* than neutral.** There is no
recall-recency penalty anywhere in the pipeline. Worse: recall refreshes
importance, and importance amplifies composite score — so a node I inspected
because it surfaced becomes *more* likely to surface again (a
rich-get-richer loop the MMR diversity pass does not counteract, because MMR
diversifies within one result set, not across turns). The
`not_recalled_recently` signal is computed but rendered only as a count
("Memory: N nodes not recalled recently"). The surfaced-ledger substrate for
suppression already exists (`engram.surface.fire` events).

**(4) Budget — no governance.** Rendering caps are per-section constants
(3/3/15); there is no global char budget, no marginal-value allocation, no
telemetry on rendered size.

## §3 Proposed work-streams

### P1 — Surfaced-recency suppression ("habituation") — complaint (3)

Maintain a per-session **surfaced ledger**: `~/.engram/surface-ledger.json`
keyed by `session_id`, appending `(prompt_seq, matched_ids_rendered)` per fire
(the hook already has both; the ledger is the event-log signal made cheap to
read). At render time, demote nodes surfaced within the last `k` prompts
(default k=5), with graceful tiers rather than a hard drop:

- 1st surfacing: full render (summary line as today).
- Surfaced again within k: drop to Others (ID + keywords only).
- Surfaced ≥m times within k (default m=3): suppress entirely; free the slot
  for the next-ranked NOT-recently-surfaced node.
- Print suppressed count: `(+N recently shown — engram_surface for full list)`
  so the agent knows suppression is active (honesty about lossiness).

Suppression is at the **render layer, not the match layer** — matching stays
pure; a deliberate `engram_query` is never suppressed (deliberate recall is the
agent explicitly asking; habituation applies to ambient noise only). Specials
follow the same rule (an axiom that surfaced 3 prompts ago doesn't need
re-injection). Tripwire nudges are exempt (safety-critical, separate channel
with its own cooldown — see P3).

Second lever, same stream: **invert the not-recalled signal.** Reserve 1–2 of
the freed render slots for the top-ranked matches from the
`not_recalled_recently` set — "relevant and you haven't seen it lately" is
exactly Lei's ask, and the signal is already computed.

### P2 — In-turn ambient recall — complaint (1)

Generalize the PreToolUse beachhead from lessons-only to budgeted general
recall, with a strict novelty gate so the hot path stays cold:

- **Trigger:** PreToolUse on Bash + MCP (existing matchers) + Read/Write/Edit
  (new matcher; the file-path argument is a high-signal cue).
- **Novelty gate (all must pass, else exit 0 silently):**
  (a) extract high-IDF terms from the tool args (reuse `engram_idf`);
  (b) ≥1 term not present in any query that fired within this turn or the
      last surfaced-ledger window (topic-shift detection);
  (c) cooldown: ≥N tool calls (default 5) or ≥60s since the last in-turn fire.
- **Query + render:** daemon query with the novel terms; render **top-1 to
  top-3 lines maximum, keywords+summary**, only nodes not in the surfaced
  ledger. If nothing clears the bar, inject nothing — silence is the default.
- **Latency budget:** the gate must run <30ms (IDF lookup on read-only conn);
  the daemon query (~50–100ms warm) happens only on gate-pass, which should be
  rare (topic shifts, not every call). Measured against the perf-day baseline
  (~315ms/prompt hook-spawn overhead is the known floor; adding a PreToolUse
  matcher for Read/Edit multiplies spawn count — measure first, ship behind a
  config flag `in_turn_recall.enabled`, default on only if the p95 impact is
  acceptable per replay-bench).

This makes recall fire "right at the time you need it": when the agent starts
*acting* on a topic the user never named — reading a file, calling a tool with
novel arguments — which is exactly when prompt-time recall has gone stale.

### P3 — Cornerstone tripwire + unified principle triggers — complaint (2)

Implement the pre-stubbed cornerstone path as a sibling of the lesson system,
then unify:

1. `engram_register_exemplar` cornerstone targets populate a
   `cornerstone_incidents.json` cache (same shape as `error_incidents.json`).
2. Surface hook checks surfaced IDs against BOTH indexes; a cornerstone hit
   injects its behavioral one-liner (the same line format the warm-briefing
   anchor section uses) tagged `[cornerstone-anchor (cs_XXXX)]`.
3. Lessons with `situation_pattern` already fire at PreToolUse; allow
   cornerstones the same optional field (e.g. cs "check the graph before
   deriving" fires on `engram_derive` calls — pattern `engram_derive`).
4. Unify the caches into `principle_triggers.json` with a `kind` field
   (lesson=corrective, cornerstone=orienting, axiom=constraining,
   goal=directional) — one loader, one cooldown mechanism, one habituation
   policy (a tripwire that fires on no-ops trains the agent to dismiss it).
5. Warm-briefing anchors remain the always-on layer for the ~5 top
   cornerstones (the "letter IS the surfacing mechanism" design from #61
   still holds for the head); the tripwire covers the tail that #931 proved
   has no channel. Add the #931 cross-surface coverage check to
   `engram_diagnose` so "no delivery channel" is mechanically detectable.

### P4 — Injection budget governance — complaint (4)

- One global per-prompt injection budget for the recall nudge (chars,
  default ≈ current typical render), spent by marginal value: novelty
  (P1 ledger) × relevance × type-weight. Sections stop being fixed 3/3/15
  caps and become allocations.
- Emit `engram.surface.render_size` (chars rendered, nodes shown/suppressed)
  so the budget's effect is measurable, and Sol's repetition metric becomes a
  standing telemetry series rather than a one-off study.
- In-turn recall (P2) draws from the same budget concept with a much smaller
  per-fire cap; total ambient injection per turn is bounded.

## §4 Acceptance metrics

Baseline: Sol's repetition study (event-log mining, 2026-07-07 — 21 sessions,
769 surface-fire prompts, 4,817 surfaced-id instances over 2.5 weeks):
**median per-session repeat fraction 68.0% at k=5** (pooled 77.4%, inflated by
one 162-prompt loop session); same-node max 129×/session, p95 = 22×; wasted
context ≈ 17,700 chars/session at ~100 chars/id blended. Worst offenders are
cornerstone/lesson-adjacent nodes — evidence that some repetition is *correct
persistent relevance*, which P3 resolves structurally: cornerstones move to
their own dedicated anchor/trigger channel and exit the general recall stream,
so P1's suppression never has to distinguish "recurringly relevant principle"
from "already-seen observation" inside one channel.

1. **Repeat fraction** (share of rendered node-lines already rendered within
   the last k=5 prompts): target ≤34% median per-session (half of the 68%
   baseline; stretch <15%).
2. **Novel-relevant surfacing** unchanged or up: fraction of prompts where ≥1
   node the agent then deliberately inspects appears (proxy for usefulness) —
   must not regress.
3. **Context cost**: mean rendered chars/prompt at or below current, despite
   P2 adding a new channel (P1's suppression pays for P2's additions).
4. **In-turn fire precision**: P2 fires on <10% of tool calls; each fire's
   novel-term overlap with the eventual next user prompt (retrospective) as a
   relevance proxy.
5. **Cornerstone coverage**: every active cornerstone has ≥1 verified channel
   (warm-briefing anchor OR trigger entry) — `engram_diagnose` check green.

## §5 Implementation split (proposed)

| Stream | Issue | Owner | Notes |
|---|---|---|---|
| P1 ledger + render suppression | #1689 | Sol | Pure hook + render change; testable against replayed event logs. |
| P1 not-recalled slot reservation | #1689 | Sol | Same PR or stacked. |
| P2 in-turn recall | #1690 | Kepler | Design-sensitive (novelty gate); behind config flag; replay-bench perf gate. |
| P3 cornerstone tripwire + unified triggers | #1691 | Kepler | Substrate-semantic (register_exemplar, caches). |
| P4 budget + telemetry | #1692 | Sol | After P1 lands (ledger is a dependency). |

Umbrella: #1688.

Reciprocal review per repo convention (reviewer-fairy → counterpart colleague
→ maintainer). Ship P1 first: it is the cheapest, the most user-visible, and
funds the context budget the other streams spend.
