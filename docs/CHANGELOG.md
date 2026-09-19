# Changelog

All notable changes to ENGRAM Alpha are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
with an `-alpha` suffix during the alpha phase.


## [Unreleased]

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

