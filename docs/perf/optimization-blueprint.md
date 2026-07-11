# ENGRAM Performance Optimization Blueprint

**Author:** Kepler (Fable seat), 2026-07-06 — diagnostic day with Lei; execution by team over following weeks.
**Status:** living document. Hypotheses marked H* need profiling confirmation; quick wins marked QW*, medium-term M*, long-term L*.

## 0. Prime directive: faster, never looser

No optimization may weaken an epistemic guarantee (provenance checks, verbatim-quote
verification, trust gates, cascade semantics, edit-history). Where a check is expensive,
the fix is caching / batching / compiling / moving it off the critical path — never
skipping it. A perf gain bought with graph-integrity loss is not a faster brain; it is
an illusion (Lei: "that's an illusion created by using drugs"). Every perf PR must show
(a) benchmark before/after, (b) full affected-tests green, (c) an explicit "guarantees
untouched" statement in the PR body.

## 1. Evidence base — REVISED after F1 telemetry deep-dive (2026-07-06)

Borges's telemetry: `tool_timing` (28,675 calls, 2026-04-16 → 07-06, server-side
wall-clock inside the tool body) + `logs/index.db` events. Full analysis:
`f1-telemetry-analysis.md` + backing JSON in the shared perf-2026-07-06 workspace
(in-repo archival pending Borges's OK — it's his usage data, IDs/timings only).

**The headline "reads cost 7-9s" was TRUE HISTORICALLY, FALSE CURRENTLY** — an
early framing of this blueprint took the all-time averages at face value; F1's
percentile + time-bucket analysis corrected it same-day. The all-time averages are
dominated by a 10-day incident window (2026-05-17→27, turns 50-60): dense
single-turn bursts (largest: 2,362 inspect calls in 47 min at 2 a.m.) with
call-over-call escalating durations — a queueing/contention signature. The bursts
map to the pre-batch dream-cycle architecture, and their end maps to the
batch-summary/cohort-dispatch rework (#410/#411/#417, merged 05-26/27). Pre/post
split:

| tool | pre-05-27 avg | post-05-27 avg | post p50 | current verdict |
|---|---|---|---|---|
| engram_inspect | 9,104ms | **285ms** | 48ms | fast; historical avg misleading |
| engram_query | 11,597ms | **831ms** | 166ms | acceptable; p90 3.6s (June) worth a look |
| engram_list | 7,207ms | 314ms | 105ms | fast |
| engram_surface | 902ms | **593ms** | 288ms | **live target**: growth trend + tails + errors |
| engram_add_observation | 909ms | 324ms | 151ms | fine; historical 370s tails gone |
| engram_nap | 12,146ms | **6,237ms** | 5,612ms | genuinely expensive; scales with graph |
| engram_advance_turn | 10,231ms | **6,804ms** | 5,768ms | genuinely expensive; scales with graph |

Cross-check on my (Kepler's) small graph (~300 nodes, July, current code): surface
224ms avg, add_obs 110ms, advance_turn 698ms, nap 119ms — consistent with
"per-call costs are fine at small scale; consolidation ops scale ~10× over ~19×
nodes."

**What is actually live now (ranked):**

1. **Concurrency/queueing under fan-out** — the May incident class. Fixed for the
   dream cycle by batching, but the *substrate-level* vulnerability (many clients
   → one Python MCP process → serialized SQLite) is untouched; any future fan-out
   workload (fairy cohorts, multi-agent shared graphs) re-triggers it.
2. **engram_surface** — the every-prompt tool. p50 288ms and growing with graph
   size (179→327ms April→July over 2× nodes → projects ~1s at 20k+ nodes); an
   unresolved 83-84s stall class that recurred post-incident (06-13); 3.65% error
   rate (highest of any tool); 2.97% FTS-fallback rate.
3. **Consolidation ops** (nap 6.2s, advance_turn 6.8s, checkpoint ~7s while it
   existed) — by-design heavy but graph-scaling; they gate wake/sleep transitions.
4. **Hook spawn overhead — UNMEASURED.** index.db hook events time the hook *body*
   (negligible, <1ms-900ms) but NOT interpreter spawn + imports (~100-300ms × 19
   registered hooks × per-event). Plausibly the largest invisible per-prompt cost
   on current installs. Needs external end-to-end measurement (harness scenario).
5. **Telemetry hygiene** — 5/7 hooks silent in index.db for 3-5 weeks (disuse vs
   rename vs broken logging — undiagnosed); engram_checkpoint vanishes after May
   (retired or renamed — uncounted either way); tool_timing does INSERT+commit per
   call (measurement taxing the measured).

## 2. Hypotheses — verdicts after F2 profiling + Sol's dispatch-model verification

F2: cProfile attribution against a 5.8k-node graph copy (f2-profile-report.md).
Local serial timings: inspect 83ms, query 183ms, list 160ms, add_observation
212ms, advance_turn 904ms — consistent with F1's post-incident steady state.

- **H1 (models dominate reads): KILLED.** CrossEncoder isn't on the read path at
  all (only add_observation's polarity check, config-gated off). Warm encode =
  3.4ms; cold model load = 2.9s one-time per process. Query's residual cost is a
  pure-Python O(n²)-ish MMR rerank + per-call JSON-decode of stored embedding
  vectors (32% of query time) → **#1674**.
- **H2 (write-on-read / telemetry): CONFIRMED, bigger than hypothesized.**
  `_get_db()` re-runs an unguarded backfill/migration/DAG-check block on EVERY
  call — `_backfill_resolved_by` alone is 53% of inspect's time; the block is
  65-96% of profiled time across read tools (neighboring migrations correctly
  gate on `user_version`; this block doesn't) → **#1669**. `_record_tool_timing`
  costs ~89ms/call, 98% of it re-triggering the same scan via its own
  `_get_db()`, on every tool call → **#1670**. (The literal importance-refresh
  write is real but tiny: 1.5ms/call.)
- **H3 (payload size): KILLED** — view assembly ≈0.2ms/call.
- **H4 (hook fleet spawn cost): CONFIRMED MATERIAL (F3, 2026-07-06).**
  End-to-end: UserPromptSubmit ≈264ms across 8 hooks + Stop ≈51ms across 3 —
  **~315ms per prompt turn**, invisible to index.db's body-only timing. Bare
  interpreter start is 9ms; hooks pay 5-7× that in import tax. Dominant: the
  three forum-backed prompt hooks (baton 59ms + forum 57ms + inter-agent 49ms),
  each doing its own forum_api import-walk + live HTTP round-trip per prompt →
  **#1680** (consolidate to one round-trip; coordinate with the town-square
  unified cursor, forum #170; M1 is the structural end-state).
- **H5b (surface ~84s stall class): CONFIRMED + root-caused (F4, 2026-07-06).**
  The constant is the HuggingFace Hub etag-check retry ladder — 6 × 10s
  DEFAULT_ETAG_TIMEOUT + backoff sleeps (1+2+4+8+8) = exactly 83s — triggered by
  the daemon's warmup model load; no HF_HUB_OFFLINE/local_files_only anywhere →
  **#1682** (force offline/local-cache load). Related F4 findings: the 320
  surface errors (3.65%) are ONE 2.5-day burst (turns 61-63), duration=0,
  best-explained by an uncaught non-str-payload TypeError (only JSONDecodeError
  is caught) → **#1683**; the 2.97% FTS-fallback events are 95% instant
  connection-refused (daemon not running) — benign, working as designed.
- **H6 (SQLite settings): CONFIRMED.** In effect: `synchronous=FULL`,
  `mmap_size=0`, `cache_size≈2MB`, `temp_store=file`, `busy_timeout=5000ms`,
  fresh connection per call → **#1671** (connection reuse), **#1672** (PRAGMAs).
- **H7 (consolidation ops scale with graph): CONFIRMED + attributed.**
  advance_turn = 87% `_commit_snapshot`: full-DB `_iterdump` SQL text dump (52%)
  + repeated git subprocesses, per turn → **#1673**.
- **Concurrency mechanism (dv-level, Sol-verified):** FastMCP runs sync tools on
  a real threadpool; connections are fresh-per-call (file-level lock contention
  under 5s busy_timeout, not a shared Connection); the module-level `_embedder`
  singleton is UNLOCKED — lazy-load race + concurrent `.encode()` on one model
  instance → **#1675** (thread-safety, gates the perf work). Harness concurrency
  mode 1 = N threads over one imported server process; mode 2 (subprocesses,
  multi-agent future) secondary.

## 3. Quick wins — confirmed + filed as sub-issues of umbrella #1668

Priority order (expected impact × risk):

1. **#1669** — gate `_get_db()`'s backfill/DAG-check block one-shot (the ~50-95%
   read-path lever). Guard set only after successful completion.
2. **#1670** — take `_record_tool_timing` off the hot path (dedicated connection
   or memory buffer + flush); currently ~89ms on every tool call.
3. **#1671** — per-process connection reuse (thread-safe per FastMCP's threadpool
   dispatch: connection-per-thread or a small pool).
4. **#1675** — lock the `_embedder` singleton (correctness gate for concurrency;
   ships before/with any concurrency-focused perf claims).
5. **#1672** — PRAGMA tuning (WAL + synchronous=NORMAL, mmap, cache, temp_store),
   each documented with its integrity tradeoff.
6. **#1673** — advance_turn snapshot: async/incremental/`.backup`-API instead of
   per-turn full `_iterdump` + git subprocesses.
7. **#1674** — query MMR rerank: decode-once vector cache / numpy vectorization /
   push top-k into sqlite-vec; equivalence-oracle-checked ordering.
8. **QW-open-a** Hook-spawn end-to-end measurement (H4) — if >500ms/prompt, it
   becomes the next headline and motivates M1.
9. **QW-open-b** Surface-stall forensics (H5b, ~84s constant) + surface error-rate
   diagnosis (3.65%, 320 rows).
10. **QW-open-c** `engram_inspect` batch mode (fan-out reduction for dream/audit
    flows).
11. **QW-open-d** Cold-start pre-warm: 2.9s model load moves from first tool call
    to server start.

## 4. Medium-term (weeks)

- **M1** One persistent **substrate daemon** serving MCP server *and* hooks (today: 19
  interpreter spawns per event class + an MCP process + a surface daemon). Hooks become
  thin socket clients (~ms); models loaded once per host.
  - *Note (2026-07-10, Kepler ruling):* **#1680's fat-hook merge (fix-a, "Slice-2") is
    superseded by M1 — do NOT re-pick it.** M1 eliminates the spawn+import cost class the
    merge targeted and restructures the hook layer into thin clients anyway, so the merge
    was a higher-cost interim win for a soon-restructured layer (and its blast-radius — the
    identity_coupled fixtures + the #713 anti-regression test + ~13 docs — makes the twice-over
    rewrite not worth it). Kept: #1680 **Slice-1** (`_prompthooklib` shared-prologue extraction,
    merged) — it's a down-payment on M1 (thin clients reuse the shared prologue).
- **M2** Replace torch/sentence-transformers with **ONNXRuntime int8** (or GGUF/
  llama.cpp embedder). *Demoted from latency-play to footprint-play by F1*: models
  aren't the per-call bottleneck, but torch is still the heaviest dependency
  (install size, memory, cold-start), and a lighter embedder makes M1's
  one-daemon-per-host model cheaper to keep resident.
- **M3** Quantized vectors in sqlite-vec (int8/bit) — smaller index, faster scan;
  sqlite-vec is already C, the win is representation.
- **M4** Prepared-statement + connection discipline; split read vs write connection;
  investigate `BEGIN CONCURRENT`/wal2 branch relevance.
- **M5** Perf regression gate in CI: replay-benchmark harness (see §6) with p50/p95
  budgets per tool; fail on regression. (A mechanical gate beats relying on
  vigilance to catch a regression by eye.)
- **M6** Hook-side caching of session-static context (config, roster, tier) — many
  hooks re-read the same files every prompt.

## 5. Long-term directions (team, weeks–months)

- **L1** **Compiled hot kernel**: vec search + recall scoring + graph walk as a Rust
  (pyo3) or C extension behind the existing Python semantic layer. Python stays the
  orchestration/semantics owner (guarantees legible), the kernel does the math. Only
  pursue after M1/M2 — most of today's cost is likely architectural, not language.
- **L2** Full compiled MCP server (Rust MCP SDK) reading the same knowledge.db —
  schema-as-contract makes this a parallel implementation, A/B-testable per tool.
  Higher risk to guarantee-fidelity; needs the §6 harness as a semantic-equivalence
  oracle before any cutover.
- **L3** Speculative pre-fetch: surface daemon warms likely inspect targets at
  prompt-submit (it already knows the recall set); MCP reads then hit a hot cache.
- **L4** Telemetry v2: sampling + level budget; per-call breakdown spans (embed /
  rerank / sql / serialize) so future regressions self-locate.
- **L5** Graph-scale readiness: partition/archival strategy for >100k-node graphs
  (Borges: ~2.4k current nodes already at 7s reads — scaling headroom is negative).

## 6. Method + guardrails

- **Benchmark harness**: replay real call traces (tool_name + params shape, content
  anonymized) against a copy graph; report p50/p95/p99 per tool. Borges's telemetry
  is the seed trace; my graph is the small-graph control.
- **Semantic-equivalence oracle**: for read tools, byte-compare (or field-compare)
  outputs before/after each optimization on the full replay corpus.
- Profiling before patching: every H* gets a cProfile/py-spy attribution first; no
  optimization lands on an unconfirmed hypothesis.

## 7. Today's work plan

1. Blueprint (this doc) — draft PR early, update as data lands. [Kepler]
2. Telemetry deep-dive: percentiles, trend vs graph growth, per-hook profile,
   tail forensics. [fairy F1]
3. Profiling: cProfile attribution for inspect/query/list/add_observation on a copy
   of a real graph → confirm/kill H1–H3, H6. [fairy F2]
4. Quick-win implementation in confirmed-hypothesis order. [Sol + fairies]
5. Umbrella issue + per-item sub-issues for the team. [Kepler]
