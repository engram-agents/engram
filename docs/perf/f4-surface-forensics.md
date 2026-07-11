# F4 — engram_surface failure forensics (H5b + error classification)

Perf umbrella #1668. Read-only analysis of `/home/agent-borges/.engram/knowledge.db`
(`tool_timing`) and `/home/agent-borges/.engram/logs/index.db` (`events`, copied to
scratch to open — direct `mode=ro` open failed with "attempt to write a readonly
database", likely a WAL-checkpoint side effect on connect).

## Q A — the ~83-85s stall class (4 events)

| id | timestamp | duration_ms | status | turn |
|---|---|---|---|---|
| 8918 | 2026-05-14T22:21:40Z | 84720 | success | 48 |
| 8921 | 2026-05-14T22:26:06Z | 84508 | success | 48 |
| 8924 | 2026-05-14T22:31:44Z | 84179 | success | 48 |
| 24065 | 2026-06-13T13:52:29Z | 83102 | success | 78 |

**All 4 are `status=success`** — this is a slow-but-completing call, not a timeout
error. `index.db` recovers the exact query for the 06-13 row (uuid
`c0cd86e77069438cb8b16e5bdba7dbc1`): `data = {"query":"warmup","top_k":1,"semantic":true}`,
`daemon_latency_ms=83102` (matches `tool_timing` exactly). **This is the daemon's own
startup warmup call**, fired unconditionally at
`src/engram/hooks/claude/engram-surface-daemon.py:280`:

```python
_client.call("engram_surface", {"query": "warmup", "top_k": 1, "semantic": True})
```

Immediately after (13:52:49, 20s later, same turn) a real query on the same session
completes in 83ms via the now-warm daemon (`daemon_latency_ms: 83`,
`fallback_to_fts: false`) — confirming the 83s cost is paid exactly once per daemon
(re)start, on model load, not per-query.

### Candidate root cause chain (ranked)

1. **CONFIRMED — HuggingFace Hub network etag-check retry ladder on
   `SentenceTransformer(model_name)` load.** `_load_model` at
   `src/engram/engram_core.py:86-99` calls `SentenceTransformer(model_name)`
   (`engram_core.py:94`), which — unless `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE`
   is set — performs a network etag/revision check even when the model is
   already locally cached. No caller in the codebase sets this env var or
   `local_files_only=True` (grepped `HF_HUB_OFFLINE|TRANSFORMERS_OFFLINE|local_files_only`
   across `src/engram/` — zero hits). Constants in the installed venv
   (`~/.engram/venv/lib/python3.12/site-packages/huggingface_hub/constants.py:35-37`):
   `DEFAULT_ETAG_TIMEOUT = 10`. Retry wrapper
   (`huggingface_hub/utils/_http.py:397-491`, `_http_backoff_base`):
   `max_retries=5`, `base_wait_time=1`, `max_wait_time=8`, exponential backoff
   (`sleep_time = min(max_wait_time, sleep_time * 2)`).
   **Arithmetic**: 6 attempts (`nb_tries` 1..6, raises once `nb_tries > max_retries`)
   × 10s connect/etag timeout = 60s, plus inter-attempt sleeps
   1+2+4+8+8 = 23s → **83s total**, matching the 4 observed stalls (83.1-84.7s)
   to within call-overhead noise. This is the near-constant the telemetry
   flagged — it's baked into `huggingface_hub`'s own defaults, not ENGRAM code,
   and fires whenever the sandbox/host has no (or a black-holed) route to
   `huggingface.co` at the moment the daemon cold-starts.
2. Ruled out: no ENGRAM-local timeout constant sums to ~84s. Local timeouts found:
   daemon socket `settimeout(5.0)` (`engram-surface-daemon.py:319`), MCP-client
   socket `settimeout(10)` (`engram-surface-hook.py:456`), various sqlite
   `timeout=1.0/5.0` connects. None retry to ~84s on their own.
3. Ruled out: model-load-from-disk time alone (typically 1-3s for
   `all-MiniLM-L6-v2`, and the doc comment at `engram-surface-hook.py:165`
   itself says "typical load is 7-15s... slower hardware 30-60s") — too fast
   and too variable to explain 4/4 events landing within a 1.6s band.

**Recommended fix**: force `local_files_only=True` (or set
`os.environ["HF_HUB_OFFLINE"] = "1"`) at daemon startup before the first
`SentenceTransformer(...)` call in `engram_core.py:94`, since ENGRAM ships/expects
the model to already be cached post-install; only fall back to an online
(short-timeout) load if the local-only load raises a cache-miss error. If
online resolution must stay enabled, set `HF_HUB_ETAG_TIMEOUT=1` (env, read at
import time by `huggingface_hub.constants`) so a black-holed network fails in
~1s per attempt (6-9s total) instead of 83s, with the existing local-cache
fallback path doing the rest.

## Q B — the 320 errors (3.65% of 8,763 calls)

**All 320 rows have `duration_ms=0`** and **all 320 fall inside a single
contiguous window**: turn 61 (2026-05-27T12:09:36Z – 2026-05-28T01:12:07Z, 71
errors, 2 successes), turn 62 (2026-05-28T01:24:44Z – 2026-05-29T01:20:58Z, 98
errors, 1 success), turn 63 (2026-05-29T01:29:17Z – 2026-05-29T21:55:56Z, 151
errors, 41 successes). **Zero errors outside this ~2.5-day window** across the
rest of the 8,763-call sample. In the same window, every other tool was
essentially unaffected (`engram_list`: 3 errors, everything else: 0).

This is a **sustained, tool-specific outage**, not per-query noise or
content-dependent DB failure — a corrupted-DB or locking hypothesis doesn't fit
the interleaved successes (41 successes inside turn 63 alongside continuing
errors) or the razor-sharp start/end boundary.

**Error shape**: the global timing decorator
(`src/engram/server.py:1234-1263`, `_timing_mcp_tool`) wraps every `@mcp.tool`
function; any raised (uncaught) exception is caught here, logged as
`status=error` with the *actual* elapsed wall-clock, then re-raised. `duration_ms=0`
for all 320 means the exception fired essentially instantly — before
`_surface_impl`/`_search_nodes` did any real work. The parallel `index.db`
rows for this window (`event_type=engram.tool.engram_call`, `tool_name=engram_surface`,
`result_status=error`) all carry `data: '{}'` — the sanitized-payload field is
empty, consistent with failure at/near the `payload_json` parse boundary in
`engram_surface()` (`server.py:1684-1688`):

```python
try:
    params = json.loads(payload_json)
except json.JSONDecodeError as e:
    return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
```

This only catches `json.JSONDecodeError`. If `payload_json` arrives as a
non-str value (e.g. `None`, or an already-parsed object from some client/
transport variant), `json.loads(...)` raises `TypeError`, which is **not**
caught here — it propagates uncaught to `_timing_mcp_tool`, matching every
observed signature: instant failure, tool-specific, empty logged payload.

**Classification**: consistent with an intermittent client-side payload-shape
regression scoped to `engram_surface` calls, active for ~2.5 days
(2026-05-27 – 2026-05-29, turns 61-63) and then resolved/reverted — not
consistent with DB corruption (would not self-heal mid-window with interleaved
successes) and not consistent with the daemon-unreachable/fallback class (see
Q C — zero temporal overlap).

## Q C — fallback_to_fts clustering (130/4,374 = 2.97%)

Fallback events are clustered, not scattered: only **19 of the 48 distinct
days** with any `engram.surface.fire` activity show a nonzero fallback count
(range 2026-05-17 to 2026-07-06); several days show sharp bursts (e.g.
2026-06-05/06/07: 12/20/25, 2026-06-27: 21) rather than a steady per-query
background rate.

**124 of 130 (95%) fallback rows show `daemon_latency_ms=0`** — an instant
connection failure (socket refused / no daemon listening), not a slow daemon
that eventually times out. This is a **binary daemon-unreachable state**
(daemon not yet started this session, or crashed/idle-shut-down and not yet
relaunched), structurally distinct from the Q A stall class (which is a slow
but *successful* daemon-path completion).

**No overlap with the Q B error window**: zero fallback events fall inside
2026-05-27–2026-05-30 (the turn 61-63 outage). Fallback and the 320
immediate-fail MCP errors are separate populations with different mechanisms
and different time distributions — the outage in Q B did not manifest as
daemon-fallback at all (the auto-surface hook's daemon-mediated
`engram.surface.fire` calls kept succeeding at normal latency, 42-557ms,
throughout that same window per the raw event rows), reinforcing that Q B is
specific to the direct MCP `engram_surface` tool-call path, not the daemon.

## Summary table

| Class | Population | Root cause | Fix |
|---|---|---|---|
| ~83-85s stall | 4 events, all `success` | HF Hub network etag-check retry ladder (6×10s + 23s backoff ≈ 83s) on daemon warmup model load | `local_files_only=True` / `HF_HUB_OFFLINE=1` at daemon start, or `HF_HUB_ETAG_TIMEOUT=1` |
| 320 errors (3.65%) | Single ~2.5-day window (turns 61-63), `duration_ms=0`, tool-specific | Likely uncaught `TypeError` from `json.loads(payload_json)` on non-str payload in `engram_surface()` (only `JSONDecodeError` is caught) | Catch `(TypeError, json.JSONDecodeError)` in the payload-parse block; add a type-guard before `json.loads` |
| Fallback (2.97%, 130/4374) | Clustered on 19/48 days, 95% instant (`daemon_latency_ms=0`) | Daemon simply not running (cold-start window / post idle-shutdown / crash), not a slow daemon | Separate from both classes above — expected/benign given daemon lifecycle; no fix needed beyond confirming idle-shutdown/relaunch cadence is acceptable |
