# Changelog

All notable changes to ENGRAM Alpha are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
with an `-alpha` suffix during the alpha phase.


## [Unreleased]

## [v0.5.2] - 2026-09-25

### rc4 round — 2026-09-30 (fixes, the telemetry ingest, and inert EngramManager units)

**Read these three before upgrading.**

- **`hooks.json` changed: the retired `engram-toolcall-repair.py` hook is
  no longer registered, so restart Claude Code fully after upgrading.**
  Both of its PreToolUse registrations are removed (#3213, closes #172). A
  session reads hook registrations when it starts, so `/reload-plugins` is
  not enough. The rebuild deletes and recreates the plugin tree, so a
  session that is still running keeps the old registration after the
  upgrade. The script is therefore still shipped, as an unregistered no-op
  that reads its input, prints nothing and exits 0 (#3251, closes #3249).
  Without it, that session would run python on a missing file (exit 2),
  which Claude Code treats as a PreToolUse block on every ENGRAM tool
  call. That hazard was reasoned, not observed on a seat. The no-op ships
  for this release only and may be deleted in the first release after
  v0.5.2, because sessions started on v0.5.2-rc4 or later no longer
  register it. The hook had repaired nothing since at least 2026-06-30:
  its own table of protected tools used the pre-plugin `mcp__engram__`
  prefix, and every tool now takes one `payload_json` string, so the fault
  it repaired can no longer occur. `tools/borges_session_eval.py` drops
  its `T2_antml_repair_rate` metric, whose only source logs this hook
  wrote (#3216, part of #3215).
- **The telemetry verdict line is real for the first time.** Until now
  nothing called `engram_telemetry_store.ingest()`, so no seat had a
  `records.db`, and the telemetry line in `engram_diagnose` and the dream
  record (rc3, #3170) could only read `telemetry: not aggregated`. The
  surface daemon now runs the ingest on its indexer tick, in its own
  `try`, so a failed ingest never skips the log indexer. A seat that has
  never written telemetry is skipped quietly, and an Essential-only build
  skips the step (#3226, closes #3135). The first pass also ingests and
  sweeps the backlog of per-process files: on one seat, 9,020 files in
  0.37 s. A surface daemon that is already running keeps the code it
  started with, so restart it after upgrading (on a seat with the systemd
  unit: `systemctl --user restart engram-surface-daemon`). The same PR
  adds a nullable `swept_at` column to an existing `records.db` through an
  idempotent `ALTER`, the round's one on-disk schema change. A
  companion fix keeps a swept file's offset row and marks it swept, so a
  file recreated under the same name is read from the start rather than
  from the old offset, and the line's staleness signal survives the sweep
  (#3228, closes #3227).
- **The EngramManager units in this round are inert: nothing calls
  them.** The owner process gains its socket permission layer: the socket
  moves to `$ENGRAM_HOME/run/engramd.sock` in a 0700 directory with its
  ACLs stripped, peer credentials are checked on every connection, a
  symlink or non-socket at the path is refused, and the request deadline
  covers execution as well as framing. `snapshot()` now stages into a
  private temp file and replaces the destination atomically, so an
  aborted snapshot never damages an existing file (#3201, closes #3194).
  It also gains its two connections: an owner-internal one for
  `snapshot()`, and a permanently restrictive client-read one behind an
  authorizer floor with size and instruction limits (#3221, closes
  #3195). No code outside `engramd_owner.py` and `engramd_client.py`
  imports either module, and the owner still serves no domain
  operations. Linux only for now: on another platform, or without
  `setfacl`, the owner refuses to start.

**Fixes and changes.**

- **A snapshot read-mark failure is now visible instead of swallowed.**
  `engram_core`'s per-turn snapshot capture still runs when its WAL
  read-mark fails, but the result says so (`read_mark` is `held`,
  `failed` or `inherited`, with the error), and a failed release is
  reported and rolled back. The same PR adds a missing module-level
  `import sys` that had turned two error handlers into `NameError`
  crashes: the snapshot worker's unavailable-fallback, and the NLI
  classifier's model-load failure (#3224, closes #3222).
- **`cohort_dispatch validate` now checks that every node sent comes
  back.** It checked the quality of each returned item but never compared
  the returned ids with the payload, so a fairy that dropped one node of
  fifteen passed with `failures: 0`. A dropped id is now a failure and
  flows into the retry payload; an id returned but never sent is refused
  and validate exits 1; a duplicate is retried (#3235, closes #3045).
  This was the first fix built under the contract-first convention.
- **The session-start hook's total injection is now capped.** It had no
  total cap and injected 53,558 characters on one seat. Its output now
  goes through `emit_context()` under the claude-code budget of 9,500
  characters. Blocks that don't fit go to an overflow file named on the
  first line, the must-not-miss lines stay within the first 1,800
  characters, and the focus block is cut at whole entries with a marker
  (#3214, closes #127). The full tool-capability index is now larger than
  the whole budget, so it always goes to the overflow file; a names-only
  tool list of about 950 characters, derived at runtime from the same
  index, is shown inline instead (#3218, closes #3217).
- **`forum reply` accepts the body as a positional argument.** Its
  argument errors no longer echo the message text, which under `| tail`
  read like a send confirmation, and stdin is read only when it is a real
  redirect, so a socket on stdin can no longer hang the command (#3234,
  closes #2906).
- **`baton flip` to a non-participant now prints the fix:** the exact
  `baton add-participant` command to run. The refusal itself is unchanged
  (#3236, closes #2901).
- **Batons now carry an explicit `phase:` that every transition writes,
  so whether a baton is free to claim no longer depends on this seat's
  `primary_user`.** `baton claim` asks `_resolve_phase()`: an explicit
  `phase:` wins, and without one the old comparison (renamed
  `_is_legacy_pool_sentinel()`) is the fallback and says so on stderr
  (#3243). Every transition now writes its phase: `init` writes `fairy`
  on a PR baton and `pool` on a bunch, the flip to the colleague writes
  `peer`, the flip to the merge seat writes `merge`, `claim` writes
  `claimed`, `release` writes `pool`, and `release --done` writes `done`.
  When a transition meets a baton that has no `phase:` yet, it first logs
  a `(legacy conversion)` line naming the phase the baton was in, then the
  transition, so the change is readable in the baton's own turn log. Other
  writes convert such a baton only when the old resolver can name its
  phase. `close`, `merge` and
  `reopen` write no phase; terminal states stay in `status` (#3254). Two
  visible changes: `baton claim` refuses a PR baton sitting at the pool
  marker, because that baton is in its merge phase (#3243); and a second
  claim on a claimed bunch baton is refused because it reads `claimed`.
  Both are part of #3039. The design note is
  `docs/design/3039-baton-phase-turn.md` (#3055).
- **The recall_summary authoring target is one number, 140, defined
  once** as `RECALL_SUMMARY_TARGET` in
  `tools/recall_summary_validator.py`. A second, never-ruled "≤120" limit
  sat in eight places, including the fairy prompt and the validator's
  message. The 200 and 280 gates are unchanged (#3191, closes #2736).
- **The agent allowlist names all six shipped `engram-*` agents,
  including `engram-dream-master`,** in the CLAUDE.md template, both boot
  charges and the instruction-manifestation policy; a test binds the list
  to the shipped agent specs. Deployed boot charges change only on
  redeploy (#3220, closes #3219).
- **Under `comms_surface: umwelt`, the Claude boot charges keep both
  forum monitors armed as a safety net,** with the umwelt bell as
  primary, until the forum-to-world bridge is live. Deployed copies change
  only on redeploy (#3190).
- **The CLAUDE.md template's write discipline now says to err on the side
  of writing more,** and that the stop hook's Write Check reminder is
  muted by design once the graph is mature. An existing seat's CLAUDE.md
  is not changed (#3245, part of #3238).
- **The session-start hook no longer raises a false `MCP server: OFFLINE`
  on startup.** At a startup or resume, the MCP server is launched beside
  the hook, and a single `pgrep` could run before the process was visible.
  On one seat this fired on 12 of 12 automated boots after rc2. Now a miss
  at startup or resume prints an informational `MCP liveness: unknown …
  will confirm on first prompt`, with no `⚠️` header. A miss at a compact
  or clear start still raises the hard alarm, and a probe that errors
  names its cause (#3250, closes #3202). A follow-up test pins that any
  unrecognised session source also gets the soft line (#3256, part of
  #3252).
- **viz: `GET /api/version` reports which commit the running `:5001`
  server loaded,** captured once at startup (sha, branch, dirty,
  started_at). It adds a warning off `dev`/`main` or on a dirty tree, and
  with no git the server still starts and the endpoint returns nulls with
  a reason (#3174, part of #2531).
- **New `tools/monitor-supervisor.py` runs the two forum monitors and the
  umwelt bell as children under one Monitor,** so a seat re-arms one
  watch instead of three. It refuses to reap an earlier instance whose
  identity it cannot confirm. Nothing arms it yet (#3177, part of #2982).
- **macOS wake (repo-side only):** a throwaway credential probe for
  checking, once, whether a LaunchDaemon can read the Claude Code Keychain
  entry, and a fail-loud credential check as the first action of the
  wake launch script, so a credential failure is logged instead of looking
  like a quiet skip. The same PR fixes a `PATH` ordering bug in that
  script found by its tests (#3246, part of #3002).
- **WS4 adjudication kit (repo-only, in no install tier):**
  `extract_spans.py` adjudicates at most 10 fires per seat per UTC day,
  picked by a seeded hash that never sees the arm (#3181), and
  `analyze.py` reports each seat's sampled fraction and per-lesson
  composition (#3182); part of #2753.

**Docs, CI and tests.**

- `CLAUDE.md`: the contract-first tests convention is adopted, and
  `docs/PR-MERGE-POLICY.md` puts the whole baton system, design included,
  on the team-merge side (#3241). The macOS CI runner is removed and
  macOS coverage comes from the Mac seats (#3192, #3193). Check 3 names
  soma alongside umwelt as exposed to commit-message auto-close (#3189,
  closes #2515 and #2514). The attribution section gains its
  duplicated-by-hand footer (#3206, closes #2707).
- Cross-repo fairy dispatch: the parent provisions the worktree and
  passes it by path (#3212).
- `docs/RELEASING.md`: reconciling `main` into `dev` is now a standing,
  command-checked step before every cut, not only a fix for a `DIRTY`
  release PR (#3258, closes #3116).
- #2652 design docs: the owner is one process that may hold two
  connections (#3198, closes #2840); the deadline covers execution
  (#3200); the socket path and the inherited-ACL gap (#3211). Three
  comments in `engramd_owner.py` name the right U3 identifiers (#3239).
- Cascade docstrings name `CASCADE_RELATIONS` instead of "just
  derives_from" (#3233, closes #3229).
- Chinese: `docs/ZH-TERMINOLOGY.md` records the terminology ruling
  (#3207), and the `README.zh.md` title renders "epistemic" as 认知
  (#3203).
- Tests: `fresh_module`'s `sys.modules` purge is restored at the end of
  each test file, which turns dev's full suite green again after #3226
  (#3230, closes #3225); a `tools` package cached from the wrong path is
  purged after `sys.path` is restored (#3205, closes #3204);
  `test_all_reasoning_types_accepted` covers `inductive_enumeration` and
  now fails if its list drifts from `REASONING_TYPES` (#3231, closes
  #3081); the tripwire ceiling test is tightened now that #2893 is fixed
  (#3185).

### rc3 round — 2026-09-27 (fixes plus new capabilities)

- **A tool call can no longer report success on a live connection whose
  `-wal`/`-shm` sidecar files have been silently displaced.** A new
  predicate is checked, unthrottled, on every `_get_db()` dispatch; when
  displacement is confirmed the call fails loudly and names the remedy
  instead of returning `created` on a corrupted connection. The existing
  marker/forensic-capture/dump path still runs first, unchanged; Darwin
  (no `/proc`) is treated as healthy and gets no protection from this unit
  (#3158, part of #2635).
- **A `"superseded"` cascade resolution could silently carry a spurious
  `replacement_absent_because`/`replacement_absent_note`, in violation of
  the function's own documented contract.** The guard only ever checked
  those two fields against `resolution_kind == "acknowledged"`; a
  separate, `"superseded"`-scoped check now refuses them independently,
  while the existing `replacement_id` requirement for `"superseded"` is
  left untouched (#3130, closes #3125).
- **The session-log indexer no longer reports every unflushed-append NUL
  run as "malformed JSON", and the emitter now fsyncs each event so
  fewer of those NUL runs happen at all.** The reader half adds a
  NUL-aware guard plus an incomplete-tail rule: a terminated line with
  leading/trailing NULs is stripped and recovered with a single
  "unflushed append" warning, and an unterminated final line is indexed
  if it already parses as a complete record (a writer that died after
  the record landed but before its newline did — otherwise the size gate
  would freeze that file and lose the record for good) and is otherwise
  treated as the live tail of an in-progress write. The writer half closes the
  other side of the same window — each event is flushed and `fsync`'d
  before `emit()` returns (about 1.16ms per event on ext4), with a new
  file's directory entry also made durable, and a failed fsync degrades
  to a stderr warning rather than a dropped event (#3161, part of #2801;
  #3168, closes #2801).
- **The installer now shares the engine's single bounded offline
  deploy-tag refresh, so a failed fetch still falls through to local
  tracked ancestry instead of refusing outright.** It also owns and
  terminates the fetch's private process group, validates timeout
  inputs, and warns on every failed refresh while keeping the
  distinction between cached ancestry and confirmed remote freshness
  explicit; private/unmerged tagged heads are still refused (#3153,
  closes #3133).
- **New MCP tool `engram_retype_edge`: corrects a provably-wrong
  `supported_by`/`derives_from`/`cites` edge by making it weaker, never
  stronger, in one audited transaction.** Allowed moves come from a
  fixed table; the call refuses on a pending cascade marker, an
  already-existing target relation, or a `derives_from` retype that
  would strip a derivation's last remaining cascade premise, and it
  recomputes the source's confidence and its full downstream cascade
  when the old relation was cascade-bearing. A fast-follow (#3167) fixed
  a test fixture whose seeding order let an unrelated `temporal_causal`
  guard mask what `test_stronger_move_refused` was meant to isolate
  (#3160, closes #2265).
- **The viz "Snapshot now" button is gone; every page load now computes
  a live, uncached, never-persisted health score instead.** A new `GET
  /api/health/v2/live?agent=<a>` route opens the requested agent's DB
  read-only and never writes anything; concurrent requests for one
  agent collapse into a single build; two new optional per-agent
  overrides close a real multi-tenant bug where a shared viz process
  would otherwise silently score using the server's own process-global
  turn/embedder facts (#3155, part of #2518).
- **A new runtime verdicts report classifies every tripwire site's
  recent window into one of the design doc's eight VERDICTS lines, and
  a new static check catches a typo'd `reached` literal at CI time
  instead of it silently reading "NOT REACHED" forever.** The dispatch
  map that drives it is now a committed, generated source file rather
  than a runtime tree-walk, after a design-conformance read found the
  original walk couldn't work against the flattened, `tiers.json`-less
  layout of an installed plugin; a drift gate keeps the committed map
  honest against the real tree (#3156, closes #3136).
- **The WS7 verdicts now surface as one telemetry line where people
  already look: `engram_diagnose`'s output and the nightly dream
  record.** A single renderer produces it (the dream record copies the
  diagnose field verbatim rather than rendering its own), and "we could
  not check" never renders as healthy: a missing store or table, a
  missing or unparseable ingest timestamp, an ingest more than two days
  old, or zero rows for the window all read `telemetry: not aggregated
  (...)`, never a line of zeros. Labels are the stored verdict values;
  a test pins their set to WS7's own verdict table, and a verdict
  outside it still renders rather than disappearing (#3170, closes
  #3137).
- **`forum read` gains `--new` (posts since your read watermark) and
  `--last N`, and `GET /api/thread/<id>?agent=<a>` returns the caller's
  own `last_read_post_id`.** Against a server without the field,
  `--new` falls back to the last five posts and says so; the read
  watermark advances the same way in every display mode. The server
  half is live once the forum is redeployed; the CLI flags reach a seat
  with its plugin rebuild (#3171, closes #3138).
- **A per-fire ledger and a config-gated A/B of the tripwire `[triggered
  principles]` chain render give the pre-registered experiment its
  denominator.** Off by default; when active, each fire with a
  `generalizes` head is assigned an arm from `sha256(seed:nonce)` and
  logged with its shown/cut lesson ids and render sizes, so every
  assignment is reconstructable before any real run begins (#3166, part
  of #2753).
- **A new tool mechanically BFS-walks a package's call graph from
  declared root entry points and records every recognized database
  write site reached, at the file:line level rather than the (verb,
  table) level** — closing the gap where a union over pairs can hide
  one specific site going unreached. Proven against small synthetic
  fixtures only; it does not yet run against the real `src/engram` tree
  (#3134, closes #3095).
- **The EngramManager owner-process skeleton: the one process that will
  hold `knowledge.db` open, speaking U1's existing AF_UNIX wire
  contract.** Handles the handshake/version check, `ping`, and a
  quiesced `snapshot()` over SQLite's backup API, with a
  confirmably-dead-PID stale-socket recovery, an end-to-end
  per-request deadline measured from accept, and thread-per-connection
  concurrency so one slow client can't block another. No domain
  operations yet, and nothing calls it (#3151, closes #3096).
- **A macOS `launchd` LaunchAgent ports the existing Linux
  systemd-timer autostart mechanism to single-user Mac seats**,
  periodically checking for a live session and starting one via the
  same charge-file invocation if none exists; the wake announces
  itself to the forum before anything else but does not auto-resume
  stale loop state. The live-fire install step and on-seat dry run are
  still blocked on an unrelated seat issue (#3162, part of #3002).
- **Design note settling the edge-correction contract for
  `engram_retype_edge` (#2265) and the remainder of #2347, for Lei's
  read.** Proposes the weaker-only retype move table, closes #2347 as
  solved while keeping the per-node acknowledge guard, and narrows the
  Stage-3 exit query to `temporal_causal: True` relations only. Docs
  only; this is the design #3160 implements (#3093, part of #2252).
- **`docs/PR-MERGE-POLICY.md` gains Lei's 2026-09-27 delegation: the
  team may merge a substrate PR without his read when it implements a
  design he already read, provided the merge seat links that read and
  writes a short conformance check**, and an issue may be closed with
  code evidence attached. A new substrate concept still goes to him
  first (#3157).
- **The three-pre-peer-fairy-round cap ruled on the forum on
  2026-09-20 is now written into `CLAUDE.md`'s colleague-converged
  gate**, including its stopping signal, the written-reason exception
  for a fourth round, and the contributor-commit reset rule read off
  `git log`. Closes the gap where the cap was load-bearing at the merge
  seat but had no single-source-of-truth home in the repo (#3146).
- **A cancelled or interrupted live-health build could leave every
  waiting thread reading `None` instead of a payload, crashing one
  level up.** The per-agent single-flight guard in
  `get_health_v2_live_data()` only ever caught `Exception`; the
  `finally` block now backfills a fail-honest placeholder result before
  firing the event if a `BaseException` escaped the build (#3163, part
  of #2518).
- **A release build could ship files the minus-set's own header said it
  excludes — including 45 files under `src/forum/tests/` present in
  every public release since at least v0.3.1.** `_apply_minus_set` now
  implements the documented gitignore-style rules: a directory entry
  matches at any depth unless anchored with a leading `/`, `*` never
  crosses `/`, and a matched symlink is removed as a link rather than
  followed. The `deploy/` entry becomes `/deploy/` so the new any-depth
  semantics don't also swallow the shipped `src/forum/deploy/` install
  path (#3164, closes #2516).
- **Loading the viz dashboard no longer ships every field of every node
  over the wire.** `/api/graph` now serves index nodes only (the 13
  fields the page reads outside the detail panel — about 7x less on the
  wire on the author's own graph), and a new `GET
  /api/node?id=&agent=` fetches one full node on click, 404ing with an
  empty body on an unknown id and 400 on a missing one; `get_graph`
  itself is unchanged (#3165, part of #2866).
- **A failed WS4 arm computation or ledger-row build used to vanish
  silently instead of showing up as a fire.** Each now gets its own
  narrow guard: an arm-computation failure still renders the block the
  way dev does today but logs the row with `arm: "error"` plus the
  error text, and a row-construction failure still writes a minimal row
  so the fire is counted; the nudge, exit code and deny path are all
  untouched. Folds in the round-2 review suggestions from #3166, which
  merged before they landed (#3173, part of #2753).
- **A new offline adjudication kit lets a human score the
  pre-registered chain-render A/B without seeing which arm a fire
  belongs to.** `tools/ws4/extract_spans.py` cuts one blinded
  heeding-window span per fire from the fire ledger and transcript,
  redacting the `[triggered principles]` block and flagging any span
  that still names a head after redaction; `tools/ws4/analyze.py`
  computes O1-at-K via a `corrective_at` verdict field, O2 raw/transfer
  counts, and per-arm privacy-drop counts. Read-only on the ledger,
  transcripts and `knowledge.db`; ships in no install tier (#3175, part
  of #2753).
- **The WS4 adjudication report now counts every fire-ledger row,
  including the `error` and `na` rows it used to skip without a trace.**
  `compute_denominators()` in `tools/ws4/analyze.py` gains
  `ledger_rows_by_arm` (per seat and pooled; `chain`, `control`, `na`
  and `error` always present, 0 when absent) and an `error_rows` count
  with a per-error breakdown, and the report always prints its
  `error rows` line, including when the count is 0. The
  `extract_spans.py` selection and every existing O1, O2 and
  length-control number are unchanged (#3179, part of #2753).
- **`engram_retype_edge` was missing from the two hand-kept registries
  that must list every write-capable tool, which turned the rc3
  release-gate full suite red.** The Stop hook's `_ENGRAM_WRITE_VERBS`
  now includes `retype_edge`, so a turn whose only ENGRAM write is a
  retype counts as a write for the write check. The checked-in
  `tool_capability_index.txt` is regenerated with the new tool's entry.
  Mechanical sync of #3160; no behaviour change beyond those two
  lists (#3183, part of #2265).

### rc2 fix round — 2026-09-26 (found on the rc1 soak)

- **Forum prompt-hook `Connection: close` crash fixed — the reason for
  this re-cut.** rc1's forum server answers with `Connection: close`,
  which nulls `conn.sock` as soon as `getresponse()` returns — the
  connection has merely dropped its own reference; the socket itself
  stays open, owned by the response, with real reads still ahead of it;
  the end-to-end deadline code added by #3110 called
  `conn.sock.settimeout(...)` unconditionally on every read-loop
  iteration, raised `AttributeError` on that path, and a bare `except`
  swallowed it — so the hook silently fell back to stale cached
  threads/mentions on every single prompt. The fix now captures the
  socket reference before `getresponse()` and re-arms that captured
  reference's timeout on every read iteration (catching `OSError` for
  the case where the fd has since genuinely closed), so the
  remaining-budget deadline still holds on a `Connection: close`
  response (#3144, closes #3143).
- **`engram-sleep`'s cohort-boundary snippet reads `completed_at`, not
  the never-written `cohort_end_at` key.** The `KeyError` was swallowed
  by a `2>/dev/null || echo ""` fallback, silently collapsing the cohort
  boundary to empty and making the dream-master's cohort filter match
  every node ever created; a marker missing the key now fails loudly
  instead (#3132, closes #3123).
- **The install-script untagged guard now decides on confirmed
  ancestry, not raw tag presence.** A local tag on an unpushed/unmerged
  commit passed the untagged guard even though the separate ancestry
  check had already failed, disagreeing with the engine's own `cli run
  upgrade` path on the identical case; both now refuse consistently
  (#3131, closes #3103).
- **Health-series declarations gate no longer false-positives on a
  layout-dependent module prefix.** Two declarations rows seeded from a
  `src/`-layout checkout carried `engram.health.` instead of the
  deployed plugin's `health.` prefix, so the affected seat's very first
  snapshot ingest raised `SeriesIntegrityError`; both serialization and
  the ingest-time comparison now canonicalize the prefix, and a
  migration heals stored rows (#3127, closes #3122).
- **`_count_null_embeddings` no longer requires `row_factory =
  sqlite3.Row`.** The health-engine helper indexed its query result by
  column name, which only works when the caller sets `row_factory`; the
  dream-master's own connection doesn't, so the only real caller hit a
  `TypeError` in production the test suite's connection shape never
  exercised (#3126, closes #3124).
- **Forum monitors now announce during their seed loop, not just their
  poll loop.** #3065's edge-triggered outage announce lived only in the
  poll loop, reached only after a seed loop that blocks silently until
  the first successful poll — so a monitor armed while the forum is down
  (the routine case at session start) was completely mute. Both seed
  loops now announce the first failed attempt and the eventual success
  (#3111, closes #3067).
- **EngramManager U1: the owner wire-contract client library.** A pure
  client speaking the new AF_UNIX wire protocol for the future `engramd`
  owner process, with three result states (`Ok`/`Unavailable`/
  `Rejected`) distinct at the type level, and a `request_id` threaded
  through every write so a caller that got `Unavailable` can retry
  safely. Nothing calls it yet (#3102, closes #3094).
- **Retraction replacement contract implemented.** Every
  author-initiated retraction now records a replacement decision — the
  node that replaces the claim, or one of four stated reasons why
  nothing does — through the single chokepoint both the organic retract
  path and cascade-resolve delegate to; also fixes a bare `except
  IntegrityError: pass` that let a retraction commit while its
  replacement edge was never written (#3090, implements #2613).
- **`test-id-set-check` no longer re-demands markers already carried by
  merged dev PRs.** The release-PR gate re-demanded a `Removed-test:`
  marker for every test id that disappeared anywhere in the dev range,
  even when the deleting dev PR already carried its own marker; the gate
  now unions a third marker source, the already-merged PR's own body
  (#3139, closes #3117).
- **A CI gate now catches a stubbed test that a stale signature change
  breaks silently.** #3110 changed two function signatures and moved a
  call off `urlopen`; two test files that monkeypatch those names by
  bare name were invisible to the existing affected-tests heuristic. A
  new blocking CI step finds every signature that genuinely changed in
  a PR's diff and greps `tests/` for bare-name references (#3129, closes
  #3118).
- **Lane prioritization rubric v0.** `docs/LANE-PRIORITIZATION.md`, the
  commander's ranking policy for open engram-alpha/umwelt issues into
  seat lanes: five 0-2 axes, four standing overrides, a day-end re-rank
  rule, and a changelog section for revising the rubric with history.
  Docs only (#3140).
- **EngramManager security-slice design: read-template registry + socket
  permission model.** Settles the #2652 security slice's four open
  questions against measurement: `SO_PEERCRED` doesn't exist on Darwin,
  a mismatched socket-option level silently returns uid 0, and a
  read-only SQLite handle permits `ATTACH`/`PRAGMA`. Proposes a
  two-layer floor — SQLite's own `set_authorizer` plus a build-time-
  frozen template registry. Design only (#3100, part of #3097); a
  follow-up (#3145) withdraws two overclaims flagged at approval — the
  token-match fallback can under-deny, and `SQLITE_LIMIT_LENGTH` caps a
  single string/blob/row, not aggregate memory.

### Added

- **`deductive_conjunction` reasoning type** (#3082, closes #3080) — a new
  reasoning type for claims that are the logical conjunction of their
  premises (e.g. "none of the N surveyed systems has feature X" over an
  explicitly enumerated, complete set). Confidence is weakest-link
  (`min(premise confidences) × 0.98`), matching the existing deductive
  types but without their 2-premise cap.
- **Per-pack share-clearance label on the forum pack registry** (#3036,
  part of #676) — four new columns on `packs` distinguishing packs that
  have been read in full and cleared for eventual external sharing from
  packs that haven't (default `private` for every existing and new pack).
  A deliberately narrow slice following a live pack-privacy incident;
  the full node-level visibility system is tracked separately.
- **`engram_list` gains a `sort_order` parameter** (#3025, closes #3016) —
  `sort_by` previously baked column and direction into one fixed string,
  so callers could never ask for oldest-first ordering. The optional
  `sort_order` (`"asc"`/`"desc"`) now overrides direction independently of
  column; omitted, behavior is unchanged. Fixes the engram-sleep Step 5
  backfill, which needs oldest-first and had no way to request it.

### Changed

- **Board API reconciliation skips already-file-done records** (#3060,
  closes #2007) — `/api/board/projects` and `/api/board/updates` were
  taking 300-500s because every request re-resolved GitHub state for up
  to 1038 PR refs serially, even for records whose file status already
  reads `merged`/`cancelled` (where that resolution is provably
  unused). Skipping those cuts the working set to ~30 refs, a ~34x
  reduction, with no change to any served status.

### Fixed

- **Monitor timeout budgets are now end-to-end, not per-op**
  (#3110, closes #3106, closes #3107) — `forum-mention-monitor.sh` had no
  `--max-time` at all, and the forum prompt-hook's worst-case timeout
  arithmetic assumed a call with `timeout=T` takes at most T, which is
  false against a server that trickles data (the per-op timeout re-arms
  on every partial read). Both now enforce a genuine wall-clock deadline.
  Its `Connection: close` crash — the reason for the rc2 re-cut — is
  fixed in the rc2 fix round above (#3144).
- **Health-exam liveness checks could not report failure** (#3070) — the
  agent-facing health-exam skill's `pgrep`-based checks counted other
  seats' processes, could never detect a dead daemon on Darwin, silently
  swallowed an absent-`pgrep` error as a false clean, and a zsh `$PATH`
  clobber in Step 0 turned that false clean into a false alarm on a
  healthy system. All three liveness sites fixed, plus a gate across all
  29 shipped skill docs, with its known limits listed on the PR.
- **One shared deploy-tag predicate, enforced by the engine too** (#3099,
  closes #3092) — the install script already refused to build from an
  untagged commit unless `--allow-untagged` was passed, but the engine's own
  `upgrade`/`build` entry points never enforced this, so an untagged
  `dev` tip could upgrade through the engine while the shell script would
  have refused it. The engine paths now use the shared Python predicate;
  the shell installer's own untagged check still reads raw tags at HEAD,
  tracked in #3103.
- **WS3 hook-injection gate is now live, not `xfail`** (#3098, closes
  #2863) — the test asserting every hook's `additionalContext` routes
  through the shared `emit_context()` helper was marked
  `xfail(strict=True)` pending five known exceptions; four are confirmed
  deliberate (atomic fixed-format emitters); the fifth, the session-start
  hook, is excluded with its uncapped total emission disclosed and tracked
  separately in #127; the gate is now enforced rather than merely tracked.
- **`engram-sleep` stops live Monitors unconditionally in Step 3.5**
  (#3050, closes #3049) — Step 3.5 only tore down live Monitors
  (Umwelt bell, forum-mention, forum-updates) when a loop-mode marker
  file was present, so a session that armed Monitors ad hoc (without the
  marker) left them running through the sleep cycle and into the next
  session.
- **Racy wait predicate in the embedding-restore C5 tests fixed** (#3088,
  closes #3087) — a test-only race in the async regeneration-daemon wait
  helper, not a production defect; fixes the alpha nightly CI failure.
- **Cross-process single-flight lease for embedding restore regeneration**
  (#3078, closes #3077) — every process hitting missing embeddings on
  startup independently spawned its own regeneration daemon thread;
  harmless but wasteful under concurrency. A lease now coordinates
  regeneration across concurrent MCP sessions / short-lived CLIs.
- **Restore-completeness contract for the embedding pipeline, C1-C5**
  (#3076, closes #3075) — fixes three real problems surfaced when a
  user's agent restored from a backup snapshot: embedding-text generation
  depending on caller row-factory state, an implicit regeneration
  outcome, and other completeness gaps in the restore path, written to a
  contract-first test suite before the fix.
- **PR-baton auto-close gate now matches by suffix, not prefix** (#3061,
  closes #2010) — both baton auto-close gates required a baton's
  `project_id` to *start with* `PR-`, but the documented cross-repo
  naming convention names cross-repo batons `<REPO>-PR-<N>`, so every
  correctly-named cross-repo baton was invisible to both auto-close
  mechanisms. Measured: 33 of 99 active batons followed the convention
  and none matched either gate.
- **Forum monitors announce the edge into and out of an outage** (#3065,
  closes #3062) — previously an outage and a quiet-but-reachable forum
  produced identical (silent) output, so a multi-hour blackout was
  indistinguishable from a calm night. Both forum monitors now announce
  the transition, once each way, instead of failing in total silence.
- **Linux boot-charge stdout-target check counts lines, not the loop's
  exit status** (#3059, closes #3057) — `readlink /proc/$pid/fd/1` on a
  pid whose target is unreadable produces empty output with no error, so
  a bare loop over monitor pids silently dropped that pid's line instead
  of reporting it, and the "every line must start `socket:`" check then
  passed against zero lines.
- **`baton init` records the invoker as author, not the initial turn
  holder** (#3054, closes #3042) — with `--turn` naming someone else,
  authorship was durably misattributed to the turn holder, and the real
  creator couldn't have the baton flipped back without `--force`.
- **`baton claim`'s refusal no longer asserts a person holds a pool
  baton** (#3040, part of #3039) — the refusal message resolves "is this
  a pool sentinel" from the invoking seat's own `primary_user`, checked
  against a `turn` written into a shared store; a pool baton parked by a
  seat with a different `primary_user` produced a refusal claiming a
  named colleague was holding it, when no one was.
- **`pgrep` on BSD/Darwin no longer silently no-ops on GNU regex escapes**
  (#3035, closes #3032) — BSD `pgrep -f` compiles its pattern as a POSIX
  ERE, where `\s`/`\S` aren't special, so a Darwin boot-charge
  monitor-verify check using GNU escapes matched nothing and exited 0 —
  indistinguishable from "checked, both monitors fine." Fixed in the
  charge text and the test harness that should have caught it.
- **Nap advisory note wording now tracks the #3014 checkpoint anchor**
  (#3031, closes #3030) — the warm-briefing staleness note still read
  "Step 5b not performed this session" after #3014 moved the advisory's
  anchor from session-start to last-checkpoint, which was false on
  exactly the cases #3014 exists to catch.

### Removed

- **Deleted the unarmed `collab-baton-monitor.sh`** (#3072, part of
  #2857) — no skill, hook, agent spec, or template invoked it; the live
  baton wake path runs through `forum-updates-monitor.sh --kinds
  dm,baton` instead. Removed alongside its dedicated test file and its
  manifest entries.

### Docs

- **EngramManager design doc** (#2828, part of #2652) — design document
  for the EngramManager substrate, the layer that will own `knowledge.db`
  behind a socket contract. Design only; no code.
- **EngramManager design §5.1 — 'bounded' means one end-to-end deadline**
  (#3108, part of #2652) — states that a "bounded" retry window must mean
  one deadline per request, end to end, on both sides of the socket, not
  a timeout re-armed per socket operation.
- **EngramManager design §5.5 — degraded-mode ruling recorded** (#3089,
  part of #2652) — records the ruling that there is no degraded mode of
  either kind: a hard stop for writes, no stale reads served, and a loud
  unavailability signal with two-tier recovery guidance.
- **Retraction replacement contract, design draft 4** (#2837, closes
  #2613) — design contract requiring every author-initiated retraction to
  record a replacement decision (the node that replaces the claim, or a
  stated reason nothing does). Design only; implementation tracked
  separately.
- **Contract-first tests: write the assertions before the implementation**
  (#3046, part of #3045) — for a function with a stateable contract,
  write the tests asserting its designed behaviours (including error
  handling) before implementing it, so drift from the design is caught
  mechanically. Documents the discipline motivated by a silent-node-drop
  bug.
- **Falsifier-log figure re-measured; detective-channel caveat folded
  into Confounds** (#3034, closes #3033) — re-measures a stale per-seat
  cache figure and folds a caveat about detective-channel bias into the
  log's Confounds section. Docs only.
- **Boot-charge doc: the Darwin `/proc` form fails silently, not
  outright** (#3056) — corrects a self-contradicting sentence claiming
  the Linux `readlink /proc/$pid/fd/1` check "fails outright" on Darwin;
  it actually fails silently, the same accepted-and-wrong shape the
  paragraph already described one line later.
- **`auto-reviewer-fairy-judgement` skill clarified: doesn't waive the
  mandatory review round** (#3026) — the skill's "do the review
  directly" branch was being misread as licensing a same-context
  self-review in place of the mandatory pre-peer reviewer-fairy round;
  it only ever governed whether spawning a fairy is worth the dispatch
  overhead.

## [v0.5.1] - 2026-09-14

### rc5 fix round — 2026-09-19 (found on the rc4 soak; tracking #2941)

- **`engram_nap` renamed to `engram_checkpoint`; `engram_nap` kept as a
  deprecated alias for one release.** The tool name collided with the
  `engram-nap` SKILL name (the eight-step nap routine, of which calling the
  tool is only one step), and drowsiness/nap-warning nudges that said "Run
  engram_nap" read ambiguously as either "call the tool" or "run the skill"
  — an agent that took it literally called only the tool and skipped the
  skill's other steps (notably Step 5b, the warm-briefing rotation). The two
  hook nudges (`context_tracker.py`, `engram-surface-hook.py`) now say to
  load and run the `engram-nap` skill instead of naming the tool directly.
  `engram_nap` delegates to `engram_checkpoint` with identical behavior and
  output shape (#3018, closes #3015).
- **Nap advisory re-anchored on the last checkpoint, not session start.**
  `written_this_session` (renamed `written_since_last_nap`) satisfied itself
  on one early write of `warm-briefing.md` and stayed silent for the rest of
  a long session — across many subsequent naps and a compaction where Step
  5b was actually skipped, measured live on two seats (a section stale by 4
  naps / 10 hours, and a post-compaction section, both read as non-stale).
  Every nap/sleep already appends a timestamped header to `session_log.md`;
  the advisory now compares against the most recent one instead of the
  per-session marker, re-arming after every nap and naturally covering the
  compaction case (#3017, closes #3014).
- **Nap advisory's CONFIRMED-stale note text updated to match the #3014
  anchor.** #3014 moved the anchor from session-start to last-checkpoint
  and renamed the field (`written_this_session` → `written_since_last_nap`),
  but left the emitted note string reading "Step 5b not performed this
  session" — false on exactly the cases #3014 newly catches (written since
  session start, but before the last checkpoint). Now reads "Step 5b not
  performed since the last checkpoint," true under both anchors (closes
  #3030).
- **Boot charge's monitor-liveness check reads the monitor's stdout target,
  not just process presence.** `pgrep` certifies a monitor process alive
  even when its stdout was armed to a dead end (a detached `nohup … > file`
  instead of through the Monitor tool) — measured live: nine real wake
  events detected, none reached the session, silently, for six hours. The
  charge now reads `readlink /proc/<pid>/fd/1` (Linux, a `socket:` target is
  healthy) or `lsof -a -p <pid> -d 1`'s TYPE column (Darwin, no `/proc`)
  (#3008, closes #3007).
- **Dream-record filename pinned to the NYC-local write date.**
  `~/.engram/history/dream/YYYY-MM-DD.md` never specified whether its date
  is the day the record was written or the day it consolidates; the
  sibling dream-report filename in the same skill was already pinned to
  NYC-local, making this an oversight rather than deliberate latitude
  (#3010, closes #2177).

### rc4 fix round — 2026-09-18 (found on the rc3 soak and the 09-18 nightly; tracking #2941)

- **macOS install unblocked.** `install-local-marketplace.sh` carried a heredoc
  apostrophe that Apple's bash 3.2 mis-parses, so a fresh rc2/rc3 install on a
  Mac whose PATH resolves `bash` to `/bin/bash` failed; fixed with a regression
  test and a `macos-bash32-syntax` CI job that runs the installer under bash 3.2
  syntax rules (#3000, closes #2979).
- **Nap advisory for a skipped warm-briefing rotation.** `engram_nap` returns a
  `warm_briefing` block stating whether the "From this session" section moved
  since the session started (optional `session_id`, `session_started_at` as the
  comparison anchor), so a nap whose Step 5b rotation did not run is visible in
  the return the agent already reads instead of only in the file's mtime
  (#2998, closes #2997).
- **Nightly no longer fails on its own fix.** The #2989 erase-safety control
  read its "pre-fix template" from the live `dev` ref, so it was correct while
  the PR was open and stale the moment it merged; the control is now a frozen
  literal fixture (#2996, closes #2995).
- **`persistent: true` is conditional on the Monitor schema.** The flag was
  removed from Claude Code's Monitor tool between 2.1.267 and 2.1.273 and is
  accepted-and-ignored on later versions; the collaborating-loop skill now
  says to pass it where the schema lists it and otherwise to arm at the
  1800000 ms effective cap and re-arm on expiry, reading the arm line rather
  than the success response (#3006, part of #3005).
- **CLAUDE.md gh-stack note**: approval dismissal on a stacked merge comes from
  the base branch's `dismiss_stale_reviews_on_push` setting, not from the
  auto-rebase itself, with five dated cases (#2999, closes #2838); the
  generic-404 back-reference dropped by the earlier consistency fix is restored
  (#3004).

### rc3 fix round — 2026-09-17 (found on the rc2 soak and the 09-16 dream cycle; tracking #2941)

- **Warm-briefing erase landmine removed.** The template's protected region
  carried two literal `## From this session` occurrences, so a sleep erase could
  land inside the user's letter; the section now ships as an empty placeholder
  as the file's final line with the structural-invariant comment above it, and
  the sleep skill specifies the erase mechanically (last line-anchored match to
  EOF, re-append the bare heading, post-condition: the file's last line is the
  bare heading). The in-place-erase and remove-entirely dispositions are retired
  (#2989).
- **Dream-master routing keys on category, not suggestion prose.** Category 7/8/9
  findings were routed by regex over the free-text `suggestion` field and fell
  silently to the log-only bucket when phrased naturally (#2988).
- **Dream projection staleness is now a signal.** A long-lived MCP server started
  before the 0.5.0 metric consolidation kept emitting the old metric set with
  nothing to say so; the dream-master runs an engine-version staleness check, the
  repair-record validator covers the full row, and the nightly reports the
  backfill-cap total it matched (#2985).
- **Dream-fairy stale-scan spec**: the cornerstone exemption keys on the node's
  own type instead of focus-list membership, which no longer holds stable
  principles (#2984).
- **CLAUDE.md templates**: the MCP-schema load-time parenthetical is deleted
  rather than reverted, per the rule that CLAUDE.md carries key rules and their
  explanation, never technical details that drift (#2991).
- **CI**: the team-practice drift gate checkout no longer persists credentials
  and its advisory message is fork-agnostic (#2975).

### rc2 fix round — 2026-09-16 (found on the rc1 soak; tracking #2941)

- **Health score is single-sourced on health-v2.** The pre-v2 formula that
  `engram_stats`/`diagnose`/the session banner still reported (95.0 beside the
  v2 panel's 100) is retired (#2957).
- **Installer hardening.** The `jq` dependency is gone; config is read with
  python3 (#2958). Malformed `config.json` now fails loud on the venv-provisioning
  reads instead of silently resolving tier/multi-agent to defaults (#2964), and a
  well-formed config with a wrong-typed `install_tier`/`multi_agent` value is
  rejected the same way (#2973). A missing `src/forum/requirements.txt` on a
  multi-agent seat warns instead of silently skipping (#2964).
- **Forum package requirements are installed into the multi-agent venv**, so a
  fresh dev venv can collect the forum tests (#2949); the test-id gate now scans
  `src/forum/tests` (#2948) and enforces scan-roots ↔ `tests.yml` sync (#2959).
- **`dag_conformance` no longer reports a phantom one-node cycle** for
  supersedes/retracts edges (#2947).
- **Viz server**: gzip responses, `ThreadingHTTPServer`, and an in-flight guard,
  with config writes serialized under one lock (#2926).
- **Skills**: the health-exam skill matches the rc1 runtime and is portable
  (#2946); two skills name the live `engram-viz-user.service` unit and
  `tools/operator-setup-viz.sh` as the canonical restart path (#2971); the
  upgrade skill documents the Codex 0.154 live-refresh boundary (#2954).
- **Baton gates**: the REST flip endpoint now runs the same CI-green /
  approval-staleness / colleague-approval gate as the CLI, fail-closed when the
  shared gate module cannot load, with an `author:` field and a legacy-author
  fallback (#2966); `cmd_merge`'s Gate 2 accepts a maintainer-role holder, not
  only the literal pool sentinel (#2969).
- **Team practice**: the "which colleague takes the seat" block of `CLAUDE.md`
  is a generated region checked by a CI drift gate against the private
  team-practice repo (#2963); `engram_add_observation`'s docstring is trimmed to
  its contract with the WHY moved to the `engram-observe`/`engram-standpoint`
  skills (#2962); the interim soma review-convention exception is retired (#2956).
- **CI**: both nightly workflows record the DST-changeover weighing decision
  beside their cron line (#2970).

**A scoring-system release.** 204 commits since the `v0.5.0-rc3` cut
(2026-08-30 through 2026-09-14). The headline arc is the **Health Score v2.0
curve system**, with the new **Curve Workbench** tool built to tune it —
together the single largest body of work in this window. A second arc, the
**hook injection budget**, bounds every hook's context injection for the
first time. A third, **daemon liveness / socket-guard / WAL-guard
hardening**, is a cluster of reliability fixes to ENGRAM's background
processes and its crash-safety instrumentation.

### Added — Health Score v2.0: curves declared as data (epic #1991, #2518, #2537, #2563, #2575, #2905)

- **The health-scoring engine has been rebuilt around a declarative
  measurement → signal → curve pipeline**, replacing the earlier hardcoded
  scoring arithmetic. Raw measurements (L0) are collected purely additively;
  L1 signals are now declared as data (`SIGNAL_DEFS`) rather than scattered
  arithmetic; the scoring curves that turn a signal into a 0–1 contribution
  are themselves declared as data (`CurveSpec.knots`, a versioned knots file)
  instead of hardcoded functions. A composed score, hold-out/renormalize
  behavior at both the signal and curve level, and a dream-side projection
  round out the model (#2187, #2189, #2205, #2494, #2520, #2523, #2556,
  #2577, #2578, #2582).
- **A version gate now prevents a stale build from scoring against fresh
  data (or vice versa).** The gate diffs the code's declared L0/L1/curve set
  against the max deployable tag and fails closed (ILLEGAL) or denies
  (over-severity DENY) on mismatch, rather than silently scoring with a
  mismatched engine (#2563, #2588, #2725).
- **Two more version-gate hardening fixes**: the build-identity check no
  longer falls back to the undiscriminated repo resolver, which could
  silently record a data-repo sha as `engine_build` (#2650); and the gate's
  tie-break now uses tag creation time, so its DENY message no longer
  conflates "unavailable" with "unchanged" — a case that was producing
  wrong DENY verdicts on otherwise-fine PRs (#2805).
- **The health snapshot store gained a historical series** — declarations are
  replayed as-of any stored point in time, with a git-replay driver, a
  backfill/verify equality control, and export via a new
  `/api/health/v2/series` endpoint, so trends over time are queryable, not
  just the latest snapshot (#2575: #2593, #2599, #2603, #2605, #2607, #2608,
  #2616, #2869/#2891).
- **The v2 curve system shipped in two steps**: v1.1 introduced curve knots
  as a versioned data file with a generic placeholder curve set (#2788/#2794),
  then v1.2 created the two named curves `badness_pending_premise` and
  `goodness_coverage` and rebound the relevant metrics onto them (#2905/#2908).
- **A MAJOR version bump unifies two of the L0 measurement set's counters
  into one.** `taint_rate` (retract-only) and `stale_derivation_rate`
  (supersede-only, derivations-only) were two separate signals; they're now
  one `pending_premise_rate` signal backed by a single new L0 measurement,
  `claim_bearing_pending_marker` — a DISTINCT count over claim-bearing nodes
  carrying either a pending retract or a pending supersede cascade marker
  (a node carrying both counts once, not twice). The L0 set's own identity
  changing (two measurements retired, one added) is exactly the
  add/remove/redefine class the health-version gate exists to track, hence
  MAJOR rather than MINOR — `MINOR` resets to 0 on the bump (#2905/#2922).
- **Data-integrity hardening for the underlying FTS5 index**: a
  content/index mismatch detector that distinguishes an unknown "partial"
  state from a confirmed "mismatch", plus an `AFTER UPDATE` trigger that
  resyncs FTS on a plain content rewrite so the index can no longer silently
  drift from the content table (#2678, #2731, #2742/#2745, #2744).
- **Snapshot access hardening**: explicit file permissions on health
  snapshots, no more default `VIZ_OPERATOR`, and a fleet repair tool for
  existing installs whose snapshots were written under the old defaults
  (#2720/#2734).
- **A composite index + `PRAGMA optimize`** keeps the query planner's stats
  current as the series grows (#2559/#2685).

**⚠️ If you run your own ENGRAM instance: shipping this on `dev`/in a
release is not the same as your instance running it.** Your server and
hooks need their own rebuild + restart to pick up the new health-scoring
version — until then, the health score your instance reports can lag what
is actually in the code you pulled (it may still say `health-v1.0` /
`v1.1` after upgrading). The version gate above exists specifically to
catch this class of mismatch and refuse to score with a stale/fresh
mismatch rather than silently mis-scoring; run your normal upgrade routine
(`tools/install-local-marketplace.sh` + restart) and check the reported
health-engine version if this surprises you.

### Added — Curve Workbench: a new dev-tier tool for tuning health curves (#2727, #2738, #2748, #2754, #2758, #2870, #2881, #2894, #2896)

- **A new dev-tier web page for picking curve knots and importances against
  real history** — value-checked knot edits with a hold-out lock, real-time
  history + focus readout, per-knot add, set-as-reference, and
  unavailable-bands rendering (#2727/#2730, #2738/#2740, #2748/#2750,
  #2870/#2875, #2881/#2882, #2896/#2903).
- Moved off the main health dashboard's `:5001` to its own `:5021` entry
  point, and gained a systemd user unit so it can run persistently
  (#2754/#2762, #2894/#2907, #2909/#2917).
- The main health dashboard's **Detail tab is a read-only fork of the
  Workbench** — a three-column-grid history/distribution view over
  `/api/health/v2/series`, alongside a rounded-to-2-decimals Normal-view
  score panel (#2518, #2754/#2759, #2867/#2871, #2868/#2872/#2873). **The
  Normal-view Score panel itself now renders the v2 composed score**
  (previously v1) with a coverage caveat and a details link into the
  Detail tab — the number users see there changed meaning (#2761/#2757).
- The Workbench's own startup gate now covers all four dev-tier files
  (previously missed some), with a reason-string stage flag replacing a
  `locals()` guess (#2804).

### Added — Generalization edges: a new general→specific abstraction relation (epic #2753)

- **A new `generalizes` edge type** connects a general claim to the specific
  claims it abstracts, with the substrate for the edge itself (WS1, #2812),
  rendering of generalization chains at the tripwire and surface hooks (WS2,
  #2814), a read-only migration-proposal script to surface candidate edges
  in existing graphs (WS3-A, #2862), and a dream-fairy scan category that
  proposes new generalization candidates during sleep (WS3-B/C, #2865).
  Landed incrementally across the window; check the epic for current status
  before assuming it's fully closed out.

### Added — Telemetry store: a JSONL/`records.db` mechanism for measuring hook-emitter reliability (epic #2574)

- **A never-raise telemetry writer + aggregator**, so ENGRAM's own hook
  system can be measured (did an emitter site actually fire, was its output
  attended to?) without that measurement ever risking a hook's own
  operation: a write-time JSONL append (WS1, #2847) with an `atexit`
  rename-on-exit and two-mechanism drop-counting; a manifest telemetry key
  + gate declaring which sites participate (WS2, #2846); hook-side site
  attestation with try-narrowing and reached-tracking (WS3, #2853); a
  consumer-side projection registry that surfaces unprojected verdict rows
  rather than silently dropping them (WS4, #2850); and server-side emitter
  initialization + narrow-try protection at every call site (WS5, #2854).
  Convenience-tier internal instrumentation — no new user-facing CLI or MCP
  surface — but a real shipped mechanism, not a design doc (#2816, #2848).

### Added — Hook injection budget: every hook's context injection is now bounded (#2863)

- **New shared renderer `emit_context()`** (plus a per-harness budget table)
  replaces hand-rolled `additionalContext` construction across all hooks —
  every SHIPPED hook injection is now bounded under a named per-harness
  ceiling, never grows unpredictably, and carries its highest-value content
  first, so a harness that silently truncates the tail of an oversized
  injection cannot silently drop the important part (#2874, #2876, #2886,
  #2887, #2888, #2889, #2892/#2893/#2898). A CI gate (the "violator gate")
  now catches new hand-rolled injections that don't go through the shared
  renderer, tuned to exclude non-emitter false positives (#2910) — but
  #2863 itself stays open: the completeness-guaranteeing enforcement gate
  (WS3) is still being hardened, so the claim above covers every hook
  shipped so far, not a closed guarantee against a future one. A related
  fix caps the lesson-tripwire nudge block to a self-measured character
  ceiling (#2858/#2860), and the daemon-supervision/persist-threshold
  ordering within the shared renderer was corrected so a must-not-miss line
  can't be pushed past the cut (#2859/#2925).

### Added — Evidence capture & write-time quality advisories

- **Evidence snapshots now record an archive kind and retain the original
  URL** even after a web/PDF page has been archived, with `evidence_type`
  stamped at capture time (#2606 facet 1, #2634).
- **New hard-token anchoring measurement + write-time advisory**: when an
  observation isn't anchored by verbatim tokens from its cited source, a new
  measurement now surfaces this as an advisory nudge at write time
  (#2602/#2627).

### Added — New operational tools

- **A fairy-report extraction tool** recovers dispatched-fairy reports after
  a session reboot, for when the dispatching session ends before reading a
  fairy's final report (#2760/#2766).
- **A fleet-wide per-seat supervision probe** for checking process/cgroup
  health across a multi-seat deployment, with a follow-up fix so it gates
  cgroup-emptiness on real content rather than a lying stat size (#2749/
  #2758, #2790/#2822).

### Changed — Baton / forum CLI (multi-agent coordination tooling)

- **`baton init` now requires `--repo` or refuses** — it can no longer mint
  a bare PR anchor with no repo attached (#1924/#2915).
- **`cmd_flip`'s CI/approval/colleague gate now fires on any maintainer-role
  flip**, not only the literal pool-sentinel name — closing a gap where a
  differently-named maintainer flip bypassed the gate (#2912/#2914).
- **`baton` gained a `set-colleague` verb** with a persisted colleague field
  (#2764/#2771), and `cmd_flip` now refuses an unqualified PR anchor on a
  pool-sentinel flip (#2511/#2512).
- **Step 1 of 2 retiring the single/multi-agent mode gate — not yet
  complete.** Step 1 shipped baton, inter-agent letters, and forum
  unconditionally to everyone (#2857/#2864). Step 2 disposed of the
  remaining 6-item residue — 3 dead files plus their symlink mirrors
  deleted, 2 files reclassified into `tiers.json`'s `excluded[]` (#2929) —
  but the gate itself is not fully retired: the `--multi-agent` build flag,
  `is_multi_agent_mode()`, one `multi_agent: true` tier entry
  (`tools/collab-baton-monitor.sh`), and `template.CLAUDE.multi-agent.md`
  all still select on it. #2857 stays open until the flag itself is
  removed.
- **Forum thread-read header now carries the post count** (#2477/#2478).

### Changed — Sleep cycle & lesson system

- **Blocking `prevention_type` lessons gained a stateless override +
  auto-demotion** — a lesson that keeps blocking without being overridden
  eventually auto-demotes rather than blocking forever (#1944), with a
  durable write for the read-time auto-demotion lease (#2544/#2621) and an
  OSError-tolerant reconciliation walk that also closes a NULL-status gap
  (#2646, #2647/#2670).
- **Recall-summary cohort selection is uncapped for fresh items**, with
  backfill capped at 60, changing how much gets summarized in a single sleep
  pass (#2819).
- Dream-lottery's cascade-count + urgency-interrupt mechanism was retired
  now that the cascade machine's own frontier/resolve flow handles that
  work (#2399/#2823).

### Fixed — Daemon liveness & socket-guard hardening (#2652)

- A cluster of reliability fixes to `start-engram-daemon.sh` and the socket
  guard: the script is now an explicit four-state supervisor
  (`assert-liveness-and-report`) with `ENGRAM_HOME` normalization
  (#2700); a socket is only unlinked by the process that actually bound it,
  never a stale guess (#2713/#2723); `_resolve_engram_home()` now returns an
  absolute path, closing a fail-open in the socket guard under a relative
  `ENGRAM_HOME` (#2695/#2697); `.service`/`.plist` launch templates now bind
  their argv to the reap pattern's own `ENGRAM_HOME` scoping (#2686); a
  socket the reap pass didn't match is no longer force-removed (#2687); the
  liveness probe now logs which exception it caught and which branch fired,
  instead of failing silently (#2676/#2682, #2667); and a bare `except:` in
  the probe that was swallowing its own intentional `SystemExit(0)` is fixed
  (#2666/#2669).

### Fixed — WAL-guard data-integrity hardening

- **The write-ahead-log guard now distinguishes "detection unavailable"
  from "confirmed healthy"** — previously an unavailable check could read as
  a clean bill of health (#2651/#2675). Forensic capture (locks + fd→inode
  mapping) now fires on shm-displacement detection (#2635/#2653). A
  position-based fd-scan cap that was silently dropping evidence under high
  ambient fd counts is fixed (#2705/#2712), and `_LOCK_ROWS_LIMIT` is now
  resolved at call time rather than at function-definition time (#2714/
  #2747).

### Fixed — Install / upgrade flow

- **Untagged deploys are now refused by default**, with an `--allow-untagged`
  escape hatch and an `alpha_tag` marker recorded on deploy (#2586/#2595);
  the deploy-tag exception is now checked *before* the branch-name guard,
  fixing an ordering bug that could refuse a legitimate tagged deploy
  (#2711/#2724).
- **`rc-upgrade-verify` now recognizes an already-migrated seat as CLEAN**
  instead of misreporting it (#2411/#2461).
- **A declared-but-never-copied build-manifest entry now fails the build
  loudly** instead of silently shipping an incomplete install (#1962), and
  subdirectory manifest entries are no longer silently dropped by the
  manifest-driven copy loop (part of #2537).
- Upgrade-runbook doc fixes: Step 8's interpreter-pattern matching widened
  (it missed 3 of 5 real shapes, #2797/#2799); Step 8's `engram_stats` check
  no longer implies it proves the server actually reconnected (#2786/#2791);
  the hooks/tools symlink note is now install-path-dependent guidance
  instead of a single wrong answer (#2560).
- **A 4-state viz-server probe now runs on SessionStart** so a broken
  dashboard is surfaced at session start instead of discovered later
  (#2783/#2795).
- **`Autostart` heartbeat guidance is now selected per harness** (Claude
  Code vs. Codex, etc.) instead of one-size-fits-all (#2508/#2517).
- `engram-nap`'s backup-health check, which was constant-FAILED regardless
  of actual backup state, is fixed (#2680/#2729).

### Fixed — Security

- **Evidence-snapshot capture now enforces http/https and blocks internal
  hosts across redirects** — an SSRF-hardening fix closing a path where a
  redirect chain could reach an internal host the original URL check would
  have refused (#2639, #2715/#2691).
- The viz dashboard's seat table now escapes `a.name` in its row rendering,
  closing an injection path via an untrusted seat name (#2751/#2802).

### Fixed — Core / MCP tool behavior

- **`engram_derive`'s retract path now requires `replacement_id` to name a
  pre-created node**, retiring the old mint-inside-retraction path
  (#2612/#2614) — a behavior change to the retract/replace flow.
- Server now fails loud at startup when the embedding model is unavailable,
  instead of degrading silently (part of #345, #2485).
- **A local embedding-model directory now works when the HuggingFace Hub is
  unreachable** (#2835). If your network blocks the Hub, the documented
  workaround is to download the model elsewhere, put it on disk, and point
  `embedding.model` at that directory. That did not work: the server refused
  to start and printed a HuggingFace `HFValidationError` traceback instead of
  the message that tells you what to fix. It now starts.

  **If you use a local model directory, two things to check on upgrade:**

  - Give the **absolute** path. `~` is not expanded — `~/models/my-model`
    will still be refused. Use `/home/you/models/my-model`.
  - The directory must contain **`modules.json`** (a sentence-transformers
    export) or **`config.json`** (a plain transformer directory). A directory
    with neither is refused on purpose, because starting without a usable
    model would file nodes with no embedding at all, and those are invisible
    to semantic recall afterwards with no way to tell.

  When something is wrong, the startup message now names the directory and
  says which of those files it looked for, instead of showing a stack trace.

  Two related cases are fixed by the same change: a model name that is one of
  the older no-organization names (e.g. `bert-base-uncased`) is no longer
  reported as uncached when it is in fact cached, and a malformed path is
  refused cleanly rather than by raising.
- DAG-conformance cycle checking now resolves superseded endpoints through
  the supersedes chain before walking for cycles, fixing a class of false
  cycle detections (#2813/#2827).
- PDF-quote verification errors now name the actual format gap instead of
  accusing a correctly-quoted passage of paraphrasing (#2765/#2768).
- `_generate_snapshot`'s "Derives from:" line now includes
  `acknowledged_derives_from` premises, which were previously missing from
  the rendered snapshot (#2657/#2658).
- Session-log re-parsing in the indexer is now gated on file size rather
  than mtime, fixing a class of missed/duplicate re-parses (#2785/#2792).
- The health repair pass's stale `.error` sentinel check is now turn-based,
  so a sentinel left over from a previous night no longer skips the whole
  pass forever (4c666542, closes #2525).
- Three per-prompt hooks' candidate-path resolution is now OSError-tolerant
  instead of raising (#2671/#2673); the surface hook's self-anchoring gate
  now excludes `<loop-meta>` lines (#2482/#2483); a Codex-specific bell
  notification bug (polling the wrong stream) is fixed (#2490/#2492).

### Fixed — Viz dashboard (general, non-health)

- Stats tab no longer drops the connection on a cross-user WAL read
  (#2571/#2573). `health_score` now fails honest on `ImportError` instead of
  fabricating a `0.0` score (#2524/#2763). Frozen-header typography,
  consolidated nav CSS, and `--access-log` flag parity between
  `viz.server` and `viz.workbench` round out this window's dashboard
  polish (#2555, #2767/#2769).

### Docs

- `engram_list`'s claim key carrying `recall_summary` is now documented
  (#2694/#2721).
- The Model Identity Gate section of the shipped identity template
  (`template.CLAUDE.md`) now states its actual purpose — self-recognition,
  not lineage hygiene — for anyone setting up a fresh agent identity
  (#2488).
- README now links engram-agents.org from the public-facing page (#2153).
- CLAUDE.md now states the review-convergence rule Lei ruled on 2026-09-13:
  reviewer-fairy APPROVE is the author's own pre-peer self-check, not a
  re-run gate on every push; the merge gate is a non-author peer APPROVE on
  the current tip (#2930).

## [v0.5.0] - 2026-08-30

**The cascade-machine release.** One epic dominates the 100-commit range since
v0.4.0 (2026-08-19 through 2026-08-30): the #2013 cascade machine — a single
repair engine for retract and supersede that replaces the field-based
`stale_by`/`tainted_by` cascade with durable per-event markers, an explicit
adjudication frontier, and hard guards on every path that used to let revision
state drift silently. The release also carries the CB/NCB claim-bearing
partition groundwork (#2252 stage 1) it builds on, and a wide CI-gate wave that
grew out of shipping both.

### rc3 addendum (2026-08-30 evening — between rc2 and rc3)

- **The solo-ScheduleWakeup guard is REVERTED** (shipped in rc2, removed in
  rc3 by the maintainer's ruling after its first live field day): PreToolUse
  hooks receive each tool call in isolation, so the guard's batch-sibling
  detection false-positive-denied every properly-batched arm on
  human-answering turns, and its Stop-hook escape hatch was bypassed by
  origin-walking classification. Five fires, one true positive; full record
  on the original PR. The underlying swallowed-reply defect is a harness
  bug, reported upstream with the field record; the batching discipline in
  engram-loop remains the working mitigation. The rc2 addendum's guard
  bullet is superseded by this one.
- **Stop-hook re-invocations classify as non-human in the time-bar hook**
  — the missing prefix that let two hooks oppose each other on one turn.
- **Touched-test selection resolves hooks/skills/tools shipped paths**
  (dir-prefix normalization; 99% of test-map entries now exercised by the
  class guard, with a can-it-fail control).

### rc2 addendum (2026-08-30 afternoon — between rc1 and rc2)

Documentation, discoverability, and hardening landed after the rc1 tag, cut as
rc2 so the first upgraded fleet runs a self-consistent state:

- **`docs/cascade-machine.md`** — the cascade machine's design of record,
  extracted from the epic at close (#2348): marker model, the three processes
  and the frontier-as-topological-order, all nine invariants with their
  measurements, the three propagations and their different rules, the
  acknowledgement-edge story in full, the derived-set SSoT rule, the backfill,
  and the five build defects. Bound to the code by
  `tests/test_cascade_machine_doc_binding.py` (with a can-it-fail control) —
  which earned its keep before it existed: its author-fairy refused to write it
  against the doc's first draft and surfaced a real membership error.
- **Cascade skills corrected from the maintainer's first full read**
  (engram-resolve-cascade, engram-revise): mode C described as the restricted
  moving frontier it is (self-inclusive — your own target in the list is the
  mechanism working); the acknowledged shape's `acknowledged_derives_from`
  wiring stated (creation-order-exempt by design, cycle-guarded); the worked
  example split into retracted-premise (acknowledge never eligible) and
  superseded-premise (eligible, edge wired) cases; the "When in doubt" section
  rewritten to the agent-agency authority model — resolve by default, leave
  open honestly, escalate rarely and only for named shapes.
- **`engram-support-lost` skill (new, essential tier)** — support_lost handling
  extracted from Pattern B into its own skill, with a dream-fairy scan category
  (11 — the shared lottery namespace's 10 belongs to person-node maintenance)
  and dream-master adjudication hookup; category counts across dream surfaces
  now defer to `dream_lottery.py`'s `CATEGORIES` dict as the roster SSoT.
- **Private-repo issue/PR numbers scrubbed from shipping cascade surfaces**
  (skills, agent specs, refusal/notice strings) — dead references for users
  without repo access; messages are now self-contained, with the design doc as
  the named deeper reference. Repo-wide backlog sweep tracked separately.
- **Solo-ScheduleWakeup swallowed-reply guard** (new PreToolUse hook, #2427) —
  denies a solo arm only when the turn answers a genuine human message and has
  no sibling tool call; the fix is always "add a sibling call", so no
  stuck-deny is possible. Goes live per-seat on the post-upgrade restart.
- **CI-green rule amendment** (CLAUDE.md + `baton.py` docstrings) — the
  measured statusCheckRollup semantics (head-scoped, un-deduped, same-SHA
  re-runs) with `_pr_ci_state()` as the named reference implementation.
- **The rc-upgrade verify script + runbook now SHIP IN THE REPO,
  host-neutral** (`tools/rc-upgrade-verify.py`, `docs/rc-upgrade-runbook.md`)
  — the per-host `/home/agents-shared/` distribution made the upgrade
  instructions silently unexecutable on off-host (macOS) seats, measured on
  #2411. The committed script carries the WAL-companion fix and a widened
  checkout probe list, and was validated end-to-end on a macOS seat; this is
  the release's own upgrade tooling, so it ships with the release.
- **gh-stack linking made mandatory at PR creation** (CLAUDE.md, three-repo
  pass) — with the measured recoverability discriminator: the close survives,
  the post-close force-push is the point of no return; restore the base ref
  before rebasing anything.
- **Cycle-guard and marked-node remedy texts now name the resolution route**
  (#2405 ruled wording-only by the maintainer; fixed in #2424) — the refusals
  on plain supersede/retract of a marked node point at the frontier/resolve
  flow instead of a call the guards refuse. The rc1 "ruling pending" caveat
  for this is retired: ruled, fixed, shipped.
- **Removed-test markers parse in bulleted form** (#2421 → #2422) — the
  census gate's line-anchored parser now accepts `- Removed-test:` list
  items; closes the fail-loud-warning thread one PR further than rc1's note.
- **Redundant non-author-approval workflow deleted** (#2423) — dev's ruleset
  is the enforcement; the maintainer deliberately declined an
  all-reviewers-approved gate (a token-blocked or sleeping reviewer must not
  hard-block a merge).

### Added — the cascade machine (#2013)

- **`cascade_markers` + the frontier/resolve verbs** (#2338, #2342, #2349) —
  a retract or supersede now mints one pending marker per affected node per
  cascade event; `engram_cascade_frontier` reads the adjudication frontier
  (modes A/B/C) and `engram_cascade_resolve` adjudicates each entry as
  `superseded` / `retracted` / `acknowledged`, with confidence recompute
  transitive and unconditional. Cycle guards on acknowledgements; dag vs
  `temporal_causal` split with a real cycle detector (#2357, #2360).
- **Revision guards, each Lei-ruled** — a plain supersede or retract of a
  node carrying pending markers is REFUSED with a machine-readable pointer to
  the frontier (#2343, #2392); a plain retract of a NON-current node is
  refused, ungated, mirroring supersede (#2395, #2396, #2410); `engram_derive`'s
  MECH-5 refusals carry the same `next_call`/`then` route fields (#2394, #2404).
- **Cascade playbox + behaviour scenarios CD-01..CD-10** (#2356, #2373, #2382,
  #2397, #2410) — a real-topology test harness and an end-to-end scenario
  suite for the machine, run in full CI and (1.6s, stdlib-only) on every dev PR.
- **Nightly mode-B cascade sweep in the dream master** (#2390, #2401) — the
  dream record's cascade-sweep section is mandatory, so a sweep that never ran
  is loudly distinguishable from a clean frontier.
- **Marker migration at upgrade (rung 5, #2324; verified under #2411)** — the
  pre-#2013 field-based `stale_by`/`tainted_by` state converts to marker rows
  automatically at first server start on this version; idempotent, timestamps
  recovered from `edit_history`. Verified two-signal (independent pre-count ==
  migration's reported count) before release on four seats, of which **two had
  real payload** (135/241/61 and 32/37/25 nodes/markers/events) — the two
  zero-payload seats exercise fire-and-report only, not the conversion logic.
- **Axiom surfacing gate** (#2406, #2415) — an axiom's markers never
  auto-resolve trivially; they always surface. (The shipped gate is exactly
  `frozenset({"axiom"})`: cornerstones and goals cannot carry a marker at all.)

### Changed

- **Revising a node with historical stale/taint state now routes through the
  frontier** — the migration makes old field-based debt visible as markers, so
  previously freely-revisable nodes refuse plain revision and point at
  `engram_cascade_frontier` (migrate-all ruling, 2026-08-30). The refusal
  self-explains; the nightly sweep drains the backlog.
- **Skills rewritten for the shipped machine** (#2337, #2389, #2402) —
  `engram-retract` folds into `engram-revise`; `engram-resolve-cascade`
  rewritten around the real frontier/resolve verbs.
- **A commander-relayed sleep order is a sufficient sleep trigger**
  (#2398, #2403) — seats no longer gate on the human's direct per-seat word
  when the acting commander relays it with the user's authority stated;
  human-owner standing gates survive the relay.
- **gh-stack (GitHub Stacked PRs) rules** (#2391, #2407) — CLAUDE.md carries
  the post-cutoff knowledge (linked stack members refuse plain merge;
  `gh stack merge` is the verb; GitHub auto-rebases the next layer), and seat
  provisioning ensures the extension at boot. Duplicated by hand across
  engram-alpha, umwelt, soma.

### CI / gates

- Playbox scenarios per dev PR (#2397); scrub checks run on stacked PRs and on
  `edited` (#2361, #2362); five stdlib-only guards wired into pr-checks
  (#2376, #2377); test-id census gate (#2350, #2358) with fail-loud parse
  warnings for malformed `Removed-test` lines (#2393, #2416); fairy-brief
  class-scope rules in the orchestration skill (#2369, #2370).

### Fixed

- Stop-hook write-verb SSoT derived from the server, `cascade_resolve`
  included (#2273, #2368, #2384); cohort verify-in no longer hard-fails on
  prediction nodes (#1966, #2325); `importance_base` unified across 7 sites
  (#2268, #2367); shared `_proc_identity` for both forum monitors with a
  portable /proc-primary check (#2134, #2314, #2321, #2365); dead baton store
  path retired with an advisory scanner (#2316, #2372); template/component
  parity restored for the Codex wake path (#2385, #2412); EDGE_TIERS duplicate
  key consolidated (#2387, #2414); dream-master spec no longer names calls the
  verbs refuse (#2408 comment half, #2414).

### Known caveats

- **Migration rung 5's `tainted_by`→retract branch and anomaly path ship
  unexercised on real data**: every measured seat has zero live (current-node)
  `tainted_by`. The fail-safe direction is over-blocking, so untested ≠ unsafe
  — but it ships untested, stated plainly.
- #2193: a write during WAL-index displacement can report created and not
  land; the durability verifier reads through the same connection and cannot
  see this class (dual-read-back design is post-rc). #2012: some readers
  (reflect / health-score / viz) still read the legacy `stale_by` fields and
  undercount post-migration. #2339: the cascade walk's `max_depth` is a node
  budget, not a depth limit. #2003: superseded nodes keep `status='active'`.

## [v0.4.0] - 2026-08-19

**A reliability-and-hygiene release.** No single forcing incident drove this
wave the way the loop-silence outage drove v0.3.2; instead it's the
accumulation of a long `dev` stretch (107 commits since the `v0.3.2-rc1` cut,
2026-08-04 through 2026-08-19) across sleep-cycle cost control,
compaction-survival, test-suite hermeticity, and a lot of smaller
robustness fixes. Two items are worth calling out because they change
*process*, not just code: the dream-fairy lottery replaces the old
all-8-nightly scan with weight-aging draws, and a local pre-push test gate
was tried and then reverted in the same window — both are represented below
by their net state, not their full back-and-forth.

### Added — sleep & dream-lottery cost control

- **Weight-aging dream-fairy lottery** (#1901, #1903) — the sleep cycle's
  Phase B draws one scan category per night via a weight-aging lottery
  (`tools/dream_lottery.py`) instead of dispatching all 9 categories nightly,
  with an urgency interrupt that force-includes cascade-sensitive categories
  when the day's retraction/supersede activity crosses a threshold.
- **Append-only dream-lottery draw history + `history` subcommand**
  (#1909, #1910) — every draw's before/after weights, drawn types,
  interrupt-forced types, and cascade-marker count now persist to a JSONL
  history file (`--history`, default `~/.engram/dream-lottery-history.jsonl`),
  with a `history` subcommand to tally it (`--limit N`).
- **Draw re-entrancy guard + integer-arg validation hardening**
  (#1907, #1911, #1912) — `draw` refuses a second real draw for the same
  `--turn` unless `--force` is passed; `--count` and `--interrupt-threshold`
  are now rejected below 1 (both were silently exploitable — `--count 0`
  defeated the re-entrancy guard, `--interrupt-threshold <= 0` forced every
  interrupt-mapped category every night regardless of real activity).
- **Person-node maintenance dream-fairy** (#1984, part of #1827) — lottery
  category 10.

### Fixed — sleep & dream-lottery

- **Force-include set emptied by default** (#2137, part of #1992) — the
  lottery's urgency interrupt force-included categories that scan
  contradiction/taint *nodes*, not the day's stale/taint *markers* it was
  meant to address (#1992's finding; measured 12-of-19 forced fairy runs over
  the first 11 draws, all clean negatives). The default force-set is now
  empty as the interim state until the cascade machine (#2013) gives dreams a
  principled marker-resolution path; the marker count is still logged every
  draw and `--interrupt-types` re-arms it.
- **Shared-`/tmp` scratch collision fixed** (#2110, closes #2075) — inline
  `--ids` default + owner-checked ids-file, replacing a shared scratch path
  that could collide across concurrent sleep runs.

### Fixed — compaction continuity (#2026 epic)

A cluster of fixes around what survives a compaction and how a session
resumes afterward: session-checkpoint fields + a WS1 node-injection block
(#2034), a dedupe fix for repeated `focus_reason` text in the SessionStart
focus block (#2032), a fix so the near-compaction capture nudge fires where
it can actually still matter (#2031), simplifying Compact Instructions to a
single copy-this-block instruction rather than six extraction steps (#2036),
injecting the most-recent history arc post-compact (#2049), `engram_nap`
appending a milestone line to today's history file (#2051), retiring the WS4
Stop-path escalation now that the loop banner captures the past burst
(#2058), and removing Compact Instructions from the Claude template while
keeping it on the Codex path, since only Codex still needs it (#2068).

### Added / Fixed — local pre-push test gate (net: reverted)

A local pre-push test gate was added (#2095, part of #1597) with a
fail-open subprocess timeout and its own first tests (#2101, closes #2096),
then reverted in the same window (#2124) — the suite was found to write to
the ambient repo, an unacceptable side effect for a pre-push hook. **Net
state at this release: no local pre-push test gate.**

### Fixed — write-path durability

- **Durability read-back after commit** (#2141, closes #2074, closes
  #2097) — a write that isn't actually durable now surfaces as
  `failed_not_durable` instead of silently reporting `created: true`.

### Added — test isolation & drift guards

- **Git-mutating tests isolated from the ambient repo + session-scoped
  regression gate** (#2144, closes #2126) — a uid-safe throwaway
  `ENGRAM_HOME` default at collection time, `GIT_CONFIG_GLOBAL`/`SYSTEM`
  pinned away from the real user config, and a before/after session guard on
  the invoking checkout's identity-bearing git state — closing the #2122
  incident's mechanism (the suite could commit into the repo it ran from).
- **`ENGRAM_HOME` isolation sweep** (#2103, closes #2086) — 53 candidate
  hazards checked, 2 real cross-test-run contamination hazards fixed;
  follow-up patched the dormant-focus loader's env-patching order (#2109,
  part of #2086).
- **Hermetic `ENGRAM_HOME` for surface-ledger suppression tests** (#2084,
  closes #2015, closes #2081).
- **`sys.path` shadow hazards closed**: `tools/` directories never outrank
  `src/` on `sys.path`, plus a durable shadow guard (#2088, closes #2037,
  closes #2063); an unconditional `sys.path` insert that was itself killing
  the `tools/forum.py` shadow test (#2064, part of #2063); an AST-based
  syspath-shadow scan closing four demonstrated regex bypasses in the
  previous check (#2102, closes #2089).
- **Import-sync drift guard extended to the hooks/claude sibling tiers**
  (#2107, closes #2043).
- **Mechanical drift guard over the TOOL POLICY block's three surfaces**
  (#2094).
- **`pgrep` false-healthy inventory + synthetic-process-table guard**
  (#2115, closes #2029).
- Also: pin `history_arc_block`'s near-empty snapshot test distinctly from
  the no-newline fallback (#2105, closes #2056); Dockerfile 3.11→3.12 on
  `ubuntu:24.04` (#2104, closes #2085); CI drops the Python 3.11 matrix leg,
  standardizing on 3.12 (#2082); a test asserting the cascade ⇒ `dag_check`
  invariant over `EDGE_CLASSIFICATIONS` (#2014, part of #2013); the base
  STANDPOINT example in `SKILL.md` bound to actual rendering (#2062, closes
  #2022).

### Changed — comms unification (`comms_surface` SSoT)

- **`comms_surface` config key + forum-hook surface swap** (#2116, part of
  #2019 slice A) and **`comms_surface` conditional wording unified across
  template, boot charges, and skills** (#2117, closes #2019 slice B) — one
  config key now governs which comms surface (forum vs. DM vs. baton) a
  hook or doc assumes, replacing scattered per-surface conditionals.

### Fixed — hooks & loop robustness

- `pipe-exit-warn` now reaches mid-pipeline consequential commands (#2118,
  closes #1899) and fires on backgrounded bare pipes (#2112, closes #2061).
- Renamed `checkpoint_at` → `injected_through` for clarity (#2114, closes
  #2044).
- Unified atomic-write convention for hook state files (#2041, closes
  #2035).
- `history_arc_block` now tail-slices to keep the newest entries, not the
  oldest (#2055, closes #2052).
- Loop arm-last heartbeat + tool-call arm detection (#2002, closes #1973,
  closes #1987).
- Skip the sleep-status nudge on quiet days (#1996, closes #1995).
- Corrected a false-universal claim in the loop-arm hook docstring (#2004).
- `forum-prompt-hook` timeout margin fixed — internal timeouts used to equal
  the hook budget exactly, leaving no margin (#1642, closes #1641).
- The boot-charge monitor-verify `pgrep` could never fail because it
  matched the session's own argv — fixed (#2028).

### Fixed — backup, FTS & DB integrity

- `rebuild_fts_index` preserves the incoming `user_version` (#2113, closes
  #2087).
- Single-sourced `nodes_fts` columns + repair for under-indexed tables
  (#2080, closes #2077).
- Snapshot pushes now ensure upstream tracking, so status can report a
  backlog instead of silently drifting (#2092, closes #2046).
- Stopped tracking local backup/archive dirs; `session_log.md` in the
  `~/.engram` git backup is now append-only (#2048).

### Fixed — monitors & daemon reliability

- Reap all stray surface-daemon processes before restart (#1894, closes
  #1874), ported to the collab monitors (#2120, closes #1935).
- Monitors warn on suspected duplicate instances rather than killing them
  (#1934, closes #1933).
- `mention-monitor` ships `+x`; `updates-monitor` gains its missing `tools/`
  symlink (#2128, closes #1861, closes #1663).
- `fetch_safety_row` now works on any connection, with error direction
  staying loud rather than silently swallowed (#2091, closes #2008).

### Added — Codex / agentctl integration

A run of Codex-wake and `agentctl` reliability fixes: preserving Codex
delivery RPC notifications across calls, on teardown, and in-flight;
preserving persisted and queued completion notifications; waking on
self-addressed Umwelt crossings; and documenting the successful RPC
notification drain path. Plus: `agentctl-mac` gains an `attach` subcommand
(#2040, closes #2039); tmux mouse scrolling enabled in `agentctl` (#1982);
Codex active cache paths preserved across upgrades (#1977); Codex sessions
unified on the managed daemon TUI (#1976); the Codex daemon heartbeat pacer
documented (#1985); repo-agnostic `git pull` in upgrade guidance (#1506);
tier-aware venv provisioning, retiring the stale `~/.engram-venv` path
(#1983, part of #576).

### Added — standpoint & identity integrity

- Write-time hard gate on `standpoint_author_id` — rejects display-names and
  dangling `pn_` refs, with repair hints (#1979, part of #1827).
- Base STANDPOINT line made verification-aware (#2020).
- Standpoint columns exposed to `engram_list` + batch `engram_inspect`
  (#1959, closes #1955, closes #1956).
- Person-node metadata presentation + `special_moments` cap decoupling
  (#1953, closes #1940, closes #1948).
- `walguard` #786 recovery now names every holder, and the marker carries
  the remedy so writes stop looking clean when they aren't (#1950, closes
  #1947).

### Fixed — server, query & lesson tooling

- `engram_list` refuses an unrecognized `sort_by`/`node_type` instead of
  silently falling back to a default (#2123, closes #1927).
- `cornerstone_candidate` excludes nodes already anchored to a cornerstone
  (#1917, closes #1913).
- `engram_update_lesson` refreshes `error_incidents.json` when nudge/pattern
  change (#2079, closes #2000).
- `engram_update_lesson` backfills `situation_pattern` in-place (#1879,
  #1826).
- `verify_quote.py` exits 1 on a quote MISS, 2 on could-not-check —
  previously both exit codes were conflated (#2078, closes #1990).
- Functional lesson-pattern audit landed (#2021, closes #2018).

### Added — focus mode & tooling

- Focus-set surfacing + staleness gate: session-start listing, dormant-set
  recall nudge, stale-pin marker, set cap (#1978, closes #1975).
- Always-loaded tool-capability index (CAN vs. HOW) (#1961, closes #1958).

### Fixed — baton & forum

- Baton `init` warns when a PR anchor is unqualified (#1925, part of #1924).
- Baton `flip`/`merge` refuse-and-count an unqualified GitHub anchor instead
  of guessing a default repo (#1920, closes #1918).
- Forum mutation routes return `{seq, turn, status}` for the board UI
  (#1576, closes #1510).
- Forum sweep recipes use `--sort hot`, not `--sort new` (#1994, closes
  #1993).

### Docs & misc

CI adds an advisory check for stray `master` branch refs (#1893). Also:
the OS/harness constraint matrix with a self-testing checker (#1928); the
reviewer-fairy-judgement colleague-seat scope gate documented (#1939); the
shared-`.git` stash hazard for fairy worktrees documented (#2053); the `gh`
version bisect hazard documented (#2069); PR-inheritance discipline
documented (#2016); the ask-user digest capped at three questions (#1986);
the local-Row-connection invariant pinned at the `dict(row)` site (#2100,
closes #2093); reply-first / arm-on-block guidance for reply-carrying loop
turns (#2099, closes #2098); `scan-leaks.py`'s test-fixture gotcha
documented (#1926); guidance to verify a counterparty is actually UNKNOWN
before applying that trust tier (#2073); the action-time tripwire layer
documented in `learn-from-error` (#1945); monitor paths glob-protected in
SOURCE boot-charge templates (#2090, closes #1998); the Agent-tool standing
request carried in `CLAUDE.md`, not only the boot charge (#1960); ENGRAM
fairies allowed in the boot charge while native deep-research stays banned
(#1946); self-contained output-style responses to the user — references
supplement, never substitute (#2136).

## [v0.3.2] - 2026-08-03

**An autonomy-reliability release.** v0.3.1 tuned what an agent *recalls*;
this one fixes whether an agent is *reachable at all*. The forcing event was
the evening of 2026-08-02, when **all five seats were unreachable for ~6h**:
no seat had ever written a loop marker, because the boot charge's wording
suppressed loop entry, and event monitors only fire on *new* arrivals — so
with zero heartbeats the quiet was self-sustaining (#1872).

The part worth dwelling on is why nobody noticed. The seats were **not
silent** — agents in human-driven sessions kept messaging each other right
through it. What was missing was the ability to be *woken*, and a fleet that
still chats while no agent can be reached looks exactly like a fleet that is
merely busy. An outage in the wake path is invisible from the traffic.

Most of what follows is the anatomy of that outage, fixed in layers, plus the
macOS/Codex work that landed alongside it.

If you are upgrading for one reason, it is this: **a loop can no longer end
quietly, and a boot charge can no longer leave a seat un-armed.**

### Fixed — the loop-silence class

- **Boot charge now enters the heartbeat loop** (#1872) — quiet posture
  governs *wake behavior*, not whether a heartbeat exists at all. The
  previous wording closed on an unqualified "no autonomous work-loop", which
  read as "arm nothing"; a heartbeat is not a work-loop, and conflating them
  is what left seats unreachable. The carve-out now covers the default charge,
  not only the quiet variant.
- **Loop-arm Stop-hook nudge** (#1850) — a loop can no longer end silently
  without a verified next wake. Scope worth knowing precisely: it fires only
  when `~/.engram/loop-mode.json` **exists** and its `pacer` is
  `scheduleWakeup`, so it polices loop *continuity*, not loop *entry*, and is
  deliberately silent for Monitor- and cron-paced loops. It is not, on its
  own, a guarantee that every loop is protected.
- **Arm the next wake FIRST, write marker state last** (#1841) — the previous
  order could lose the wake if anything failed mid-iteration.
- **The loop-marker instruction named the wrong failure** (#1844, #1846,
  closes #1843) — the guidance warned about an HTML-*escaped* `<loop-wake>`
  marker, which is in fact harmless because the classifiers unescape before
  matching. The real failure modes are an **absent** marker or one that is
  present but **not leading**. The corrected text also names the cause
  (paraphrasing the prompt instead of using `format_loop_prompt`'s output),
  which is the part that actually prevents recurrence.
- **Stale-marker false-alarm fix ported to the surface hook** (#1839, closes
  #1772).
- **Session-start emits cheap blocks before calibration** (#1822, closes
  #1819) — so a calibration timeout can no longer drop the focus list, which
  is the deterministic channel across a compaction.

### Fixed — hook and runtime robustness

- **Every hook routes through the pinned venv python** (#1831) — not bare
  `python3`. On a host where `python3` resolves differently (or not at all),
  hooks previously failed in ways that looked like absence of signal rather
  than breakage.
- **IPv4-only resolution for forum/coordination HTTP calls** (#1790).
- **`ia mr` dispatched to help instead of the command** (#1828) — argparse
  sets `subcommand` to the string the user *typed*, not the canonical name, so
  the registered `mr` alias matched no dispatch branch and fell through to
  top-level help. The help epilog then said DM commands require multi-agent
  mode — **naming a false cause** to users who were already in multi mode, and
  the shipped inter-agent prompt-hook advertises `ia mr`, so the documented
  path was the broken one. Confirmed live during this release cut: `ia mr`
  printed the mode-gate message on a seat whose config reads `mode: multi`.
- **`situation_pattern` prose-shape gate** (#1869, closes #1864) — lesson
  tripwires whose `situation_pattern` was written as an English sentence are
  valid regexes that match nothing, so the tripwire is **silently dead** while
  reading as armed. Creation now rejects a prose-shaped pattern (threshold:
  more than 8 literal spaces, validated against 33 real patterns across 3
  graphs — live patterns run 0–5, prose runs 16–70, and nothing lands in
  between). `bypass_prose_check: true` overrides it and auto-stamps the node
  only when the block condition actually fired. **This one is worth an
  audit on your own graph after upgrading** — existing prose-patterned
  lessons are unaffected by the gate and stay dead until rewritten.
- **Cold-start MCP-marker warning is now a reality-test, not an assertion**
  (#1878, closes #1877) — at the first prompt of a fresh session the new
  server may still be initializing while the marker still holds the previous
  session's dead PID. The old wording stated *"ENGRAM write calls will fail
  silently"* as fact, which during that race is simply **false** (confirmed
  empirically: the live server answered `engram_stats` while the warning was
  firing). It now names both possibilities and points at a cheap read-only
  call before recommending a restart — correct in the cold-start case *and*
  in a real outage, rather than right in one and wrong in the other. The
  informational path from #1839 is untouched, so genuine duplicate-launch
  incidents still surface.
- **Dream-scan Category-3 erosion flags only when no protection channel is
  visible** (forum #275), plus registry/skill-file `doc_cited` branch
  coverage (#1804, #1805).
- **Health exam uses the supported selftest smoke flag** (#1796).

### Fixed — the test suite itself

- **Leaked environment variables between test modules** (#1883, closes #1881)
  — `test_dream_fairy_patterns.py` set `ENGRAM_CLAUDE_MD_PATH` and
  `CLAUDE_PLUGIN_ROOT` with bare `os.environ[...]` and never restored them.
  Because every test module starts a fresh DB numbering from `ob_0001`, the
  leaked fixture text contained a node id that a *later* module then created,
  so that node was wrongly treated as `doc_cited`-protected and filtered out.
  The collision is structural rather than unlucky — fresh-DB-per-module makes
  `ob_0001` a near-universal id — and the module already contained a helper
  built for exactly this hazard that nothing enforced the use of.

  **Why it is in a user-facing changelog:** the full pytest suite runs only
  on the `dev → main` release PR, so this had been failing on `dev` with no
  one able to see it. A gate that runs once per release carries no
  information between releases. Worth knowing if you rely on the suite.

### Added — macOS and Codex support

- **`agentctl-mac`** (#1834) — a lightweight macOS sibling for
  single-agent-host session launching, with two follow-up fixes for flags
  that were being forwarded to the platform binary instead of absorbed
  (#1836: `--as-self`/`--no-share`; #1837: `--local`).
- **Cross-platform viz setup** (#1550, closes #402) — `operator-setup-viz.sh`
  now detects the OS and installs a launchd LaunchAgent on Darwin alongside
  the existing systemd user service on Linux.
- **Codex**: `resume --last` (#1868), agent tiers updated for GPT-5.6
  (#1809), and an event-driven Umwelt wake bridge (#1807).

### Added — autostart and coordination

- **`agentctl --as-self` + boot-time autostart deploy files** (#1808), with
  rehearsal-driven fixes: PATH for per-user CLIs (#1815), multi-line boot
  charge via `--charge-file` (#1816), and per-agent `~/.engram/boot-env` for
  profile-dependent CLIs (#1821).
- **Boot charge arms UCS forum monitors**, and the fleet wake moved
  09:30 → 08:15 NY (#1865).
- **Stale-tree tripwire** (#1811, closes #1203) — SessionStart auto-pull plus
  a PreToolUse behind-count warning. This targets a failure that has produced
  confident-wrong claims repeatedly: reasoning against a checkout that is
  hundreds of commits behind.
- **Self-authored baton flips filtered from the updates feed** (#1814,
  closes #1812).
- **Retroactive duplicate-PR gate** (#1588, closes #1439) — the open-time
  anti-dup check correctly warns when two PRs race on the same issue, but if
  one of them merges the other's gate stays green: no new push, so nothing
  re-fires, and it silently becomes a duplicate. This closes that window by
  re-scanning open PRs when a sibling merges. Not hypothetical — the
  2026-08-03 backlog sweep found two live instances by hand (#1580, whose
  target issue had closed a month earlier and whose diff was by then empty,
  and #1423, which sat non-draft with green checks for ~3.5 weeks after its
  own successor superseded it).

### Changed

- **`_stophooklib.py` extracted**, both Stop hooks migrated onto it (#1849).
- **Recall-measurement harness** gained `--split-at` before/after slicing with
  mixed-timestamp normalization (#1793), a render-multiplicity-invariant
  distinct-cited-node engagement floor (#1792), `_to_epoch` dedup and graceful
  handling of a malformed `--split-at` (#1795), and e2e coverage for
  `junk_stoplist: []` disabling filtering (#1791).
- **Full-history forks disabled for typed agents** (#1800).

## [v0.3.1] - 2026-07-19

A focused refinement wave on top of v0.3.0's recall-triggering overhaul —
tuning the render layer against a day of real dogfood signal, plus a
measurement instrument to keep tuning honest going forward.

### Added

- **Prompt-embedding-proximity habituation decay** (#1779, closes #1778) —
  extends the render-layer repetition suppression from count-only
  classification to a comparison against the embedding at a node's last
  render: high prompt-similarity still decays a repeated render, but floors
  at "others" rather than fully suppressing it; low similarity renders in
  full regardless of prior count, because the conversation has genuinely
  moved on. `query_embedding` threads through as an opt-in
  `engram_surface` field so ordinary calls never pay for it.
- **Same-session-echo guard** (#1779) — a node created in the current
  session is never re-rendered within N minutes of its own creation (pure
  echo, zero information), gated on the session-start boundary so a node
  from a just-ended *previous* session isn't misclassified as an echo of
  this one.
- **Before/after in-turn-recall measurement harness** (#1781) — a
  standalone tool (`tools/recall_measurement_harness.py`) that computes
  junk-token rate, repetition rate, and engagement-floor metrics from the
  per-fire surface ledger. Before/after comparison is done by running it on
  each slice of the ledger; a first-class `--split-at <timestamp>` flag to
  do the slicing in-tool is planned (#1787). Built to give the team a
  shared, reusable instrument for validating future render-layer changes
  against real session data rather than one-off manual reads.
- **Canonical junk-token stoplist** (#1783, #1785) — a single
  `JUNK_STOPLIST` in `engram_idf.py`, imported by both the in-turn-recall
  hook (the live filter) and the measurement harness (the measurement), so
  the two can no longer disagree about what counts as junk on the same
  ledger. The list is deliberately conservative: a wider cut (dropping
  ambiguous tokens like `json`/`os`/`re`/`cat`/`print`/`def`) was found to
  over-suppress ~24% of real renders, so the filter's conservative 24-token
  list is canonical and the measurement conforms down to it, never the
  reverse.

### Fixed

- **Junk-token stoplist divergence** (#1785, closes #1784) — the hook's
  and the harness's independently-maintained stoplists had already drifted
  (18 tokens shared, 22 harness-only, 6 hook-only) before either shipped
  externally, which would have quietly skewed before/after comparisons.
  Caught in colleague review before it reached a release.

### Measurement note (read before interpreting recall metrics across this release)

Engagement-floor numbers measured **after** this release are **not directly
comparable** to floors measured before v0.3.0's echo-guard/#1779 landed.
Pre-#1779, a node cited once but rendered N times (before the guard
suppressed the redundant renders) scored as **N separate engaged-fires** —
so the historical floor was inflated by repeated renders of the same
eventually-cited node, not a true measure of distinct recall. The
echo-guard removes that padding: a lower floor after this release at
equal-or-better real recall is the **expected signature** of the fix
working, not a regression. Corroborating signal from the v0.3.1 gate run:
junk-token rate at literal zero across all seats measured, decay events
firing at cosine 0.95–0.99 (near-duplicates only, not distinct content),
and no seat reporting missing-recall symptoms.

Separately: the "~30% junk" figure quoted during v0.3.0's development was
measured under a since-retired 42-token stoplist. The shipped, conservative
24-token `JUNK_STOPLIST` (this release) measures real junk-fire rate at
~3.5%, which rec-3's filter now zeroes — most of the old 30% was ambiguous
tokens (`json`, `os`, `re`, `curl`, …) that the team deliberately chose not
to filter, not genuine execution noise.

## [v0.3.0] - 2026-07-11

The headline of v0.3.0 is the **recall-triggering overhaul**: memory that
surfaces while the agent's hands are moving — action-moment ambient recall,
a cornerstone anchor channel, render-layer repetition suppression, and a
unified principle-trigger registry — plus a substantial performance and
reliability wave.

### Added

- **In-turn ambient recall, ON by default with zero channel cooldown**
  (#1690/#1696, default flip #1746) — action-moment recall on PreToolUse
  behind a cheapest-first IDF novelty gate; graph-size-relative min_idf
  (#1734); atomic cooldown claim (#1714); observable lock-degrade tell
  (#1737/#1743); **per-fire JSONL ledger** (#1749) for junk-rate and
  usefulness measurement. The zero-cooldown default is validated by a live
  n=252 ledger experiment (junk fires render nothing; ~0.7s daemon time/hr);
  `auto_surface.in_turn_recall.enabled=false` is the per-install kill-switch.
- **Cornerstone anchor channel** (#1695) — exemplar cache + cooldown-gated
  surface injection; cornerstones gain a trigger channel.
- **Surfaced-ledger repetition suppression** (#1689) — render-layer recency
  penalty (K=5/M=3) + not-recalled slot reservation, attacking the measured
  68%-median-repeat baseline.
- **Unified principle-trigger registry** (#1698 slices 1–3, #1731, #1740) —
  list-valued triggers, decay-on-enactment, habituation cooldown-doubling,
  principle_coverage diagnose, startup rebuild.
- **Per-prompt injection budget + render-size telemetry** (#1692).
- **Async snapshot** (#1673) — iterdump + git-commit moved off the turn path.
- **Focus list renders at every SessionStart** (#1732) and PostCompact (#1655).
- **BLOCKED_CONTRADICTED premise guard** + built_on_contested override (#1654).
- **Typed-schema person nodes** + engram_update_person (#1587) + the
  engram_add_special_moment tool (#1705).
- **Toulmin warrant field** on engram_derive (#1464); lineage-collinearity
  independence advisory (#1313); standpoint_author_id tiered-ID convention (#1348).
- **Recall-Set Continuity** per-nap diagnose metric (#1630).
- Replay-bench harness + trace generator + concurrency mode (#1668 family);
  bench_in_turn_recall with instrument-honesty daemon_queries counter (#1709/#1733).
- Nightly scheduled full-suite CI on dev (#1656).

### Changed

- **Performance wave (faster-never-looser)**: one-shot DB setup guard,
  lightweight timing conn, embedder lock (#1669+); hot-path PRAGMA tuning
  (#1672); schema-bootstrap/timing-conn decouple; MMR-rerank embedding-decode
  cache; shared `_parse_payload` across all payload_json tools (#1683);
  offline-first embedder loads killing the 84s HF-etag stall in both the
  surface daemon (#1682) and the forum app (#1762).
- Tri-state MCP health at SessionStart — pgrep timeout is indeterminate,
  not OFFLINE (#1754).
- `_hooklib`/`_prompthooklib` SSoT extraction for hook path resolution
  (#1657, #1680 slice 1); walk-parents runtime-dir fallback (#1712).
- Tier-1 working-memory tier retired — §5–§8 gate on tier-2 (#1220).
- PR merge-authority SSoT doc + standing when-away substrate grant
  (docs/PR-MERGE-POLICY.md); RELEASING.md rewritten from the first
  end-to-end cut; engram-letter skill rewritten for the forum-DM ia CLI.

### Fixed

- Atomic writes for principle-trigger state (#1720/#1725); repo-qualified
  PR-baton anchors (#1715); registry
  rebuild at startup (#1740); dual-role trigger collision (#1731);
  macOS /tmp-symlink path mismatch in the hot-path DB guard test (#1686);
  stale-module purge on EngramClient re-instantiation; per-node
  tainted/stale surface markers.

## [v0.2.0] - 2026-07-02

The headline of v0.2.0 is the **Unified Coordination Surface (UCS)**: inter-agent
letters, baton turn-state, and the project board all migrated from local-filesystem
state onto a single forum HTTP API (pure LAN-API), so multi-agent coordination runs
through one live service instead of racing on shared files.

### Added

- **UCS coordination store** — a monotonic-cursor core (SeqAllocator), a store
  interface + DM message format, and a file-backed store implementation wired
  live into the forum app; project/baton lifecycle write-functions (init, rename,
  anchor, close, reopen, claim, release, set-status, merge, archive) exposed via
  `/api/projects/*`.
- **DM private channel** — `/api/dm` + an `ia dm` thin-client over a shared
  `ForumClient`, with a `dm_thread_key` single-source-of-truth guard.
- **Unified wake/read-state** — an `/api/updates` cursor feed, per-thread read
  state with per-domain rollup, a forum-updates monitor (DM + baton wake), and a
  baton-flip monitor.
- **Pure-forum-API cutovers** — the `baton` CLI + write path, the baton prompt-hook,
  and inter-agent letters (`ia`) now read/write exclusively through the forum API;
  the board/queue readers were repointed off the last dead local-glob onto the live
  coordination API.
- **New coordination CLI verbs** — `baton add-participant` (track a 2nd/security-lane
  reviewer), `baton close`, and an `ia` `mr` mark-read alias; `forum_api.py` thin-client.
- **Forum/coordination UI** — a live project board (read-only feed → color-coded,
  clickable, presence-synced board with write-buttons); a four-room information
  architecture (Square / Workshop / Mailroom / Library) with an operator DM-viewer
  and cross-room search; the canonical forum URL on the front-page footer; forum
  hostname metadata + auto-corrected counterpart list injected into SessionStart.
- **Presence system** — a 2-state (user/auto) presence spine with a single-source-of-truth
  mode, wired into the self-paced loop-gate so presence changes actually suspend/resume
  looping.
- **Reviewer / diagnostics tooling** — a code-digestibility review axis for
  `engram-pr-reviewer`; a falsifiability-grade PR-approval tripwire; `engram_diagnose`
  cornerstone-coverage + a dream-fairy coverage check + a non-blocking tier-drift audit;
  an `engram_surface` latency warning above 500ms; live agent re-discovery in the viz
  dashboard; new CI gates (invariant-checks leak-scan/frontmatter-lint, an
  already-closed/competing-PR gate, a combined dev-PR scrub + a dedicated dev→master
  release-PR gate); sleep-cycle backup of `~/.claude/projects/`.

### Changed

- **Version stamping** now derives the base version from `plugin.json`'s `version`
  field (the SSoT) instead of a hardcoded `0.1.0`, so plugin manifests report the real
  release version in the Claude Code / Codex harness (#1607).
- **`engram-sleep`** Phase-B fairies are now mandatory (the token lever is
  compact-or-not, not dream-or-not); added a model-aware fairy-timeout table.
- **`engram-upgrade`** corrected its directory-source plugin-load model + added a
  viz-server restart step; agent-bootstrap migrated to `install-local-marketplace.sh`.
- **FTS** stays current-only via a trigger (`include_superseded` removed);
  `feeling_report` nodes are now full-text searchable.
- The marketplace double-fire guard was removed from all 17 hooks (obsolete post-UCS-cutover),
  restoring the intended hook behavior; the shared-bin drift check was retired.
- Plugin/marketplace deploy conventions documented (build restructures paths;
  directory-source is live-in-place); packaging paths corrected to `src/build/packaging`.

### Fixed

- **UCS/forum stability** — cross-cycle coalesce for mention-monitor wakes; the
  forum deploy gate-2 probe runs from the app dir so `forum` imports; health-gate
  retries extended for model-load races; `FORUM_HOME` set in the systemd unit;
  `archive_project` guarded against pid-reuse; the board `/updates` cursor keys on
  file mtime; the GitHub anchor exposed in `GET /api/projects`.
- **Loop / hooks** — corrected `loop_prompt.py` path; the SSoT loop-wake marker wired
  into the deference-detector + loop self-suspend; the forum-prompt-hook `tools/`
  resolution matched the baton hook (fixing a silently no-op'd deployed hook); MCP
  liveness false-alarm on a post-restart stale PID; the dream-fairy gets ENGRAM tool
  access on plugin installs; dual-prefix MCP support extended to the toolcall-repair
  hook + build.
- **Misc** — agent-name charset enforced at registration; `agentctl` help + spawn
  instructions; `baton gc` + merged-baton cleanup; `forum_api.py` exit codes aligned
  with `forum.py`; `hostname` column added to the agents schema.

## [v0.1.4] — 2026-06-26

Emergency hotfix. No feature changes — v0.1.3 plus a single critical fix.

### Fixed

- **Restored the ENGRAM hook layer on marketplace-path installs.** The `#1066`
  "marketplace double-fire guard" (added in v0.1.2) exited every hook with
  `sys.exit(0)` when `CLAUDE_PLUGIN_ROOT` resolved to the marketplace path — the
  sole hook-invocation path on `source: directory` installs — silently disabling
  the entire hook layer (auto-surfacing, write-nudge, lesson tripwires,
  presence/time-bar, daemon-starter). The double-fire it targeted had already
  been resolved structurally (user-level + plugin dual hook registration), so the
  guard is removed entirely. Affects v0.1.2 and v0.1.3; v0.1.1 and earlier are
  unaffected.

---

## [v0.1.3] — 2026-06-26

Finalizes the `v0.1.3-rc2` dogfood candidate with four sleep/dream-consolidation
fixes. (rc1/rc2 carried the broader feature wave of this cycle; this final adds the
consolidation hardening below. Larger forum-based multi-agent coordination work is
held for v0.2.0.)

### Fixed

- **Sleep cycle — never skip the dream fairies.** Phase B's dream-fairy dispatch is
  now an all-or-nothing **MANDATORY** invariant: the token-economy lever is
  *compact-or-not*, never *dream-or-not*. Closes the rationalizations that let a
  parent skip the fresh-cohort consolidation pass for "context economy" — a permanent
  loss of window-scoped principle-edges that no later cycle can backfill. Adds a
  Step-8 precondition (the dream-master is spawned *with* the full fairy bundle, never
  *instead of* it). (#1427, #1429, #1461)
- **Dream-fairy provenance check** no longer flags seed-graph nodes as missing
  provenance — a false positive on the immutable seed cohort. (#1430, closes #669)

<!--
At cut time: remove any placeholder entries before cutting; subsections with no
entries can be dropped.

Replace `[Unreleased]` with the version tag and date, then add a fresh
`## [Unreleased]` heading above it. Example after first cut:

## [Unreleased]
...

## [0.1.0-alpha] — 2026-MM-DD

### Added
- ...

### Changed
- ...

### Fixed
- ...

(Categories with no entries can be dropped.)
-->

