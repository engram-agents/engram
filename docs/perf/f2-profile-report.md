# ENGRAM MCP Tool Latency — cProfile Attribution (2026-07-06)

**Method**: scratch COPY of `agent-borges`'s live `knowledge.db` (5808 nodes,
96MB, 149 resolved-status nodes) at
`/tmp/claude-1006/.../perfwork/data/` (`ENGRAM_HOME` redirected — verified
`engram_core.DATA_DIR`/`DB_PATH` point at the copy before any calls ran).
Called `_inspect_impl` / `_query_impl` / `_list_impl` / `_add_observation_impl`
/ `_advance_turn_impl` directly (bypassing the MCP transport), N=10 profiled
calls each after one untimed warm-up call per tool. cProfile `.prof` + text
dumps in `f2-profile-raw/`. **No node text is quoted anywhere below or in the
raw dumps** — only IDs, counts, and function-level timings. Scratch DB deleted
after this run (see confirmation at bottom).

## Headline: local single-process numbers do NOT match production telemetry — and that gap is itself a finding

| Tool | Telemetry avg (prod) | Profiled avg (local, N=10) | Ratio |
|---|---|---|---|
| engram_inspect | 6967ms | 82.9ms (recall) / 79.0ms (deep) | ~84x / ~88x |
| engram_query | 9035ms | 182.9ms | ~49x |
| engram_list | 4550ms | 159.8ms | ~28x |
| engram_add_observation | 688ms | 212.2ms | ~3x |
| engram_advance_turn | 8095ms | 904.1ms | ~9x |

The two tools that do real O(n)/O(graph-size) work locally (`add_observation`:
embedding compute; `advance_turn`: full-DB dump + git) land within an order of
magnitude of production. The three "should be cheap" reads (`inspect`,
`query`, `list`) are 28–88x off — nothing in a serial, no-concurrency profile
explains a gap that large. That points to a production-only multiplier not
reproducible in this harness: most likely multi-agent concurrent access to the
same SQLite file (see H6/PRAGMA findings below) causing SQLITE_BUSY waits, or
a substantially larger/more-resolved production graph than this copy (see
H2 — the dominant local cost scales with count of `resolved`-status nodes,
which was only 149 here). **Recommend a follow-up: reproduce with 2+
concurrent processes hammering the same DB file**, which this single-process
harness cannot do safely against a live graph.

## Per-hypothesis verdicts

**H1 (in-process embedding / CrossEncoder rerank dominates query latency;
does inspect hit models at all?) — KILLED as stated, PARTIAL for a different mechanism.**
- `engram_inspect` (no query text): profile shows **zero** embedding/model
  calls anywhere in the top functions. It does not hit models at all — its
  cost is 96% inside `_get_db()` (see H2). Sub-question answered: inspect's
  latency is "something else entirely," not models.
- CrossEncoder is **not on the query or inspect path at all** — grep confirms
  the only call site is `engram_observation.py:211`
  (`_compute_polarity_alerts`, add-observation path only), and it's gated by
  `config["polarity"]["enabled"]`, which is **absent from this graph's
  config.json → defaults to `False`** (`engram_observation.py` `_get_polarity_config`).
  Measured its cold load anyway for completeness: **132.1s** one-time load for
  `dleemiller/ModernCE-large-nli` — irrelevant to inspect/query but would be a
  severe first-call cliff for any user who *does* enable polarity checking.
- `engram_query` genuinely does spend model/embedding-adjacent time, but not
  in CrossEncoder or even mostly in `SentenceTransformer.encode()` (warm
  encode measured separately at 3.4ms — trivial). The real cost is
  `_mmr_rerank`'s **pure-Python O(n²)-ish diversity rerank** (0.582s / 1.829s
  = 32%): 900 calls to `_max_cosine_to_selected` → 3900 `cosine_similarity`
  calls, plus **JSON-decoding every candidate's stored embedding vector from
  text on every query call** (`_decode_embedding` → `json.loads`, 5090 calls,
  0.228s). Embedding storage-as-JSON-text (re-parsed every query) plus a
  pure-Python MMR loop is the real "embedding-adjacent" cost — not the model
  forward pass itself.
- **Cold model-load cost** (measured directly): SentenceTransformer
  `all-MiniLM-L6-v2` first `embed()` call = **2.884s** (one-time per process),
  warm calls = **3.4ms**. Confirms lazy-load is a real but one-time/amortized
  cost, not a per-call tax — consistent with it not showing up per-call in the
  N=10 profiles (warm-up call already absorbed it).

**H2 (write-on-read + per-call tool_timing INSERT/commit) — CONFIRMED, and the real mechanism is bigger and different than described.**
This is the dominant finding. `_get_db()` (`engram_core.py:1259`, called at
the top of **every** tool invocation to obtain a connection) does not just
open a connection — it **unconditionally re-runs the full one-time
schema/migration/backfill/invariant-check block on every single call**, with
no "already ran this process/session" guard (unlike the schema-version
migrations elsewhere in the same file, which correctly gate on `PRAGMA
user_version`). Breakdown on `engram_inspect_recall` (96% of total 82.9ms
lives inside `_get_db()`):
  - `_backfill_resolved_by` (`engram_core.py:956`) — **53% of total call time**
    (0.436s/0.829s over 10 calls). For every call, scans resolved-status nodes
    and runs `_best_resolver_for` (a JOIN over edges+nodes) per row — genuinely
    O(resolved-node-count), and it re-does this scan on literally every
    `_get_db()` call, not once.
  - `_backfill_vec_nodes` (`engram_core.py:892`) — **27% of total** (0.221s):
    reads the full `vec_nodes` table AND all `nodes.embedding IS NOT NULL`
    rows every call to check for missing backfill rows.
  - Plus: `_backfill_source_type`, a DAG-invariant integrity scan (JOIN over
    all edges+nodes, logs warnings — this is the source of the repeated
    `DAG violation: <derivation> -> <observation>` log spam observed once per call, 2
    pre-existing violations × every `_get_db()` invocation), an idf-vocab
    ensure-table check, a focus_state upsert, and a full `executescript` of
    ~15+ `CREATE TABLE/INDEX/VIRTUAL TABLE IF NOT EXISTS` statements — all
    re-run per call.
  - The literal "refresh importance/recall counters on read" piece is real
    but **tiny**: `_refresh_recall` = 0.015s/10 calls (1.5ms/call) — not a
    meaningful contributor by itself.
  - The literal "tool_timing INSERT+commit" piece: measured `_record_tool_timing`
    directly (via `server.py`, N=20) — **89.2ms/call**, of which **98% is its
    own internal `_get_db()` call re-triggering the identical backfill scan a
    SECOND time.** Since `_record_tool_timing` runs in the `finally` block of
    every `@mcp.tool`-wrapped function (`server.py` `_timing_mcp_tool`), **every
    real tool call in the live server pays this full backfill-scan cost at
    least twice** (once for the tool's own `_get_db()`, once again for
    `_record_tool_timing`'s) — confirmed by `_get_db` call counts in the
    profiles: 1:1 for directly-called `_inspect_impl` (bypasses the wrapper in
    this harness) vs 2:1 for `_list_impl`/`_add_observation_impl` (call
    `_get_db()` twice internally already) — the wrapper's timing call is a
    third hit not exercised by this harness at all, meaning **live-server
    numbers are understated even further** by what's shown here.
  - Net effect: this single unguarded per-call re-scan is 65-96% of total time
    across inspect/query/list/add_observation in this profile, and it scales
    with graph structure size (resolved-node count, edge count) — the more
    mature/larger the graph, the worse every call gets, unboundedly, forever.

**H3 (payload serialization size — deep/edges views, neighbor assembly) — KILLED.**
`_build_inspect_recall_view` / `_build_inspect_deep_view` cumulative time:
0.002s over 10 calls each — negligible. Neighbor/edge assembly and JSON
response shaping are not meaningful contributors for either view mode on this
graph.

**H6 (SQLite PRAGMAs) — CONFIRMED as a real, fixable, compounding contributor (not the sole explanation).**
Measured directly on the live connection:
```
journal_mode = wal        (good)
synchronous  = 2 (FULL)    — conservative default; NORMAL (1) is safe & faster in WAL mode, not set
cache_size   = -2000       — only ~2MB page cache; small for a 96MB+ growing DB
mmap_size    = 0           — memory-mapped I/O disabled entirely; not set
busy_timeout = 5000        — 5s; plausible source of the prod/local gap under concurrent
                             multi-agent access to the same file (see Headline section)
page_size    = 4096         (default, fine)
temp_store   = 0 (file)    — temp b-trees spill to disk instead of memory; not set
```
None of these alone explains a 28-88x gap, but combined with H2's per-call
backfill re-scan (many more disk reads per call than necessary, hitting a
2MB cache with mmap off) they compound: every call does far more I/O than
needed, against non-optimal cache/mmap/sync settings. The 5000ms
`busy_timeout` is the standout candidate for explaining the *residual*
prod/local gap specifically under concurrent access — a busy writer (e.g.
another agent's simultaneous tool call, given `_get_db()` opens a **new**
connection per call rather than pooling/reusing one) could force a reader to
wait most of that 5s window before proceeding, repeatably, which is roughly
the right order of magnitude to explain telemetry averages of 5-9 seconds. This is a hypothesis my harness cannot confirm directly (no safe way to load-test a live graph concurrently) — flagging as the top follow-up.

**Distinct finding, not in the original hypothesis list — `engram_advance_turn`'s cost is fully attributed and is real, not a profiling artifact:**
9.041s/10 calls = 904ms avg, **the one tool where local profiling lands in the
same order of magnitude as production (8095ms)**. Breakdown:
- `_commit_snapshot` (`engram_core.py:3170`) = **87% of total** (7.877s/9.041s).
  - `dump_stripped` (`engram_backup.py:76`) = 52% of total (4.741s): serializes
    the **entire database** to a SQL text dump via stdlib
    `sqlite3/dump.py:_iterdump` — 566,250 individual function calls, 1.84s
    tottime — genuinely O(total-row-count), on every single turn-advance call.
  - `_git` subprocess calls (`engram_core.py:652`) = 22% of total (1.962s
    across 63 `subprocess.run` calls): shelling out to `git` repeatedly
    (snapshot commit), each paying `subprocess.communicate`/`select.poll`
    IPC overhead (~1.92s just in `poll()`).
  - Plus ~0.910s in `commit()` calls (30 commits across the operation).
- This is a real, structural cost (full-DB dump + git commit on every
  `engram_advance_turn`), not a profiling artifact — and unlike the other
  three tools, it scales predictably with graph size, which is consistent
  with it being the one tool whose local-vs-prod ratio (9x) is close to
  expected variance rather than a mystery 28-88x gap.

## Top-5 ranked recommendations (evidence-based)

1. **Gate the one-time backfill/DAG-check block in `_get_db()` behind a
   process-lifetime flag or a `PRAGMA user_version`-style one-shot marker**,
   matching the pattern the codebase already uses correctly for schema
   migrations elsewhere in the same file. This is the single highest-leverage
   fix — it is 65-96% of profiled time across 4 of 5 tools tested, confirmed
   by direct cProfile attribution to `_backfill_resolved_by` and
   `_backfill_vec_nodes` specifically.
2. **Stop opening a fresh connection per `_get_db()` call; pool/reuse one
   connection per process** (or at minimum, per MCP server lifetime). This
   both removes redundant migration-scan invocations and is the mechanism
   most likely to reduce concurrent-access lock contention (H6) since fewer
   simultaneous connections means fewer writers to block on.
3. **Move `_record_tool_timing`'s DB write off the hot path** (e.g. batch/
   buffer timing rows in memory and flush periodically, or write to a
   separate lightweight always-open connection) — confirmed to independently
   cost ~89ms/call, 98% of which is a second full backfill-scan pass,
   currently paid on **every** tool call in the live server via the
   `_timing_mcp_tool` wrapper's `finally` block.
4. **Tune the underused PRAGMAs**: `synchronous=NORMAL` (safe + faster under
   WAL), `mmap_size` to a few hundred MB, `cache_size` up from the current
   ~2MB, `temp_store=MEMORY`. Cheap, safe, and compounds with fix #1/#2 since
   every call currently does far more I/O than necessary.
5. **For `engram_advance_turn`: avoid a full `_iterdump` SQL-text serialization
   of the whole DB on every turn-advance.** Either checkpoint-based
   incremental backup (WAL-level) or a periodic (not per-turn) full dump would
   remove the confirmed 52%-of-call `dump_stripped`/`_iterdump` cost, and
   batch/reduce the 63 separate `git` subprocess invocations per call (22% of
   call time in IPC overhead alone).

## Files

- Per-tool cProfile dumps + top-15 text summaries: `f2-profile-raw/*.prof`,
  `f2-profile-raw/*.txt`
- `f2-profile-raw/record_tool_timing.prof` — isolated profile of
  `_record_tool_timing` confirming the double-backfill-scan finding above.

## Scratch DB disposal

The scratch copy of `agent-borges`'s `knowledge.db` (+ `-wal`/`-shm`) used for
this profiling run has been deleted (see confirmation below) — no copies of
another agent's memory graph are retained anywhere outside this run's
now-cleaned scratch directory.
