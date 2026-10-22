# F3 — Hook spawn cost (H4): end-to-end wall time per registered hook

Perf umbrella #1668, hypothesis H4: existing telemetry (index.db) times hook
**bodies** only; the invisible cost is interpreter spawn + imports per
invocation. Claude Code spawns a fresh `python3` process per registered
matching hook per event, so per-prompt latency = sum over hooks that fire.

## Method

- Measured END-TO-END wall time (`date +%s%N` around the process, not just the
  hook body) via `echo '<payload>' | ENGRAM_HOME=<sandbox> python3 <hook>`.
- N=5 runs each, 1 warmup discarded, median reported (variance was tiny — all
  runs within ±1-2ms of each other, so median ≈ mean here).
- **Side-effect safety**: every hook resolves its state paths (config,
  cursors, DB, markers) through `os.environ.get("ENGRAM_HOME")`. Rather than
  skip risky hooks, I built a **sandboxed ENGRAM_HOME**
  (`scratchpad/bench-engram-home/`) — a copy of the real `config.json` and a
  full-size `knowledge.db` copy (5.5MB, so DB-open cost is realistic) — and
  pointed every hook at it via env var. All cursor/marker/DB writes landed in
  the sandbox, not live state. No hook posted to the forum or mutated the
  real knowledge graph; `engram-utility-credit-mention-stop.py`'s `UPDATE
  nodes SET utility_score` ran only against the sandboxed DB copy.
- Two hooks (`engram-forum-prompt-hook.py`, `engram-baton-prompt-hook.py`)
  made **real GET requests** to the live shared forum server
  (`localhost:5002`, confirmed running) and `gh` (confirmed authenticated) —
  both read-only, so their measured times include a genuine local network
  round-trip, which is realistic for the end-to-end estimate.
- Interpreter: every hook in `hooks.json` invokes plain `python3` (system
  `/usr/bin/python3`), not the plugin venv — verified from the command lines
  and shebangs. Only `start-engram-daemon.sh` (SessionStart) launches the
  daemon itself via the venv python; the hook script that fires per-prompt
  does not. All measurements below use system `python3`, matching
  registration exactly.

## Baselines

| Baseline | Median ms |
|---|---|
| `python3 -c "pass"` | 9 |
| `python3 -c "import sqlite3, json"` | 12 |

## UserPromptSubmit (8 hooks registered)

| Hook | Median ms (E2E) | Exit | Side-effect note |
|---|---|---|---|
| engram-time-bar-hook.py | 20 | 0 | writes `last-user-activity`/`last-user-msg` — sandboxed |
| engram-user-identity-hook.py | 14 | 0 | opens DB, writes `current_user.json` — sandboxed |
| engram-surface-hook.py | 32 | 0 | opens DB (FTS query) + writes counter/reminder markers — sandboxed |
| engram-deference-detector-prompt.py | 18 | 0 | writes cooldown marker — sandboxed |
| engram-end-of-day-hook.py | 15 | 0 | reads sleep marker only |
| engram-inter-agent-prompt-hook.py | 49 | 0 | advances surfaced-cursor (sandboxed); imports `forum_api`, may hit forum |
| engram-baton-prompt-hook.py | 59 | 0 | real GET to live forum + potential `gh` subprocess (read-only); cache write sandboxed |
| engram-forum-prompt-hook.py | 57 | 0 | real GET to live forum server (read-only) |
| **Sum** | **264 ms** | | |

## Stop (3 hooks registered)

| Hook | Median ms (E2E) | Exit | Side-effect note |
|---|---|---|---|
| engram-stop-hook.py | 15 | 0 | atomic-write of write-nudge state — sandboxed |
| engram-deference-detector-stop.py | 17 | 0 | writes marker — sandboxed |
| engram-utility-credit-mention-stop.py | 19 | 0 | `UPDATE nodes SET utility_score` — ran against sandboxed DB copy only |
| **Sum** | **51 ms** | | |

## Other events (measured for completeness, not summed into the per-prompt-cycle total)

| Hook | Event | Median ms (E2E) | Exit |
|---|---|---|---|
| engram-bash-pipe-exit-warn.py | PreToolUse (Bash) | 13 | 0 |
| engram-lesson-tripwire-hook.py | PreToolUse (Bash/mcp) | 17 | 0 |
| engram-toolcall-repair.py | PreToolUse (mcp__*) | 16 | 0 |
| engram-session-start-hook.py | SessionStart | 43 | 0 |

(PreToolUse hooks fire once per matching tool call, not once per prompt — a
Bash-heavy turn pays `13+17=30ms` per Bash call, `16ms` per MCP `engram_*`
call. Not folded into the per-prompt-cycle sums above since cardinality
differs from UserPromptSubmit/Stop.)

## Top-3 slowest, attributed

1. **engram-baton-prompt-hook.py (59ms)** — heaviest imports (`pwd`, `re`,
   `shutil`, `subprocess`, plus a `sys.path`-walk to locate and import
   `forum_api`/baton tooling), then a real GET to the forum server
   (`timeout=3`, but the live server answered fast) and a possible `gh`
   subprocess call for court/anchor state (cached, `timeout=4` worst case).
   Attribution: import breadth + one network round-trip, not DB cost.

2. **engram-forum-prompt-hook.py (57ms)** — imports `urllib.request`, then a
   synchronous `GET /api/threads?since=...` to the live forum server at
   `localhost:5002` (`timeout=3`). Attribution: almost entirely the real
   network round-trip (imports alone are cheap — bare `urllib` is stdlib).

3. **engram-inter-agent-prompt-hook.py (49ms)** — same `sys.path`-walk +
   `forum_api` import pattern as baton, plus its own GET to the forum for
   letter-cursor state. Attribution: import-resolution walk (parents-search
   for `forum_api`, per the plugin's flattened-tree convention) + one network
   round-trip.

`engram-surface-hook.py` (32ms, 4th place) is the one dominated by genuine
compute rather than network/import: it opens the 5.5MB `knowledge.db` and
runs an FTS query — real DB-open + query cost, not spawn overhead.

## Verdict

**H4 is material.** Per-prompt-cycle overhead on this machine is
**~264ms (UserPromptSubmit) + ~51ms (Stop) ≈ 315ms of hook spawn+import+run
cost per prompt turn**, on top of whatever the hook bodies' own telemetry
already accounts for (0-900ms). Against the bare-interpreter floor (9ms) each
hook pays 5-7x its own logic in pure Python startup + import tax, and three
of the eight UserPromptSubmit hooks (baton/forum/inter-agent, sharing the
`sys.path`-walk + `forum_api` import + network-call pattern) account for over
60% of the UserPromptSubmit total — consolidating them into a single spawned
process (or caching the `forum_api` import path) is the highest-leverage fix
if this overhead needs cutting.
