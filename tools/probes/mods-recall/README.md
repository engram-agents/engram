# engram-probe — mods-recall probe v0

A Claude Code **mod** (a plugin of function hooks; Claude Code 2.1.289 early-access
API) that MEASURES what thinking-keyed ENGRAM recall would surface at every model
step of a turn. It does not inject anything during the turn: the model reads exactly
what it would have read without the probe, until the one rating request at the end.

Dev tooling, tier T3 (`tools/probes` in `src/build/packaging/tiers.json`). Part of
engram-alpha #3362. Not shipped to users.

## What it does

Per model step (main loop only; subagent steps are skipped):

1. `turn.step` is observed, never changed. Every chunk is yielded as received, in
   order, and the result is returned as received.
2. After the step, off the turn's critical path (`$.clock.after(0, …)`), five
   retrieval **arms** run concurrently over the step, each through the same
   `engram_query` call (`return_debug: true`), so the comparison is about the
   QUERY, not the backend:
   - `thinking_full` — the last 6000 chars of the step's thinking
   - `thinking_tail` — its last 3 sentences (else the last 400 chars)
   - `extract_model` — a small model names 3 noun phrases the thinking assumes
   - `tool_input` — the step's tool-call arguments (the control: today's PreToolUse channel)
   - `novel_terms` — `novel_terms.py`: high-IDF terms not yet seen this probe session
     (parity with the in-turn recall hook; it only builds the query)
3. One JSON file per step: `<probeDir>/<sessionId>/steps/<turnId>-<NNN>.json`, with
   each arm's candidates (rank, scores, match type, latency) and cross-arm fields
   (`armsHit`, `inContext`, `principle`).
4. At `turn.complete` (answer, main loop, at least one step ran arms) ONE compact,
   **blinded** request is appended to the conversation: arms are shown as letters
   drawn at random per turn. The agent answers with one call to the tool the mod
   registers, `mcp__engram-probe__rate`, which writes
   `<probeDir>/<sessionId>/ratings/<turnId>-<ts>.json`.

Files outside `<probeDir>/<sessionId>/` are never written.

Invariant: every non-streaming registration (`session.start`, `turn.complete`,
`tool.call`) carries a `.catch` that replays `next`. The streaming `turn.step` hook
has none, by the declaration: a hook that fails mid-stream is left and the rest
streams from beneath it, and a replaying `.catch` could issue a second model
request. Observation inside the hook is wrapped in its own try/catch.

## Deploy (per seat)

Nothing here installs itself. To try it on a seat, point Claude Code at the folder:

```
claude --plugin-dir <repo>/tools/probes/mods-recall
```

or list it in `CLAUDE_CODE_PLUGIN_DIRS`. Options (`userConfig`, set in the config
menu or settings `pluginConfigs`): `enabled`, `topK` (1..20), `minThinkingChars`,
`arms`, `extractModel`, `extractTimeoutMs`, `maxRatingChars`, `probeDir`
(default `~/.engram/probe/mods-recall`; if `HOME` is unset the probe disables itself
rather than guess a path). An invalid value logs a line and falls back to the default
for that key only.

Arm E reads the graph at `$ENGRAM_HOME/knowledge.db` when `ENGRAM_HOME` is set, else
`~/.engram/knowledge.db` (read-only); the arm result's `db` field records which file
it used.

The ENGRAM MCP server is discovered from `$.tool.list()` (a tool named
`mcp__<server>__engram_query`); nothing is hardcoded. If none is found the probe
writes a note and runs no arms; step rows then say `skipped: no-engram-server`.

## Read the results

```
python3 convert_ratings.py <probeDir>/<sessionId> --out <dir>
python3 probe_eval.py --golden <dir>/golden --test <dir>/test \
    --pairs <dir>/pairs.jsonl --out report.md
```

`convert_ratings.py` writes one test RRU per (step, arm), one golden per rated step
(relevance 2 = `good`, 0 = `noise`, 1 = surfaced but unmarked; a rated id no arm
surfaced is kept with `notes: "unsurfaced"` so recall@k sees the miss), and the
unblinded pair verdicts. It prints a completeness summary and exits non-zero rather
than drop a step silently. `probe_eval.py` reuses `tools/recall_harness` (metrics,
golden loader, RRU dataclasses) unchanged and prints one row per arm.

## Things to know before you read the numbers

- Every retrieval sends `refresh_recall: false`, so the probe does not re-stamp the
  nodes it surfaces (results and ranking are identical). This REQUIRES a server whose
  `engram_query` has the `refresh_recall` field. On an older server every arm reads
  `error: engram_query error: Unknown fields …` in the step rows, by design: the
  probe never retries without the flag, because a silent fallback would contaminate
  the measurement.
- Step files hold ONLY the selected candidate fields (id, type, claim clipped to 160 chars, recall_summary, scores, match type, recall_turn, created_at, rank), never the raw node row.
- Step files hold the thinking-derived query text (up to 6000 chars) on local disk.
  Arm D (`tool_input`) also records up to 2000 chars of the step's TOOL-CALL ARGUMENTS
  as its `query`: a Bash or MCP call can carry tokens or file contents. Step files are
  per-user and local, under `probeDir`; do not share them unreviewed.
- `inContext` counts ids found in the conversation text, EXCLUDING the probe's own
  rating requests (any row containing `[engram-probe · turn `), so an id it listed
  last turn does not read as "already in context" now.
- Turn end waits up to 5 s for the last step's arms. `requests/<turnId>.json`
  records the real cost: `waitedMs`, `pendingAtComplete`, and `pendingTimedOut: true`
  if the cap fired with jobs still pending (their steps are then missing from the
  request). It also records `appended` (whether the request row landed).
- Converter and eval outputs are as sensitive as the step files (`test/*.json` carries
  every arm query, arm D's tool-call arguments included). Use a private `--out`: new
  directories are 0700 and files 0600, and an existing `--out` that is not yours or has
  group/other bits is refused. The mod's own files are written by `$.fs.write(path, text)`,
  which takes no mode in the 2.1.289 types, so their mode is the engine's (umask);
  keep `probeDir` private yourself. `novel_terms.py`'s state file is 0600.
- Step, rating and request files are trusted input only after validation: turn ids,
  indexes, arm names and blind letters are checked before `convert_ratings.py` writes
  anything (they become file names), and a violation exits non-zero naming the file.
- The probe pays for arm C (one small-model call per step). Arm C replies are cached
  per prompt for the module's lifetime only.
- `claude plugin validate` follows `$` only into functions declared in the same file
  as the hook that holds it, never across an import. So `$` never leaves
  `hooks/register.ts`: `makeIo($)` there wraps the calls the probe makes in plain
  functions (an `Io`), and the other modules take that.
- The test kit (`claude plugin test`) has no conversation store: an append made by
  the mod rejects there, and the probe logs each failed append with its row text.
  The tests read the attempts back from that log; they cannot see an append succeed.
- Tests build node-id-shaped strings at run time (`kit.ts` `nid`) because the
  repo's `no-new-shipped-node-ids` gate scans `tools/`.

## Known minor limitation

`novel_terms.py` reads and rewrites `novel-terms-seen.json` without a lock. Two step
jobs that overlap in time (an arm slower than the next step) can both read the same
seen set, so a term may be reported "novel" twice. Not fixed in v0.

## Evaluation modes

`probe_eval.py` scores in two modes. Default (full): every surfaced id is scored;
an id the request never showed counts as neutral. `--shown-only`: each arm's ranking
is first truncated to the ids the request showed for that step/arm. The table's
`unshown` column is the fraction of an arm's ids that sat in the unrated region
(golden rows carry `notes: "unshown"`; relevance stays 1). `convert_ratings.py`
requires `requests/<turnId>.json` for every rated turn.

## Tests

```
claude plugin validate tools/probes/mods-recall
claude plugin test tools/probes/mods-recall
python3 -m pytest tests/test_probe_mods_recall.py
```
