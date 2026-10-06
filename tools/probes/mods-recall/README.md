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
   - `thinking_full` — the last 6000 chars of the step's proxy material (see "Query material")
   - `thinking_tail` — its last 3 sentences (else the last 400 chars)
   - `extract_model` — a small model names 3 noun phrases the proxy material assumes
   - `tool_input` — the step's tool-call arguments (the control: today's PreToolUse channel)
   - `novel_terms` — `novel_terms.py`: high-IDF terms of the proxy material not yet seen this probe session
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
menu or settings `pluginConfigs`): `enabled`, `topK` (1..20), `minQueryChars` (old name `minThinkingChars` still accepted), `querySource`,
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
- Step files hold each arm's query text on local disk (up to 6000 chars for arm A). In
  thinking mode that text is thinking-derived; in `visible` mode (and `auto` steps that
  fell back to it) the A/B queries are built from the step's answer text ONLY, never the
  tool-call arguments. Arm D (`tool_input`) records up to 2000 chars of the TOOL-CALL
  ARGUMENTS as its `query` in every mode, so only arm D's query can carry the tokens or
  file contents the arguments hold. Step files are per-user and local, under `probeDir`; do not
  share them unreviewed.
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
  A completeness-identity failure is only detectable after writing, so it leaves the test
  RRUs, goldens and pairs already written and no `summary.json` (a reused `--out`'s old one is removed first).
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

## Model boundary

Only arm C (`extract_model`) sends text to a side model. Its input is the proxy
material: the thinking text (thinking mode) or the step's answer text (visible mode).
Raw tool-call arguments never reach arm C's side model, and since the proxy material
never contains tool arguments in either mode, that guarantee now holds by construction
(it was already tested; the test was widened: A/B/E now assert the sentinel is absent, and exactly one recall call, D's, carries it). Arm D's query (the tool arguments) goes
to the ENGRAM MCP server the probe discovered by tool-name prefix (the code does not
verify that server is local) and is written to the local step files (the disclosure
above); arms A, B and E now see only the proxy material, whose arm E helper runs as a
local python process on stdin. Step rows record `extractChars`, the length of what arm C
was given (equal to `proxyChars`). Arm C is not called for a blank or short input.

When the side model gives no answer, the arm records `error: extract not answered:
<reason>` and `extract.detail`: the JSON of every field of the reply object except
`text`, cut to 400 chars with a trailing `…` (on a rejected call, `String(err)` cut the
same way). In the 2.1.289 types (`ModelCompleteResult`) the not-answered arms carry
`reason` (`api-error` | `empty-reply` | `aborted`), `usage`, and for `api-error` also
`status` (HTTP status or null) and `error` (a kind such as `overloaded` or
`rate_limit`; never the error's text or body, per the type's own docs). So `detail`
tells a 529 from a refused request; it cannot carry a provider message the API does not
hand over.

## Lineages whose thinking is hidden

Measured live on Claude Code 2.1.289 with claude-fable-5-1: over 5 steps the engine
reported thinkingChars 0, 278, 0, 0, 0 (1-2 thinking blocks each) and the transcript's
own thinking blocks were empty. A thinking-only probe measured nothing there. So the
query source is a choice, `querySource`: `thinking`; `visible`; or `auto` (the default:
thinking when its trimmed length is at least `max(1, minQueryChars)`, else visible).

### Query material: proxy material vs tool arguments

Two materials per step, never mixed:

- **Proxy material** (`proxyChars` on the row): the thinking text when the source is
  `thinking`, else the step's answer text ONLY (no tool-call arguments). The arms that
  stand in for "what the agent was thinking" query it: A `thinking_full` (its last 6000
  chars), B `thinking_tail` (its last 3 sentences), C `extract_model` (a side model's 3
  noun phrases), E `novel_terms` (its high-IDF unseen terms). In thinking mode that is
  the thinking; in visible mode it is the answer text.
- **Tool arguments**: arm D `tool_input` queries the tool calls' arguments JSON (head
  2000 chars) in every mode. It is the control, and it is the only arm that sees them.

Why: with thinking hidden, 185 of 418 steps measured on one seat had no answer text at
all, only tool calls. The old visible material (answer + arguments) then made A, B and D
query the same tool-argument text (on 34 steps A's query was byte-identical to D's), so
one arm was measured three times; and arm C was called with an empty prompt and answered
`api-error` on every one of those steps.

A and B differ in clip only, as before. Each step row records `querySource`,
`visibleChars` (answer + arguments, kept), `proxyChars`, `extractChars` (= `proxyChars`),
`terminal`. `convert_ratings.py` copies `querySource`, `visibleChars` and the arm's
`envelope` into every test RRU's metadata; `probe_eval.py` shows each arm's step counts
by `querySource`, because arms A/B/C/E score over different material per step and
pooling them hides that. In `visible` mode arm D is now independent of A/B/C/E (it is
the only arm over the arguments).

### A step is skipped

- `terminal` (visible source, zero tool calls): see "Terminal steps". No arm runs.
- `short-material`: the proxy material is blank or under `minQueryChars` AND the step
  has no non-blank tool-argument text (nothing for arm D either). No arm runs.
- `no-engram-server`, `probe-inactive`: as before.

A step whose proxy material is short but which has tool arguments is NOT skipped: arm D
runs, and each of A, B, C, E records `skipped: short-proxy-material` (no recall call, no
side-model call, no helper process). Those arm skips count under "Arm runs excluded" in
the eval and are never scored. The rating request shows an arm with no candidates as
`X: -`, as it does for any empty arm; a step with only arm D's candidates still gets
one (an empty list is shown, not hidden).

### Terminal steps

A step is `terminal` iff its source is `visible` and it made zero tool calls. It writes
its row with `terminal: true`, `skipped: terminal`, runs no arm, and adds nothing to the
rating request. Rationale: with thinking hidden, the only material of an answer-only
step is the final answer itself, which is emitted after the moment recall could have
helped; with thinking visible, the thinking precedes the answer, so such a step (thinking
source, zero tool calls) is NOT terminal and still measures something. `terminal` is an
explicit `false` on every row that did not qualify, so the eval can count both.
`convert_ratings.py` summarizes skipped steps as `steps_skipped_by_reason`
(`{reason: count}`, a row with neither arms nor a reason counts as `no-arms`) and
`steps_terminal`, and requires `steps_total == steps_with_arms + sum(steps_skipped_by_reason)`
and `steps_terminal == steps_skipped_by_reason["terminal"]`. The first is a bookkeeping
check that can only fail if the accounting code itself is edited; the terminal
identity is the independent one (it fails on a row that says `terminal` but ran arms,
or the reverse). `probe_eval.py` prints
`steps skipped by reason: terminal N · short-material M · …` after the goldens line, read
from the converter's `summary.json` beside `test/` and `golden/` (`unknown (summary.json absent or unreadable beside the test dir)` if it is missing, malformed, or lacks the key).

## engram_query result envelope

The engram server's MCP text is `{"result": "<server JSON as a string>"}`, for errors
and successes alike. `recall.ts` unwraps exactly one level; each arm records
`envelope: "result-string" | "direct"`, on error rows too once a body has been parsed
(absent only when the call failed before any JSON, e.g. `isError`).

## The rate tool's result contract

The engine validates a plugin-registered tool's `result` at run time: it must be a
string (or an array of content blocks, or undefined). An MCP-shaped
`{content:[...]}` object is rejected (measured live, 2.1.289), so `rate` returns the
plain string `rated N step(s) for turn <id8>`. The ratings file is written before it
returns.

Measured live: a 479-char request appended at turn end (`waitedMs` 18); the ratings
file was written before the result was returned.

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

Save the runner output to a file first, then read the counts and the `(fail)` lines from that file: a count-only pipe loses the failing test's name, which is the only thing a one-in-many failure carries (#3378).
