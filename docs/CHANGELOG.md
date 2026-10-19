# Changelog

All notable changes to ENGRAM Alpha are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
with an `-alpha` suffix during the alpha phase.


## [Unreleased]

## [v0.5.3] - 2026-10-15

### Substrate (recall, forgetting, search, graph integrity)

- **Write tools say so when a recall_summary runs over the target (#3726, part
  of #3537).** When the stripped summary is over 140 chars (and under the hard
  reject), the write response carries `recall_summary_overrun` naming the
  length; it is absent at or under 140. `engram_add_observation` (and
  `engram_add_observation_batch`, per item), `engram_derive`,
  `engram_add_definition`, `engram_add_conjecture` and `engram_add_lesson`
  all attach it. Advisory only: the node is still created, flagged
  `needs_tightening`. Known limit: it is not attached on the post-commit
  durability-failure responses.

- **The remaining `json_extract(metadata, ...)` read sites now guard with
  `json_valid` too (#3485, follow-up to #3425).** Measured: one non-JSON blob
  in `nodes.metadata` made BOTH lesson-tripwire loaders (`load_tripwires`,
  `load_prevention_lessons`) return `[]`, because the query aborted with
  `malformed JSON` and the loaders' `except Exception: return []` swallowed it
  — so one bad row silenced the tripwire layer for every lesson on every
  prompt. Both hook queries and the `reconcile_lesson_demotions` lookup now
  read through `json_extract(CASE WHEN json_valid(metadata) THEN metadata END,
  ...)`: a bad blob reads as NULL and only that row drops out (the reconcile
  tool skips it like a missing row). `engramd_read_templates.json` was
  regenerated with `tools/engramd_read_scan.py` (the two hook entries; it is
  generated from the hook source, not a separate site). A new drift guard,
  `tests/test_json_extract_metadata_guard_3485.py`, fails on any SQL JSON
  function or `->` / `->>` applied to an unguarded `metadata` column anywhere
  under `src/engram/`, repo-root `tools/`, or the templates file.

- **`nodes.metadata` must now be valid JSON or NULL (#3425).** Migration rung
  7 first quarantines every existing non-JSON blob (raw copy into the new
  `metadata_quarantine` table, the node's `metadata` set to NULL, nothing else
  on the node changed), then installs two triggers that refuse a non-JSON
  `INSERT` / `UPDATE OF metadata` with `nodes.metadata must be valid JSON or
  NULL`. Valid JSON of any shape (including non-objects) is still allowed.
  `engram_diagnose` reports a `metadata_quarantine` finding (count, 0 when
  none) plus `live_invalid_count` (rows invalid right now); either being
  non-zero flares, and is the signal to read the raw blobs. The six SQL
  `json_extract(metadata, ...)` read sites also guard with `json_valid`, so a
  blob that slips past the triggers (restore of an old dump, a hand edit) reads
  as NULL instead of aborting the query. The whole rung is one transaction
  (the quarantine INSERT/UPDATE open it, so the `CREATE TRIGGER`s join it); a
  fault rolls back to user_version 6. Only the empty `metadata_quarantine`
  table can survive a failed rung (`CREATE TABLE IF NOT EXISTS` autocommits;
  harmless, diagnose reports 0).
  Known limits:
  - `engram-pkg scope-export` on a PRE-rung-7 source DB that still holds an
    invalid row aborts with an uncaught `IntegrityError` (the `INSERT OR
    REPLACE` hits the guard), after `cmd_init` already created the package
    directory.
  - Metadata containing NaN/Infinity floats (`json.dumps` emits them,
    `json_valid` rejects them) is now refused at write; no current writer was
    found to produce one.
  - `json_valid` accepts invalid UTF-8 text, which the guard and the read
    belt cannot see (pre-existing; unreachable through Python writers).

- **Evidence nodes are out of search and out of the tier-2 budget (PR #3610,
  part of #2005).** Lei's ruling: once evidence is out of search results there
  is no reason to count it toward a budget that exists purely for search
  efficiency. One rule in `engram_core` (`SEARCH_EXCLUDED_TYPES`) now drives
  the tier threshold, the FTS and semantic search legs, the similarity hints
  and the attached-pack search. Measured on one graph near the cap:
  `tier2_max_nodes` was 10000 with about 20 nodes of headroom, 2,248 nodes were
  evidence, 27.5% of them ever recalled against 61% of other nodes, mean
  utility 0.002 against 0.166; the searchable tier-2 population drops to about
  7,734. `engram_inspect`, `engram_get_subgraph`, `engram_list` (including
  `types=evidence`), cascade walks and quote verification are unchanged, and
  `engram_query(types="evidence")` now returns nothing, with a note saying
  evidence is excluded from search. The first checkpoint after deploy shows a
  one-time continuity dip as evidence ids leave the set.

- **Every in-mind, shown-relevant use of a memory now refreshes it (PR #3606,
  part of #2005).** The forgetting formula stays recency-only (Lei's ruling);
  a new stdlib module, `engram_restamp.py`, holds the one restamp, the use
  alpha table and `RESTAMP_ACTIONS` (derive, supersede, contradict, resolve,
  register_exemplar, lesson_incident, citation, focus, focus_load, history,
  mention). The
  mention stop hook restamps the ids it credits. Behaviour changes to note:
  dream-cycle writes now refresh their premises, and if the shared module
  fails to import in the hook the mention bump is lost for that turn (logged
  as `bump_utility failed`, never silent).

- **`engram_sign` / `tools/engram-sign.py`: one lineage-transparent signature
  line for every external communication (closes #3450 via PR #3454).** Every
  field is read mechanically, never typed (name, model and its id, ENGRAM
  version and deploy tag, whose agent). It refuses, exit 2, naming the field,
  when an identity key such as `signature_emoji` is unset.

- **`cites` now means observation -> evidence only (PR #3460 and PR #3470,
  part of #2243).** One guarded writer, `_insert_cites_edge`, is the only code
  that can write `cites`; it refuses a non-`ev_` target and a non-observation
  source. Every writer that took `context_ids` / `implements_ids` now writes
  `about`. `engram_retype_edge` can move a loose `cites` to every relation that
  can be added after creation (`about`, `exemplifies`, `generalizes`,
  `instantiates`, `serves`, `subtask_of`, `tensions`) under one shared
  endpoint guard; moves into provenance relations stay refused.

- **`tools/cites_step3.py`: a per-seat migration of loose `cites` edges (PRs
  #3476, #3487, #3532, part of #2243).** Dry run is the default and writes
  nothing; `--apply` needs the seat's sampling report, per-edge labels and a
  hand check. Phase f retypes every loose `cites` whose source is non-current
  to `about` with reason `frozen-history` (a colleague's run on a copy of her
  graph measured 139 such edges no phase selected). A generalization or
  corroboration derivation source is listed as hand-only, never refiled by
  the tool: wiring a non-case premise into one inflated a derivation from
  0.777 to 0.941 on one graph. Developer tier.

- **DAG acyclicity is checked per relation family (closes #3210 via PR
  #3444).** `EDGE_CLASSIFICATIONS` carries `dag_family` (`grounding` or
  `abstraction`); `check_dag_conformance` walks each family separately and
  reports one cycle per back edge, the ack writer guards grounding, and the
  audit reports all cycles. A substrate change that Lei reads before merge.

- **`engram_list` reaches `nodes.metadata` (closes #3402 via PR #3409), and
  `engram_query` takes `refresh_recall=false` (PR #3367, part of #3362).**
  `metadata.<key>` is a filter field and a projection (top-level key, pattern
  `^[A-Za-z_][A-Za-z0-9_]{0,63}$`, read through the guarded `json_extract`);
  `engram_inspect` gains a `support_lost` recall view. With `refresh_recall`
  false the call ranks and returns as usual but writes no `importance_score`
  / `recall_turn` / `recall_count`, so a probe cannot inflate what it
  surfaces; the default response shape is byte-identical.

- **`cornerstone_candidate` ranks by citations gained in a recent window
  (closes #3154 via PR #3370, docs in PR #3373).** The old score was
  age-biased twice (a saturated `importance_base`, plus a storage-order
  `LIMIT 50` that kept the oldest rows). It now uses `recent_window_days`
  per preset (30 / 60 / 120) with a gate of at least 3 recent citations, and
  a deterministic order. One colleague's graph measured 1 result against 15
  before; the dream-fairy spec now says a thin result is normal and must not
  be padded.

- **A rejected observation or evidence entry leaves nothing behind.** A
  rejected observation no longer leaves an evidence row (closes #3399 via PR
  #3401, single and batch paths). `file://` evidence must name a regular file
  at every entry path: directories, devices, FIFOs and a cross-repo tree are
  rejected by name, including on `engram_add_evidence`, which used to accept
  a directory outside any repo (closes #3393 via PR #3397). The
  committed-file check resolves symlinks, so a committed file cited through a
  symlinked path (macOS `/var` to `/private/var`) is no longer rejected (PR
  #3386, part of #3292). `tools/verify_quote.py` rejects a non-regular file
  before `open()`: a directory no longer crashes it and a FIFO no longer
  hangs it; exit 2 (closes #3400 via PR #3407).

- **Contradiction and FTS fixes.** The obsolescence scan, reflect and the
  dream-fairy fallback agree on one set of unsettled contradiction statuses;
  before, the fallback filtered on `open` and returned 0 rows on a live
  graph (closes #3355 via PR #3408). `rebuild_fts_index` excludes superseded
  rows, matching the sync triggers, and migration rung 4 stops double
  deleting (closes #2746 via PR #3515).

- **Embeddings are mandatory and failures are loud (#345).** The server
  refuses to start, exit 1 with a message naming the key, when
  `embedding.enabled` is falsy, and the toggle is retired (PR #3339). The
  `ENGRAM_NO_EMBEDDINGS=1` bypass now prints one stderr WARNING and its
  docstring says it is test/dev scaffolding (part of #3350, PR #3351). A
  write-time embed failure stores the node, flags it, reports it in the
  response (`embedding_failure`, plus `prediction_embedding_failure` for a
  prediction) and revives the model once in-process (PR #3343, PR #3352).
  Sleep gains a backfill step and `tools/engram-regenerate-embeddings.py
  --json` prints `{before_null, embedded, failed, after_null}` (PR #3332).
  Each observation is stamped once instead of twice, which also removes an
  extra embed and a duplicate "created" `edit_history` row per write (closes
  #3353 via PR #3354).

- **`nodes.metadata` parse sites go through one helper (closes #3413 and
  #3424 via PR #3423), with follow-ups in PR #3449 and PR #3484.** An AST
  census of every `json.loads` over a metadata value in `src/engram/**` found
  62 sites, 49 of them reachable crashes on valid-but-non-object JSON; all go
  through `_metadata_dict` now and an AST drift guard holds the line. PR
  #3449 tightens that guard's LOCAL check and makes `detect_speaker` survive
  `None` / invalid / empty person metadata. PR #3484 adds the write guard
  (`nodes.metadata` must be valid JSON or NULL) with migration rung 7
  quarantining existing invalid values (part of #3425); PR #3502 guards the
  remaining `json_extract(metadata, ...)` read sites with `json_valid`
  (closes #3485). Those two changes are the entries that open this section,
  which cite them by issue number.

- **One path for trust-tier changes (PR #3297, part of #1825).**
  `engram_set_trust_tier` is the only writer: `update_person` and `add_person`
  with `trust_tier="primary_user"` are refused, as is `trust_tier="self"` on a
  node without `is_self`. Measured before the fix: four bypasses on dev.

- **Smaller substrate fixes.** A durable goal tension now rebuilds
  `principle_triggers.json` (closes #1721 via PR #3260). Nested `*_json`
  sub-fields sent as a real array or object return a structured error instead
  of an uncaught `TypeError` (measured: `{"observations_json": []}` on
  `engram_add_observation_batch`; closes #1753 via PR #3262).
  `migrate_person_nodes` checks `is_self` before tier inference and flags the
  legacy `role` key (closes #1665 via PR #3259). `write_durable_snapshot`
  backs up from a fresh read-only connection, so an uncommitted write on the
  caller's connection can no longer make the backup hang silently (closes
  #3232 via PR #3273).

- **`recall_summary_overrun` is documented in the write-tool descriptions,
  observe skill and changelog (#3731, part of #3537).** Docs and docstrings
  only.

- **`idx_nodes_created_at` via migration rung 8 (closes #3766 via PR #3770).**
  The two U9b checkpoint statements now SEARCH an index instead of scanning
  every row.

- **A cross-process `flock` guards the one-time DB setup (closes #3771 via PR
  #3774, part of #3767).** Two processes starting together could run
  migrations and backfills at once.

- **The setup lock rechecks its inode after the `flock` (#3798, part of
  #3791).** A waiter whose lock file was deleted under it re-opens instead of
  holding a dead lock.

- **The DB setup lock is a `0700` directory, not a file (closes #3791 via PR
  #3800).** `<db>.setup.lockdir` replaces `<db>.setup.lock`; the hard-link
  refusal and its delete remedy are gone.

- **`bears_on`, a question-to-claim relation (#3618, part of #3615).** A
  non-cascade edge saying that once a question is answered, the answer counts
  for or against a claim; ask, resolve and list surface it.

- **`bears_on` hints whether it still holds toward a replacement (#3641, part
  of #3625).** On supersede, retract-with-replacement and resolve, the agent
  is prompted to re-point the relation.

- **`_relation_endpoint_error` moves into `engram_core` (closes #3631 via PR
  #3649).** One shared endpoint-type rule for `instantiates`, `generalizes`
  and `bears_on`; three doc notes from the #3641 review are folded in.

- **A confidence recompute after an acknowledgement no longer double-counts
  the superseded premise (closes #3531 via PR #3662).** It read both the
  original and the `acknowledged_derives_from` edge.

- **`config.json` writes are atomic, and node creation fails loud on an
  unreadable config (closes #3607 via PR #3623).** `_set_current_turn` used to
  truncate the file on every turn advance.

- **Health reads no longer depend on the caller's `row_factory` (closes #3128
  via PR #3617).** Functions in `src/engram/health/` read columns by name and
  broke on a plain `sqlite3.connect`.

- **`embedding.device` (default `cpu`) and `embedding.cpu_threads` (default 4)
  apply at every model load (#3905, closes #3898).** ENGRAM loads
  `all-MiniLM-L6-v2` through `SentenceTransformer`, which silently took CUDA
  whenever a CUDA device existed (about 750 MB of VRAM per process, no latency
  gain for single-query recall). `embedding.device` is `cpu`, `cuda` or
  `auto`; `auto` is exactly the old behaviour. On `cpu` the load path never
  touches CUDA (the roughly 610 MB is the CUDA context itself). `cuda` with no
  visible CUDA device loads on CPU and prints one stderr line, and an invalid
  value also loads on CPU with one stderr line naming the value and the
  allowed ones; embeddings never turn off silently. `embedding.cpu_threads`
  (an integer of at least 1) is applied after any load that ends on CPU; an
  invalid runtime value falls back to 4 with one stderr line. Both keys are
  read at load time through the same accessor as `embedding.model`, so a
  config edit applies on the next load, and both are registered in
  `tools/config_schema.py` (editable, no restart, tier B), listed in
  `README-AGENT.md` and in `templates/config.json.example`. The polarity
  cross-encoder follows `embedding.device`; no `polarity.device` key exists.
  Behaviour change on Apple silicon: under `auto` the library loaded on
  `mps:0`, so the new default moves a Mac seat to CPU. Measured by the
  colleague on a Mac: single query 2.4 ms on cpu against 5.2 ms on mps, batch
  of 50 13.9 ms against 20.8 ms, parity minimum cosine 0.99999994 over 20
  sentences. Known limits: there are no CPU numbers for the polarity
  cross-encoder (its model was not cached on the builder's seat), and
  `src/forum/embeddings.py` still loads on whatever device the library picks
  (sibling issue #3899).

### Daemon, owner and read registry (EngramManager)

- **The surface daemon's startup gate no longer unlinks a socket another live
  process holds (closes #3361 via PR #3490), and a refusal waits before
  exiting (PR #3491).** When the recorded pid is dead the gate asks who holds
  the path (`/proc/net/unix` path-text match, never substring) and unlinks
  only on a positive "nobody": exit 75 means held, 76 means unproven. A
  socket-holder refusal sleeps 60 s so supervisors do not hot-loop. The new
  operator verb `clear-stale-engram-daemon.sh` clears a crash-stale socket the
  gate refuses to unlink, as a deliberate human decision with no heuristic
  (closes #3493 via PR #3517). A later test fix replaces `select()` with
  `selectors` in the backoff test, which failed above fd 1023 (closes #3529
  via PR #3530).

- **The daemon-hook socket-ownership guard fails closed on macOS (PR #3359,
  part of #3333).** BSD `stat` has no `-c`, so both identity snapshots came
  back empty and `rm -f` removed the socket and pidfile despite a live
  holder. It now tries GNU then BSD `stat`, treats an empty snapshot for an
  existing path as not-nothing-on-disk, and falls back to `lsof` for the
  holder check where `/proc/net/unix` is absent.

- **`setup-surface-daemon-service.sh` refuses a temp-dir or worktree daemon
  path (PR #3545, part of #3519).** On 2026-10-03 a test run from a fairy
  worktree registered a real launchd job under the production label whose
  program was a deleted worktree's script; it respawned 27,605 times.

- **Owner hardening (closes #3240 via PR #3285).** Pid identity survives pid
  reuse and clock drift through one shared `engram_pidfile.py` helper, the
  owner checks whether it has been displaced, and the destination-rights
  check is pinned. `EngramdOwner.start()` fails closed on Python older than
  3.11 with a named `OwnerPythonTooOld` (closes #3349 via PR #3369), and one
  `platform_unsupported_reason()` is shared by the refusal and the test
  marker (PR #3375, part of #3292).

- **Read-template registry, tier 1 (closes #3196 via PR #3321, part of
  #2652).** The owner admits a client read only if the statement's exact
  bytes were registered at build time. `generalization_chain` hydration
  moved to fixed `json_each` templates (closes #3324 via PR #3326); the
  committed scan keys sites on `file:qualname` instead of `file:LINE` (closes
  #3342 via PR #3345); the two auto-sleep hook functions are registered in
  the scan roots (PR #3385); session start gains a named JSON1 probe in
  substrate health (closes #3327 via PR #3329); the design doc's U4 scope
  note was corrected after a re-measure (PR #3293). The user-identity hook can
  read person nodes through the owner behind a default-off flag (PR #3341,
  part of #3197); the cross-uid rejection control behind its ship gate
  became a real test later (PR #3421, see Tests and CI).

- **The owner serves document frequencies as a typed `df` op (#3733, part of
  #3665).** `df` reads `nodes_fts_vocab`; the client-read floor now allows
  `PRAGMA data_version` in its read form only.

- **The generalization chain walk is one typed `chain` owner op (#3744, part
  of #3740).** The lesson-tripwire hook sends one op per hit instead of
  walking the chain itself, to pass the A6 latency gate on the production
  build.

- **The owner refuses to start on a store behind the schema (closes #3753 and
  #3785 via PR #3776).** One `LATEST_SCHEMA_VERSION` constant replaces the
  function-local copy, and a refused load is recorded as `refused_at_load`.

- **A FIFO at the pidfile path reads as no live pid instead of hanging (#3772,
  part of #3768).** `recorded_live_pid` no longer blocks on a plain `open()`
  of a non-regular file.

- **The owner refuses to start on a non-regular pidfile and never writes
  through a symlink (#3777, part of #3768).** A FIFO at `engramd.pid` used to
  hang `start()` at the pidfile write.

- **One shared pidfile write serves the owner and both collab tools (#3779,
  part of #3768, addresses #3773).** `monitor-supervisor.py` and
  `codex-umwelt-wake.py` no longer hang on a FIFO pidfile or status file, and
  no longer truncate a symlink or hard link.

- **The owner launcher's macOS refusals give the true reason (#3769, part of
  #3709).** They no longer blame the permission layer (#3194); the real cause
  was that no macOS launcher existed yet.

- **A macOS launchd launcher for the owner (closes #3709 via PR #3778).** One
  LaunchAgent per `ENGRAM_HOME`; `setup-surface-daemon-service.sh --owner` now
  works on a Mac instead of refusing.

- **The launcher names rc 125 as `no_gui_domain` and refuses control bytes as
  a class (closes #3780 via PR #3789).** A Mac account with no GUI login
  session now reports its own state instead of a generic failure.

- **The launcher handles non-ASCII paths under stock macOS bash 3.2 (closes
  #3790 and #3794 via PR #3799).** Path captures, the `printf -v` name guard
  and `LC_ALL=C sed` are fixed, and the test harness decodes UTF-8.

- **`codex-wake` and `monitor-supervisor` refuse non-regular paths by class
  (closes #3781 via PR #3792).** A FIFO, directory, socket, symlink or hard
  link at the log, state file, status temp or `start` path is refused instead
  of blocking or truncating a target.

- **The `codex-umwelt-wake` watcher survives bad paths (closes #3793, #3795
  and #3796 via PR #3803).** It no longer dies on a log write error, no longer
  prints a `cp` that blocks, and no longer blocks on a FIFO config or session
  marker.

- **A `systemd --user` launcher for the owner, a gated SessionStart fallback
  and one owner-flag helper (closes #3663 via PR #3672, part of #2652).**
  Until now nothing started the owner except by hand; no flag is turned on by
  this unit.

- **Owner per-call floor: stage profiler, A6 installed layout, thread reuse
  and serialize-once (closes #3678 via PR #3686, part of #2652).** U7 of the
  epic: profile the round trip, then cut what the profile names.

- **The owner's accept backlog is `SOMAXCONN`, and a full backlog reads as
  busy-retry (closes #3680 via PR #3690).** More than 5 simultaneous connects
  used to read as "restart the owner".

- **An over-long socket path is refused before any mutation, with the same
  reason on client and owner (closes #3696 via PR #3699, part of #2652).**
  Linux half.

- **The owner reads the `run/` ACL back and refuses to start if any entry
  remains (closes #3698 via PR #3701, part of #2652).** Linux half.

- **Darwin arm of the owner permission layer (closes #3666 via PR #3703, part
  of #2652).** The owner starts on a Mac instead of refusing with
  `OwnerPermissionUnsupported`.

- **One helper decides the owner's permission arm (closes #3708 via PR #3711,
  part of #2652).** `main()` exits cleanly on an unsupported seat, and a hung
  `setfacl` refuses by name.

- **`platform_arm()` is the one platform discriminator for owner and client
  (closes #3712 via PR #3713, part of #2652).** The client's socket-path limit
  no longer keys off `sys.platform`.

- **Darwin arm for the pid-identity check (closes #3714 via PR #3715, part of
  #2652).** `recorded_pid_is_alive` reads libproc start time and
  `KERN_PROCARGS2` argv, so a reused pid no longer reads as a live owner on a
  Mac.

- **On Darwin a refused `connect()` with a live owner is `busy_or_stuck`, not
  down (closes #3707 via PR #3716, part of #2652).** Reason codes are now by
  meaning on every `Unavailable`.

- **`engramd_client._call` never raises (closes #3717 via PR #3718, part of
  #2652).** Socket-creation failure is `local` and an unserializable request
  is `Rejected`.

- **The owner launcher asks the socket before it calls a live pid a serving
  owner (#3825, part of #3736).** `start-engramd-owner.sh` judged health from
  `engramd.pid` alone, so "alive" meant only that the process existed, and a
  fresh launch printed `UNSUPERVISED: engramd_owner launched (pid N)` without
  checking. A new `serving` subcommand in `_engramd_owner_hook_io.py` sends a
  `ping` (the one method that touches no store) and answers `serving`,
  `notserving:<what was observed>` or `ambiguous:<why>`. The whole probe is
  bounded: 1.5 s in total, 0.5 s per call, 0.25 s between polls. On
  `notserving` the launcher reports and launches nothing, because a second
  owner against a live pid is the hazard; it re-reads liveness first, so a
  process that died during the probe never gets the not-serving line. Every
  path still exits 0. Known limit: `notserving:` is a budgeted claim ("no
  healthy ping answer within the window"), not proof that the owner does not
  serve.

- **No GUI login session on a Mac: the owner launcher launches unsupervised or
  refuses loudly (#3826, closes #3823, part of #2652).** On a Mac where
  launchd has no `gui/<uid>` domain and an owner flag is ON,
  `start-engramd-owner.sh` now launches the owner unsupervised, as on Linux
  without a user bus, and prints one `UNSUPERVISED:` line with the reason
  `launchd: no GUI domain`. If that launch fails (it cannot spawn, the owner
  dies within about a second, or `engramd_owner.py` is missing) it prints one
  line led by `OWNER REFUSED: no launchd GUI domain`. That line names every ON
  owner flag, the recovery (set those flags false in `config.json`, or log in
  to a GUI session) and that the flagged readers will block. The old "not used
  on this seat" line, which launched nothing, is gone. With every flag OFF, or
  with a GUI domain, nothing changes. `engramd_owner_flags.py` gains
  `--names`, a sorted comma list of the ON flags. Known limit: the launchd
  user-domain alternative was not tried; it is a separate unit.

- **The surface hook reads through the owner behind
  `engramd.surface_reads_via_owner`, default OFF (U10 of #2652; #3827, closes
  #3821).** Seven of the hook's eight store sites move: the embedding-restore
  flag, the dormant focus-set candidates, the triggered-principles section
  (the typed `chain` op), the keyword-prepend check (a COUNT template plus the
  typed `df` op), the focused ids, the FTS fallback and the liveness check.
  The eighth, the person-moment cursor advance (the only read-write open),
  stays direct on both flag values. With the flag ON the FTS fallback is not
  taken (a down owner gives the existing daemon-down notice and no recall
  block), and liveness is derived from the fire's own restore-flag read, so
  there is no extra round trip: after a failed read one `ping` tells "owner
  down" from "owner up but the store did not serve the read", and each refusal
  kind names itself (owner older than the hook, schema skew, unreadable
  store). Owner down is a typed `Unavailable`, never a stale or empty default,
  and there is no direct fallback. `CONFIG_PATH` is now the surface hook's
  single config source, and a `_focused_ids` failure is no longer silent: with
  the flag ON the surface and lesson-tripwire hooks each record one
  owner-failure line (fail-open is unchanged). Measured on a copy of a real
  store (N=300, sequential p50, owner over direct): 0.93x to 0.94x; a
  synthetic store read 1.14x (#3834).

- **`reconcile_lesson_demotions` reads through the owner behind
  `engramd.reconcile_demotions_reads_via_owner`, default OFF (U12 of #2652;
  #3828, closes #3824).** With the flag ON the per-lesson `prevention_type`
  read goes to the owner and the tool opens the store zero times; the owner's
  `ping` replaces the `db_path.exists()` check. Owner down is an error, never
  `[]`, and the three owner-side refusals get three messages because the
  remedies differ: down (start the owner), `unregistered_template` (the
  running owner is older than the tool; restart it on the current build) and
  `refused_at_load` (the store failed classification; migrate it). Flag ON
  never falls back to a direct open. An explicit `--db` naming another store
  keeps the direct path. Flag OFF is unchanged. This is the first `tools/`
  unit.

- **The owner unit restarts without limit, with bounded backoff, and never
  lands in a permanent `failed` (#3835, closes #3831, part of #2652).**
  `engramd-owner.service.template` shipped `StartLimitIntervalSec=300` with
  `StartLimitBurst=5`, so on a seat with any `engramd.*_via_owner` flag ON a
  crash loop hit the start limit and the unit went permanently `failed`, after
  which every stop blocked until someone ran `systemctl --user reset-failed`.
  Measured on Linux with systemd 255: the owner was down for good about 40 to
  50 s after the first failure. The template now sets
  `StartLimitIntervalSec=0`, drops `StartLimitBurst`, and adds
  `RestartSteps=5` and `RestartMaxDelaySec=300s`; `RestartPreventExitStatus=4`
  is unchanged, so a planted non-regular path is still never retried. macOS
  had no such limit (launchd's `KeepAlive` throttles and keeps trying), so
  this was also a platform-parity defect. Because a persistent fault now reads
  `activating (auto-restart)` forever, a quieter failure than `failed`,
  `start-engramd-owner.sh` reads the unit state in one `systemctl show` call
  and prints one bounded line with the restart count, the exit the record
  actually holds, what it means and the remedy. The line is chosen by state
  first and restart count second, so a unit that is merely starting is not
  told it has restarted, and a failed state read falls back to the previous
  message.

- **A supervised owner unit is probed too when no live owner is recorded
  (#3854, closes #3849, part of #3736).** Step 4 of `start-engramd-owner.sh`
  used to exit 0 for a Linux `active` unit or a macOS `running` job without
  asking the socket. This is a residual gap, not the common case: an owner
  that wrote its pidfile is already probed in step 3. These arms are reached
  only when the unit or job is up but no live owner is recorded (the owner is
  still starting, the pidfile is removed or stale, or the unit's process is
  not an `engramd_owner`). Both arms now ask the existing `_probe_serving`
  through one helper. On `notserving:`, with a liveness re-read that says
  exactly `none`, the launcher prints one line and launches nothing. The line
  never says "alive", says the socket did not serve within the probe window
  and the owner may still be starting, and names the restart remedy
  (`systemctl --user restart <unit>` or `launchctl kickstart -k
  gui/<uid>/<label>`) only if this persists across session starts. A `serving`
  or `ambiguous:` answer, or a re-read that finds the owner alive, keeps
  today's behaviour.

- **The owner's first write: a typed `use_reward` op (U11 Stage A; #3839, part
  of #3822).** Owner side only; the hook that calls it is #3847. The op takes
  `{node_ids, action, request_id}`, where `action` is a closed enum (today
  `mention`). The client supplies no alpha and no config path: the owner
  derives alpha from its own `config.json` and runs the shared
  `engram_restamp.use_reward`, so the bump and the recall restamp both happen
  (the issue's original bump-only op would have silently stopped the restamp).
  The op runs as one transaction under the owner's lock, then compares the
  values it expected with a re-read after COMMIT for all three columns; a
  difference answers `readback_mismatch` with `commit:
  "committed_unverified"`. `request_id` dedupes in memory (LRU, capped at
  1024), and the same id with a different payload is refused. Every owner
  method now carries a kind, `read` or `write`; the write set is
  `{use_reward}` and an unclassified method is refused by default. A
  restrictive, column-exact authorizer is installed for the op, and a denial
  refuses the whole op (`write_denied`, rolled back, no partial bump). The
  contract version moves 1.1 to 1.2 (additive). One total wait budget
  (`USE_REWARD_LOCK_WAIT`, 1.0 s) covers the owner lock plus a competing
  direct writer, and when it runs out the owner answers a typed
  `Rejected(busy)` with nothing committed. SQLite's own busy handler
  overshoots its timeout (measured on macOS, SQLite 3.53.4: `busy_timeout` 100
  ms waited 633 ms, 300 ms waited 1391 ms, 1000 ms waited 2796 ms), so the op
  sets `busy_timeout` to 0 and retries every 20 ms against its own budget.
  Known limits: `Unavailable(timeout|protocol)` on a write is indeterminate,
  not failed, and the remedy is one retry with the same `request_id`; an owner
  restart between COMMIT and the reply, followed by a retry, can double-bump
  (a small monotone over-credit); past the cap of 1024 the oldest id is
  forgotten.

- **The utility-credit Stop hook writes through the owner's `use_reward`,
  behind `engramd.utility_credit_writes_via_owner`, default OFF (U11 Stage B;
  #3847, part of #3822).** With the flag OFF the hook is today's code path.
  With it ON the hook sends exactly `{node_ids, action: "mention",
  request_id}`, never opens the store and never falls back to it; every
  non-success is a loud, logged loss. The time budget fits the hook's `timeout
  5`: 3.5 s for the owner write plus a 1.5 s reserve. A first attempt that
  comes back `Unavailable(timeout|protocol)` is indeterminate, so it is
  retried once with the same `request_id` if enough time is left, and the log
  then says the first attempt may have been applied once, never "not applied".
  `readback_mismatch` means committed but unverified and is not repeated
  blindly; an older owner gets a typed message naming the contract skew; an
  owner-reported `restamp_error` is logged and the bump stands. An explicit
  `alpha=` or `restamp=False` with the flag ON raises `ValueError` rather than
  write something else.

- **A refused peer's request is drained before the owner closes, and each
  refusal logs one line (#3857, closes #3845, part of #2652).** When the owner
  refuses a peer on its credentials it used to send the typed rejection and
  close at once, with the request unread. Measured on Linux, forced to refuse
  (30 trials each): the rejection is delivered, but a client that sends first
  and then reads to EOF gets the reply and then `ConnectionResetError`
  (30/30), while the shipped client, which stops at the newline, is
  unaffected. `shutdown(SHUT_WR)` alone did not remove the error; shutdown
  plus a bounded drain gave a clean read (30/30). Now the owner sends the
  typed rejection (wire shape unchanged), shuts down its write side, reads the
  peer's request until EOF, a time budget or the request-size cap, discarding
  it without ever parsing it, then closes. It also logs one WARNING per peer
  uid per window naming the peer uid, the owner uid and the reason, never any
  request bytes. The credential check, the rejection and the same-uid path are
  unchanged.

- **`advance_moment_cursor`: one shared compare-and-set for hook, server and
  owner, behind `engramd.surface_writes_via_owner`, default OFF (U13 of #2652;
  #3859, part of #3856).** `metadata.special_moment_cursor` had two writers,
  the surface hook and `engram_query`, each rewriting the whole metadata dict,
  so a concurrent `engram_update_person` or `engram_add_special_moment` edit
  could be lost, or the advance could. Both copies are replaced by one
  statement in the new `engram_moment_cursor.py` that sets only
  `special_moment_cursor` through `json_set` and never rewrites another key.
  The same statement is the owner's second write op (kind `write`, contract
  1.2 to 1.3; an older owner answers `Rejected(unknown_operation)`). The
  cursor rule has a range window (an int, not a bool, in `[-(2**53), 2**53)`,
  else 0), because SQLite's `json_type` and `json_extract` disagree on a
  stored `2**63`. The `USE_REWARD_*` timeout constants were renamed
  `OWNER_WRITE_*`, values unchanged. With the flag ON the hook makes one
  attempt and does not retry: the cursor write comes after render, and a lost
  advance only means the same moment may repeat sooner. The flag stays OFF on
  every seat until it is switched on deliberately. Known limit: a direct
  writer advancing between the owner's COMMIT and its read-back reads as
  `readback_mismatch`; the reply tells the caller to re-read before repeating.

- **The moment-cursor SQL uses the one guard idiom, and the metadata-guard
  gate sees a cut-off first argument (#3866, closes #3865; #3868, closes
  #3867).** The nightly went red because five `json_*` sites in
  `engram_moment_cursor.py` used a nested guard that the #3485 gate does not
  recognise. `CURSOR_SQL_EXPR` and `_IS_OBJECT_SQL` now take `CASE WHEN
  json_valid(metadata) THEN metadata END` as the first argument of every
  `json_*` call, and `CURSOR_GUARDED_SQL_EXPR` is deleted, leaving one cursor
  definition. The UPDATE's `json_set` is wrapped too; for the rows the WHERE
  clause admits (valid objects) nothing changes, and the object test is now
  the only term that excludes non-objects, so it must stay. A variant
  whose first argument was spliced in by string concatenation and came out
  bare passed the gate while 19 behavioural cells went red, so the gate gains
  one rule: a json-function call whose first
  argument runs to the end of its string piece is a site ("first argument not
  visible in the literal"), and the module spells the wrap literally at its
  five call sites. The ratchet stays 0 with no allow-list entry.

- **`cohort_dispatch` reads through the owner behind
  `engramd.cohort_dispatch_reads_via_owner`, default OFF; one shared
  owner-read helper; one `ENGRAM_HOME` resolution (U14 of #2652; #3869, closes
  #3862).** `_fetch_nodes` (it runs in every sleep, via `prepare` and
  `verify-in`) now uses the registry's one admitted form, `WHERE id IN (SELECT
  value FROM json_each(?))`, which also removes the 999-variable ceiling (a
  1,200-id cell shows it). With the flag ON, an owner that is down, refusing
  or malformed raises `CohortOwnerReadError`: no empty list, no partial list,
  no direct fallback. `prepare` exits non-zero and writes no chunk, and
  `verify-in` reports one `db_access` error naming the owner instead of one
  "not found in DB" per node. The direct path now opens `mode=ro` on both flag
  values (the old open had no `mode=ro`, so the docstring's "read-only" was
  untrue). The flag applies only when `--db` is this home's store; any other
  `--db` stays direct. Two root-cause fixes ride along. `tools/_owner_read.py`
  now holds the owner-read machinery that the lesson-demotion tool kept
  privately: a tool describes itself with one `OwnerReadSpec` (flag key, typed
  error, home, and what an empty answer would have meant). And the store path
  has one definition: `--db` defaults to `engram_home()/knowledge.db`, and the
  sleep skill no longer passes `--db ~/.engram/knowledge.db`. Known limit: it
  is not measured whether any seat runs with `ENGRAM_HOME` other than
  `~/.engram`; if none does, the path bug was latent.

- **`cohort_dispatch verify-in` reads the owner flag once (#3877, closes
  #3871, part of #2652).** It read the flag twice per run, so the flags-bundle
  stderr line from #3869 printed twice, and the two reads of one decision
  could disagree if `config.json` changed between them. `_fetch_nodes` takes
  an optional `use_owner`; `verify-in` passes the decision it already made,
  and `prepare` is unchanged.

- **The nightly `knowledge.db` backup reads one owner snapshot behind
  `engramd.backup_reads_via_owner`, default OFF (U15 of #2652; #3876, closes
  #3872).** With the flag ON, `tools/backup_knowledge_db.py` never opens the
  live store. It asks the owner's `snapshot()` for one quiesced copy in a
  private `0700` temp dir inside `--out-dir`, reads the watermark from that
  copy, decides whether to skip, and feeds the same copy to `dump_stripped`,
  so the watermark and the archive describe one state (today they are two
  reads at two times). Every non-success from `snapshot()` is an error: exit
  1, no archive, `.watermark.json` untouched, no fallback to the live store.
  That covers an owner that is down or refusing, an indeterminate answer
  (`Unavailable`, or `Rejected(commit="indeterminate")`; the tool never looks
  at the destination after one), and a malformed `Ok`. The temp dir is removed
  on success, on skip and on any error. The flag applies only when `--db` is
  this home's store. `tools/_owner_read.py` gains `snapshot(spec, dest_path)`
  with its own `snapshot_timeout`, default 35 s; the owner's
  `SNAPSHOT_REQUEST_DEADLINE` is 30 s and the client waits past it so the
  owner's typed timeout reply arrives. Known limit: the 35 s is a judgement,
  not a measurement. `--db` now defaults to `$ENGRAM_HOME/knowledge.db` and
  `--out-dir` to `$ENGRAM_HOME/db-backup`; the second is a change, since it
  moves where archives land for anyone running with a non-default
  `ENGRAM_HOME`. With the flag ON a run briefly holds two full-store copies in
  the temp dir (the owner's snapshot, embeddings included, and
  `dump_stripped`'s own working copy), and `--dry-run` pings the owner and
  takes no snapshot.

- **`_read_install_tier` no longer blocks on a FIFO at `config.json` (#3878,
  closes #3838, part of #2652).** `EngramdOwner.start()` called it before
  serving, and it read `<ENGRAM_HOME>/config.json` with a plain
  `Path.read_text`, which blocks on a FIFO until a writer appears. Measured:
  with a FIFO at `config.json`, a whole-`start()` cell run in a child with a
  30 s timeout does not return on the old code. It now uses the non-blocking
  `engram_regular_io.read_regular_text`, with the same `(OSError, ValueError)`
  to `None` fallback.

- **The dream master's safety, coverage and null-embedding reads go through
  the owner behind `engramd.dream_safety_reads_via_owner`, default OFF (U17 of
  #2652; #3886, part of #2652).** No agent opens `knowledge.db` for these
  reads any more. `tools/dream_master_batch.py` gets a CLI with three
  subcommands and one contract: one JSON line and exit 0, or empty stdout, a
  stderr line naming the error class, and a non-zero exit. A failed read is
  never `null`, `0` or a fraction. `safety-row <node_id>` returns
  `{"is_current","status","superseded_by"}`, or `null` for a missing node.
  `coverage {recall_summary|recall_keywords}` returns the fraction that the
  health metrics compute, or the string `"UNAVAILABLE"` when no claim-bearing
  node is current. `null-embeddings --population claim_bearing_nodes` returns
  the count the health engine computes. With the flag ON each statement goes
  to the owner as one registered read template (four registered) and an owner
  that is down, refusing or malformed fails with no direct fallback; with it
  OFF the store is opened `mode=ro`. Both dream agent specs now say never to
  open `knowledge.db` yourself and to call the CLI, and the master spec adds
  that a CLI failure is not a missing node: skip the action and log `safety
  read failed`. The two coverage aggregates are computed in SQL so the owner
  returns two numbers, not every summary and keyword list. Known limit: five
  metadata shapes that `engram_core` cannot write diverge from the health
  functions, and are pinned as documented divergences.

- **`engram-regenerate-embeddings` writes through the owner behind
  `engramd.regen_embeddings_via_owner`, default OFF (U16 of #2652; #3900,
  closes #3879).** The restore-path backfill that sleep Step 3.9 runs every
  night is the first `tools/` write to move. With the flag ON the tool never
  opens the live store; it still loads the embedder and computes the vectors
  itself, so the model stays out of the owner. The owner gains two write ops,
  so the write set is now `use_reward`, `advance_moment_cursor`,
  `fill_embeddings` and `finish_embedding_regen`. `fill_embeddings` takes
  `{model, items: [{node_id, vector}]}`, 1 to 50 items, vectors of exactly 384
  numbers (finite, not bool), the model must equal the store's configured
  model, and all items are validated before any write. It writes with a
  compare-and-set per item (`... WHERE id = ? AND is_current = 1 AND embedding
  IS NULL`), stores `nodes.embedding` text byte-identical to the direct
  path's, mirrors changed rows into `vec_nodes` (if the owner cannot load vec0
  the fill still succeeds with `vec_mirrored: false`) and reads every changed
  row back. This is the owner's first extension load, of `sqlite_vec`.
  `finish_embedding_regen` rebuilds the FTS index and recounts the embeddable
  NULLs itself, clearing the restore flag only when that fresh count is 0, in
  one transaction; it takes `embedder_available` from the tool so the owner
  never imports the embedder (measured: about 4 s, then resident). Tool side:
  reads go through three registered templates, writes in batches of up to 50,
  and `after_null` is a fresh read, never `before - embedded`. An owner
  failure (down, refusing, indeterminate, malformed answer or
  `readback_mismatch`) exits **3**, distinct from exit 1 for per-node
  failures, with the reason on the last stderr line, no JSON on stdout and no
  direct connect, so sleep records `embedding backfill FAILED (exit 3):
  <reason>`. There is no retry after an indeterminate write; the fill is a
  compare-and-set, so re-running is safe. Known limit: a flag-write failure
  now rolls back the FTS rebuild and is reported, where the direct tool
  swallowed it, so the two paths differ there.

- **The launcher's race-winner path reports a live owner that is not serving
  (#3915, closes #3736).** When the launcher's own launch dies inside its
  watch, it assumes it may have lost an `OwnerAlreadyRunning` race and
  re-checks for a live pidfile for about a second. That re-check used to exit
  0 silently the moment it saw `alive`. Now it probes the socket, and if the
  answer is `notserving:` while a liveness re-read still says alive, it prints
  one budgeted line containing "alive but NOT SERVING", the observed text, the
  race provenance and the probe window; nothing is launched. A winner that
  serves stays silent, a winner that dies during the probe gets no not-serving
  line, and an `ambiguous:` answer claims nothing. With this, every launcher
  path that observes a live owner (recorded pid, launchd `running`, systemd
  `active`, the unsupervised launch watch, the race re-check) asks the socket
  first.

### Hooks, lessons, session start and agentctl

- **"Forum configured" means `forum.url` is set, everywhere (closes #3546 via
  PR #3550, closes #3552 via PR #3559).** The forum, baton and inter-agent
  prompt hooks and the `ia`, `baton` and `engram_client` CLIs used to accept
  `forum.url` OR `agent_name`, so an install with no forum server reported the
  forum as unreachable. `agent_name` is identity only now and no message
  recommends it.

- **`forum.url` has one writer and a bring-up path.** `set_forum_url.py` is
  the one tested writer, called by `install-forum-service.sh` (PR #3561, part
  of #3557). First-session Step 5.7 asks for the URL and writes it only
  through that tool (PR #3565, part of #3563); the answer reaches the block as
  a file read, never shell source, after a forged heredoc delimiter was shown
  to run lines as shell (PR #3573, part of #3571). `agentctl spawn
  --forum-url` leaves a one-line hint that Step 5.7 offers as the default
  (PR #3569, part of #3563).

- **Advisory probes say unknown, never healthy, when they could not
  measure (PR #3524, PR #3534, PR #3544; part of #3521).** Four probes return
  a tri-state and render one bounded line when unknown; the forum prompt hook
  says so when it could not read the forum or the inbox, instead of reporting
  "nothing new"; an unreadable or corrupt `config.json` is unknown, not "forum
  not configured".

- **Lesson tripwire and lessons.** The tripwire loaders still return `[]` and
  never raise, but now record a visible notice instead of silencing every
  lesson (closes #3503 via PR #3504). A new `tripwire_priority` field ranks
  the display-cap sort key, which used to rank by pattern length (a 15-char
  guard on `git stash` lost its slot to a 119-char broad reminder; closes #3270
  and #3112 via PR #3278). `engram_add_lesson` returns an advisory
  `unarmed_warning` for a command-shaped lesson with no `situation_pattern`
  (closes #3209 via PR #3420).

- **Session start.** SessionStart reads an existing session marker through
  the non-blocking regular-file open, so a FIFO at the marker path no longer
  hangs it (closes #3576 via PR #3589). The auto-sleep cron instruction gets a
  must-not-miss line so overflow cannot drop it (closes #3336 via PR #3371).

- **`agentctl` and the loop prompt.** `agentctl session` runs a shared
  prologue (umask 022 plus `~/.engram/boot-env`) in the target user's shell at
  every Claude launch site (closes #3451 via PR #3452; PR #3472 pins the order
  at each site). It fails loudly on a Codex resume permission override
  (PR #3281). `format_loop_prompt` keeps the caller's meta on already-marked
  input (PR #3310, part of #3309) and returns the stripped core from its
  idempotency branch (PR #3301, part of #1847).

- **`_focused_ids` reads through the caller's connection (#3737, part of
  #2652).** Closes the zero-opens hole in the tripwire owner path: a fire used
  to open `knowledge.db` directly, twice, even with the owner read flag on.

- **`vocab_table_absent` maps to the empty df result in the tripwire shim
  (#3738, part of #3665).** Adds two cells for mutants that survived the
  earlier review, before the owner-read flag is turned on.

- **`extract_keywords` takes `n_docs` (#3743, part of #3740).** The tripwire
  passes the count it already computed, so a fire runs one `COUNT` instead of
  two.

- **The six SessionStart store readers go through the owner behind
  `engramd.session_start_reads_via_owner`, default off (#3746, part of
  #3735).**

- **The session-start calibration block reads through one typed
  `confidence_by_type` owner op (closes #3741 via PR #3759).** With the flag
  on it no longer builds a server-runtime client and calls `engram_stats`
  twice.

- **`session_checkpoint_block` fetches only what it renders (#3763, part of
  #3761).** One statement returned every row of the window (about 4.8 MB
  through the owner on a real store); it is now a `COUNT(*)` plus a capped
  list.

- **The five unordered focused reads now `ORDER BY focused_at` (#3760, part of
  #3613).** They plan on `idx_nodes_focused` instead of scanning.

- **Live in-context exclusion on the prompt and tool recall routes, default
  off (#3620, part of #3613).** Recall stops re-showing nodes already in the
  model's context.

- **`⟦ENGRAM⟧` meaningful-span marker, consumer side (#3626, part of #3613).**
  A shared helper plus the surface and tripwire hooks understand the marker; a
  message without one behaves as before.

- **`⟦ENGRAM⟧` marker, producer side (#3632, part of #3613).** The loop stub,
  probe rating request and forum monitors emit it; an empty marker means skip
  recall.

- **SessionStart opens every path non-blocking and regular-file-only (closes
  #3591 via PR #3627).** A FIFO at a path used to hang the hook until its 5 s
  timeout killed it.

- **A whole SessionStart survives a FIFO at `config.json` or `knowledge.db`
  (#3633, part of #3628).**

- **Shared hook libs and small hooks open paths non-blocking and
  regular-file-only (closes #3572 via PR #3636, part of #3628).**

- **The surface hook's file opens are non-blocking and regular-file-only
  (#3637, part of #3628).**

- **The lesson-tripwire hook's file opens are non-blocking and
  regular-file-only (#3639, part of #3628).**

- **Hook `mode=ro` sqlite connects and the surface daemon's file opens never
  block on a FIFO (#3642, part of #3628).** Completes section 3 of the issue.

- **A lesson-tripwire crash is visible, not "nothing matched" (#3643, part of
  #3521, row 9).**

- **A broken principle-trigger registry reads as UNKNOWN, not "nothing fired"
  (#3644, part of #3521, row 7).**

- **The drowsiness banner says unknown when the transcript cannot be read
  (#3645, part of #3521, row 8).**

- **The forum prompt hook reports a failed `_prompthooklib` import (#3646,
  part of #3521).** It was a silent exit 0.

- **The baton and inter-agent prompt hooks report a failed `_prompthooklib`
  import (#3647, part of #3521).**

- **The import-failure gate comment matches the `__main__`-gated exit (#3652,
  part of #3521).** Comment only.

- **A corrupt `principle-trigger-state.json` is UNKNOWN and moved aside in the
  surface hook (#3654, part of #3521, row 40).** It was silently overwritten.

- **Core and the utility-credit hook never overwrite an unusable
  `principle-trigger-state.json` (#3659, part of #3521, row 40).** A
  three-reader parity test pins it.

- **The utility-credit hook takes `_with_state_lock` for its trigger-state
  read-modify-write (closes #3658 via PR #3661).**

- **Shown records are pruned after 14 days and the append never follows a
  symlink (closes #3622 via PR #3668).** Session `*.shown.jsonl` files used to
  accumulate forever.

- **The shown record is all-or-nothing on `emit_context`'s final cut (closes
  #3621 via PR #3671).**

- **The tripwire's shown record is appended only after the response was
  written out (closes #3670 via PR #3674).**

- **The Stop hook's maturity read goes through the owner behind
  `engramd.stop_reads_via_owner`, default off (closes #3664 via PR #3675, part
  of #2652).** U6a.

- **Lesson-tripwire total-budget pass: recall then chain are cut, never the
  block, and the first line says so (closes #3677 via PR #3681).** The joined
  `additionalContext` had no bound.

- **`_render_truncated_first` re-trims the head so the in-band marker cannot
  push past the target (closes #2899 via PR #3692).**

- **The lesson-tripwire hook reads through the owner behind
  `engramd.lesson_tripwire_reads_via_owner`, default off (#3725, part of
  #3665).** The shim routes the IDF lookup to the typed `df` op. U6b.

### Recall-measurement probes (developer tier)

- **The mods-recall probe v0 and its fixes (part of #3362).** A Claude Code
  mod under `tools/probes/mods-recall/` measures what thinking-keyed recall
  would surface at each model step without changing what the model reads
  (PR #3368). Live-run fixes: the MCP result envelope is unwrapped one level
  (PR #3376), thinking-proxy arms use answer text in visible mode and arm C
  skips a blank input (closes #3398 via PR #3406; measured on 418 live step
  rows, 163 `api-error` all had empty input), the query floor drops to 0 with
  a narration fallback (PR #3453), and arm C's cache is keyed by the prompt,
  closing a flake that failed 38 of 80 runs at 16-way parallelism (closes
  #3378 via PR #3412).

- **A route ledger records what the live recall hooks surfaced (part of
  #3458, #3362).** The surface response exposes per-candidate ranking signals
  (PR #3462); the two live hooks append one ledger line per fire when the
  probe is live (PR #3459); the probe joins the ledger into its rating
  request and adds an offline floor and formula sweep (PR #3461). `in_context`
  was null on every candidate because nothing produced it; the hooks now do,
  and `ranked` is a flag (closes #3551 via PR #3554). The live `in_context`
  flag now handles compaction: `shown` counts only the current window, and ids
  in the last compaction summary count as in context (PR #3566, part of #3556).

- **Offline readers.** `recompute_in_context.py` recomputes `in_context`
  from the transcript with a compaction-aware definition, as a sidecar that
  never writes the ledger (PR #3567). `probe_fleet_report.py` renders one
  table across seats and days (PR #3553). `probe_eval` and the fleet report
  read the offline sidecar; before, the route arms' in-context rate always
  read `0.000` (PR #3575).

- **Recall-harness fixes.** `signals["composite"]` is populated from
  `composite_score`; the old branch never fired (closes #3464 via PR #3474).
  Offset-less ISO timestamps are UTC, not local time: measured +4.0 h under
  `TZ=America/New_York` (closes #1801 via PR #3263).

- **`engramd_a6_latency.py --hook tripwire` (#3739, part of #3665).** The
  installed-layout A6 arm for the lesson-tripwire hook, with an `--exclusion
  on|off` switch, a U8 contract gate (exit 3) and a ledger row.

- **`engramd_a6_latency.py --hook session-start` (#3754, part of #3747).** An
  A6 arm for the SessionStart hook: fresh-process fires, an allow-list on the
  flag-on store opens, and a cold row.

- **`--layout-timeout` and a named exit 5 for a layout child that outruns it
  (closes #3755 via PR #3756).** The fixed 1800 s timeout used to end in a raw
  `TimeoutExpired` traceback on a real-size store.

- **An ambiguous `killpg` `ProcessLookupError` falls back to killing the
  child, and every timeout wait is bounded (closes #3757 via PR #3758).** A
  non-leader child used to leave the final `wait()` unbounded.

### Forum server and installer

- **The forum's GitHub integration is an explicit server-side switch (PR
  #3578, part of #3568).** `FORUM_GITHUB_INTEGRATION=on|off`, read once at
  startup: `on` with a token behaves as before byte for byte; `on` without a
  token refuses to start (one stderr line, exit 78); `off` is advisory and
  never a refusal; an unset legacy unit with a token stays ON. The installer
  gained `--github-integration off` for an existing install (closes #3583 via
  PR #3592) and now reads drop-ins, so a token supplied only in
  `engram-forum.service.d/*.conf` no longer renders `=off` on a re-run with
  no flag (closes #3593 via PR #3596). PR #3599 closes the findings deferred
  from those two (a plain, quoted or multi-word `Environment=GH_TOKEN=` in a
  drop-in counts as a token; an empty switch value is refused). The install
  verify probe no longer reports the admin shell's GitHub state and shows the
  service's own line (closes #3597 via PR #3601). PR #3594 adds non-dry-run
  refusal cells so "nothing was written" binds to the check's position, not
  just its presence. `docs/FORUM-SERVER.md` is the user guide (PR #3582).

- **The installer refuses what it cannot render, before writing anything.**
  A flag whose value is another flag, and a `--service-dir` with `;`, `$` or
  `%`, are refused (closes #3590 via PR #3598). `--service-dir` is now an
  allowlist, `[A-Za-z0-9/._+=,@:-]`, absolute, no leading dash, with the same
  check on a reused `FORUM_HOME` (closes #3600 via PR #3602). `--port` must
  be a plain decimal 1-65535, and `--group` and `--admin-user` are validated
  at the flag (closes #3603 via PR #3611). A kept unit may contain only the
  shapes the installer renders; anything else is a loud exit 2 naming the
  line and the remedy (closes #3579 via PR #3588).

- **Deploy files carry no one host's layout (PR #3574, part of #3568).** The
  backup and backup-alarm units are templates rendered with the service dir,
  and a dead-target check runs before any restart. The installer keeps the
  baton login map on re-install and requires an explicit `--group` (closes
  #3312 via PR #3314). The backup writes `.last-success.json`; `/health`
  reports backup age and an `OnFailure` alarm fires, after the nightly backup
  failed for 12 days unnoticed (closes #3276 via PR #3277).

- **The forum service reads GitHub under a dedicated read-only token (part of
  #3283).** The baton gate calls `gh` with an explicit minimal env
  (`GH_TOKEN`, an empty service-owned `GH_CONFIG_DIR`, `PATH`) and returns
  unknown when the token is missing (PR #3507). The board's PR-state read runs
  under the same identity (PR #3512, part of #3509). Under a fine-grained
  token, CI is read through workflow runs (PR #3538, addresses #3533) and
  approvals through REST reviews plus `gh pr view` instead of GraphQL
  `commit{oid}` (PR #3547, addresses #3541). A test enumerates every process
  spawn on the forum service surface and fails on any that is not allow-listed
  or lacks the identity kwargs (closes #3518 via PR #3520).

- **Request hardening on the non-baton routes.** A lone surrogate or other
  non-UTF-8-encodable string is a 422 naming the field, not a 500 (closes
  #3294 via PR #3317, extended to the other routes by PR #3431, closes
  #3318). A non-string value in a JSON field is a 422 (closes #3436 via PR
  #3437). An integer outside SQLite's signed 64-bit range is a 422 before any
  read or write (closes #3438 via PR #3439). `POST /api/projects` holds
  `project_id` to the CLI's rule and guards the store path, so `../../escape`
  can no longer create a file outside the store (closes #3286 via PR #3288).

- **Forum monitors name what failed.** The unreachable and cannot-seed lines
  carry curl's exit code and `time_total` (closes #3441 via PR #3442) and the
  phase curl timed out in (closes #3443 via PR #3463). The elapsed time no
  longer reads `?s` under a comma-decimal locale on bash 3.2 (closes #3469
  via PR #3475).

- **The forum's fairy-phase 409 names the way out (#3916, closes #3914).** A
  PR baton created with `baton init ... --turn <colleague>` stays in phase
  `fairy`, because only a flip to the colleague writes the fairy-to-peer
  transition. A later flip to the merge seat, or `baton advance`, then got a
  409 that was correct but did not say how to reach `peer`. For a baton in
  phase `fairy` that has a colleague, the one 409 branch in `_advance_gate`
  now appends a hint naming the baton id and the colleague: flip it to its
  colleague first, which writes the fairy-to-peer transition, then advance. A
  `fairy` baton with no colleague, and every other phase that reaches the
  branch, keep exactly today's text. The status stays 409 and the phase
  machine is untouched.

### Baton and coordination

- **A strict, server-authoritative gate for `phase: peer` to `phase: merge`
  (PR #3284, part of #3039).** `baton advance <PR>` passes only when GitHub
  shows an `APPROVED` review on the exact current head by a login mapped to
  the baton's colleague; the server derives phases and the same check applies
  to the old flip spelling. The PR also closes four bypasses found after the
  first build. `POST /colleague` is refused (422) on a PR baton at phase
  `merge` (closes #3296 via PR #3298).

- **Closing a baton is gated on the server.** `/status` close and reopen need
  a participant (403) and refuse overwriting a terminal status with 409, the
  10-03 `merged -> cancelled` incident (PR #3415, part of #3313). `/gc` and
  `/merge` derive PR state server-side for repo-qualified anchors, and the
  client's `new_status` no longer decides (closes #3416 and #3478 via PR
  #3471). `/merge` on an unanchored baton needs a participant (closes #3477
  via PR #3486). `merge()` refuses an already-closed baton (closes #3488 via
  PR #3492), and the CLI prints the right hint for that 409 (closes #3494 via
  PR #3505).

- **Anchors fail closed.** `baton init` derives the PR anchor from prefixed
  and slugged ids such as `UMWELT-PR-959` (closes #2916 via PR #3363), refuses
  a PR-shaped id that resolves no anchor (closes #3364 via PR #3365), and
  `POST /api/projects` does the same server-side with no bypass (closes #3366
  via PR #3374) and refuses a present `github` that does not parse, for every
  id (closes #3377 via PR #3384). Both refusals read the accepted-forms text
  from one constant (closes #3390 via PR #3392), pinned by a test (closes
  #3394 via PR #3404). A project anchor carries its GitHub owner,
  `project/<owner>/<N>`, so the prompt hook no longer hardcodes one (PR #3595,
  part of #3568). No default repo is assumed for a bare `pr/<N>` in the
  prompt hook's auto-archive or the board (closes #1919 and #1650 via PR
  #3510).

- **`baton init` and writes.** `init` refuses a case-variant of an existing
  id and names both spellings (closes #3432 via PR #3433). Every
  `projects.py` write body reads, decides and writes under the allocator
  lock, ending lost updates (closes #3255 via PR #3434). The frontmatter
  field separator can no longer cross a newline, in all five copies of the
  parser: `turn_by: ` followed by `participants: [borges]` used to swallow
  the next field (closes #1813 via PR #3264).

- **`baton init` refuses a PR baton whose PR does not exist or is not the
  participants' (closes #3638 via PR #3657).** A mistyped `PR-3637` had
  blocked another agent's PR from its canonical id.

- **After a cross-agent `baton flip` or `advance`, the CLI says the hand-off
  is a crossing when `comms_surface` is not `forum` (#3846, closes #3843, part
  of #3039).** After a successful flip or advance that moves the turn to an
  agent other than the caller, if the caller's `$ENGRAM_HOME/config.json` has
  a `comms_surface` other than `forum`, the CLI prints one bounded line to
  stderr saying the turn moved, that under that surface a baton carries
  whose-turn only, and that the hand-off is an addressed crossing
  (`send_crossing` or `wake`) to the new holder, whose seat may be down.
  Stdout and the exit status are exactly as before. The hint (`_handoff_hint`)
  never raises and is capped at 300 characters. There is no hint for a `forum`
  surface (key absent, null, empty, not a string), a flip to the caller itself
  or to the pool sentinel, an `advance` that did nothing, a failed write, or
  `release` and `close`.

- **The baton login map's `repos` prefix gate is pinned on a non-PR prefix,
  with an admin checklist (#3902, part of #3884).** A flip or advance on a
  baton such as `STUDIO-PR-34` was refused with 422 because the forum's baton
  login map had no row for that prefix. That table is admin config on the
  forum host, read from a root-owned file, so a PR cannot add the rows; this
  change pins and documents the behaviour. Measured before writing the cells:
  `advance` and a flip to the merge seat are refused with the `'repos' table`
  message when the row is absent and succeed when present; a flip to a
  non-merge seat and `close` succeed either way; and `merge` succeeds either
  way, because that route never reads the table (whether it should bind is
  tracked as #3901). `docs/baton-protocol.md` gains an admin checklist next to
  the `repos` section. Tests and docs only; no server code changes.

### Dream, sleep, skills and tools

- **Sleep.** The warm-briefing erase is offset-based with a byte-identity
  post-condition (closes #3141 via PR #3414), written in place so the inode,
  ACL and xattrs survive, with a read-back that restores on mismatch (closes
  #3419 via PR #3422). Sleep-end triage gives every dream finding addressed
  to the awake seat a written disposition (do, file, defer), after one
  question sat in all 36 dream records since 2026-08-26 (closes #3403 via PR
  #3405). The `## From this session` anchor must survive every warm-briefing
  write: stated once in nap Step 5 (closes #3266 via PR #3269) and asserted at
  first-session Step 6 (closes #3274 via PR #3358).

- **Ask-list and task sweeps resolve refs by `owner/repo`, never the cwd
  checkout.** Same bug in three places: `engram-sleep` Step 4 (closes #3188
  via PR #3275), the dream master's step 8a (closes #3418 via PR #3426), and
  the dream-fairy's Category 8 (closes #3427 via PR #3429). A bare `#N` is
  never prune-eligible.

- **Dream spec fixes.** `engram-dream-master.md` shipped 22 occurrences of
  an unsubstituted `{{USER_NAME}}`; it now reads `<primary_user>` (closes
  #3121 via PR #3417). The person-node fairy spec gains a known-awaiting-ruling
  ledger and P5 measures a rate instead of a top-10 sample (closes #3445 and
  #3447 via PR #3465); PR #3489 pins the "re-keyed entry is not gone"
  carry-over.

- **Tools.** `check_stubbed_signatures.py` gives its NARROW lane the
  noise-fallback the WIDE lane has; two bare names had selected about 255 of
  280 test files (closes #3334 via PR #3468).

- **Agent-decided compaction at the nap boundary, default off (#3700, part of
  #3640).** A Python request half (`compact-request.py`) and a skill half
  joined by files under `$ENGRAM_HOME/compact/`.

- **Loop re-arm preserves the pending deadline (closes #3005 via PR #3724).**
  A Monitor expiry notice used to replace the pending wake at full cadence, so
  the declared heartbeat never came due.

- **The `engram-pr-reviewer` spec reads back every comment it posts (#3852,
  closes #3851).** A reviewer round once posted a week-old review of a
  different PR under a "round 2" heading: it wrote its body to a fixed-name
  file that already held the old review, its own write never landed, and its
  report to the parent described the right PR, so nothing signalled the wrong
  post. The spec now has the signature line name its PR (`PR #<N>`), has body
  files come from `mktemp`, and reads back every posted comment, printing
  `READ-BACK OK: comment <id> on PR #<N>: <first line>` or `READ-BACK
  MISMATCH` for a wrong PR, a wrong first line, a truncated body with no `##
  Verdict` section, or a failed fetch.
  `tests/test_pr_reviewer_readback_3851.py` extracts the snippets from the
  spec file itself and runs them with bash against fixtures, so a renamed
  heading fails the test instead of passing it vacuously. A seat loads agent
  specs from its plugin cache snapshot, so this reaches a seat only after
  `/plugin marketplace update` and `/reload-plugins`.

- **The reviewer's post is gated on its first line, and the brief's expected
  line wins (#3858, part of #3853).** The pre-post check and the read-back are
  now one gated block, with `gh pr comment` inside the gate. When the body
  file's first line does not match the expected one, or the fresh body file is
  empty, the block prints `NOT POSTED`, naming both lines, and the comment is
  never posted. When the dispatch brief gives the exact first line, the
  reviewer copies it verbatim and never takes it from the file or from the
  posted comment. Only body files are allowed, since an inline body cannot be
  checked before it is posted, and a substitute for the block in a sandbox
  must keep the gate a gate. Known limit: #3853 stays open for the owed live
  check, one real reviewer dispatch from the installed spec that prints
  `READ-BACK OK`.

- **The `engram-meta-loop` scratch-log date step is portable (#3892, closes
  #3889).** The step used `sed -i` with no backup suffix, which is GNU-only;
  on macOS (BSD `sed`) it failed with "invalid command code". It now writes to
  a temp file beside the log and `mv`s it over, and uses `date +%F`. A new
  scan in `tests/test_prose_home_sweep_3863.py` fails on any GNU-only in-place
  `sed` (`sed -i` or `--in-place` without an attached suffix, and `-i ''`) in
  a fenced block of the shipped skills, agents and templates, and a test runs
  the skill's block as printed with BSD-style `sed` and `date` stand-ins, in
  the default home and in a home with a space. Side effect: `mktemp` creates
  the file with mode 0600, so the scratch log is now private to its owner
  after the step. Known limit: the BSD behaviour is simulated by stand-ins; a
  Mac seat then ran the test file on macOS 27.0.1 (BSD `sed`, `date`,
  `mktemp`) with 176 passed and 4 skipped, and the two cells that failed on
  bare `dev` passed.

- **`tools/weakest_load_bearing.py`: a read-only ranking of weak load-bearing
  beliefs (#3927, closes #3921, part of #3919).** Load-bearing means a
  current, claim-bearing, non-retracted node with dependents; load is the
  number of distinct transitive dependents over current nodes (a diamond
  counts each dependent once). Weakness is 1 minus confidence, and is 1.0 when
  the node is contested (in an active or partially resolved contradiction),
  stale (it has a `pending` cascade marker), or has a NULL confidence, which
  the output says. Score is load times weakness, ties broken by id. Axioms,
  lessons and cornerstones are excluded by type. The output is the top N, each
  with a one-line claim, confidence, load, weakness and its reason, turns
  since last recall (the store has no per-node recall timestamp, so never
  days) and the cheapest falsifier its own evidence suggests. It is not the
  `stale_load_bearing` preset, which ranks importance times low recall over
  nodes in general. It reads only through the owner (`open_reader` /
  `snapshot`, behind `weakest_load_bearing_reads_via_owner`, seven registered
  read templates, no new `sqlite3.connect(`), and with the flag ON and the
  owner down, refusing or malformed it exits non-zero with a typed error,
  never an empty answer. On a copy of one seat's graph of about 3,500 nodes,
  the flag-ON run through a real owner gave byte-identical JSON to the
  flag-OFF run, and with the owner stopped it exited 1 with empty stdout.

- **The compaction mod is on by default once installed (#3923, part of #3640).**
  Lei ruled on 2026-10-18, after a one-day trial on one seat: 3 compactions
  requested at nap boundaries, 3 fired 41 to 49 seconds later, none anywhere
  else. `userConfig.enabled` in `tools/mods/engram-compaction` now defaults to
  true; `enabled: false` still registers nothing. The mod remains a separate
  user-installed plugin (convenience tier), so nothing changes for a seat that
  has not installed it.

### ENGRAM home resolution (`ENGRAM_HOME`)

- **A CI gate keeps new hard-coded ENGRAM home paths out of shipped files
  (#3870, #3896, #3904 and #3906, all part of #3863).** Lei's ruling: the
  deployment uses `ENGRAM_HOME` wherever possible, because the install allows
  a custom folder. `tools/home_path_gate.py` (stdlib only, a blocking
  `pr-checks` step, tiered `dev`) is a ratchet:
  `tools/home_path_baseline.json` lists today's violators and may only shrink.
  #3870 started it at file level with 15 named spellings of the default home
  (`Path.home() / ".engram"`, `os.path.expanduser("~/.engram")`, `~/.engram`
  in strings and prose, `$HOME/.engram`, an absolute `/home/<user>/.engram`,
  and others). #3896 made it line-level: a line with a spelling is clean only
  if the same physical line contains `ENGRAM_HOME` or calls the shared
  resolver, or the file is allowlisted by class (`resolver`, `foreign-home`,
  `self-contained-no-lib`); the baseline is a count per file and the ratchet
  is exact in both directions, so a stale or padded count is red. #3904
  widened the scan to `src/build/` (10 lines in 2 files entered), and #3906
  exempts the plugin venv by name, because it lives at `~/.engram/venv`
  regardless of `ENGRAM_HOME`, by design. Known limits: for the one step from
  the file-level to the line-level baseline, the growth check against the old
  base was skipped (the exactness rule bounds padding there, not new
  violations; that PR's `src/` diff added none), and `src/forum` is not
  scanned.

- **The health-exam, upgrade and sleep skills and the walguard remedy take the
  home from `ENGRAM_HOME` (#3874 and #3875, both part of #3863).** In those
  three skills every `~/.engram`, `$HOME/.engram` and `${HOME}/.engram` in a
  fenced block or inline code span is now `${ENGRAM_HOME:-$HOME/.engram}`, and
  the remedy text in `engram_walguard.py` names the pidfile and db the same
  way. #3875 passes the path to Python snippets as `sys.argv[1]`: splicing it
  into a Python literal broke on a quote in the path (measured: two
  `SyntaxError`s and one `agent=$(...)` that returned empty with rc 0; the
  default `$HOME` hits it too, for a home like `/Users/O'Brien`). The
  line-level test `tests/test_skill_home_paths_3863.py` now accepts only the
  canonical expression, and a runtime pin runs the 7 home-touching snippets
  under a home containing a quote and a space. Known limit: the test does not
  see indented code blocks, link targets or unbackticked prose.

- **Ten tools resolve the home through the one shared resolver (#3883, part of
  #3863).** `backup_claude_projects.py`, `backup_session_logs.py`,
  `compute_task_time.py`, `dream_lottery.py`, `migrate_person_nodes.py`,
  `migrate_prediction_pairing_edges.py`, `migrate_stranded_supported_by.py`,
  `monitor-supervisor.py`, `tripwire.py` and `loop_prompt.py` now use
  `tools/_owner_read.engram_home()`, with no second copy and no inline
  fallback. With `ENGRAM_HOME` unset or empty every default is the old path;
  set, every default follows it, and an explicit flag or env override (`--db`,
  `--marker`, `ENGRAM_CONFIG_PATH`) keeps priority. `loop_prompt.py` changes
  shape: `DEFAULT_MARKER_PATH` is replaced by `default_marker_path()`,
  resolved only on `--rearm-delay`, so hooks that import the module cannot
  fail on it. Three tools (`config_schema.py`, `enumerate_armed_monitors.py`,
  `fairy_report.py`) change prose only. Known limit: a supervisor started from
  a tree that lacks `tools/_owner_read.py` now fails loudly; the
  deployed-layout claim rests on a fixture, not a live deploy.

- **A per-seat ENGRAM home registry for viz discovery and the ACL tool (#3885,
  part of #3863).** A seat whose home is not `<login home>/.engram` records it
  in `<login home>/.engram-home.json` (`{"schema":1,"engram_home":"<abs>"}`,
  mode 0644, written atomically); its own SessionStart hook writes it, prints
  nothing and never raises. Viz `discover_agents` and the viz-operator ACL
  tool read the registry first and keep the `/home/*` glob as the fallback. A
  reader trusts an entry only if it is a single-link regular file of at most
  4096 bytes with schema 1, an absolute path with no `..` naming a directory,
  and an owner uid equal to both the login home's uid and the named
  directory's uid; anything else is ignored. In the same change
  `plan_for_seat` in the ACL tool refuses a `health-snapshots` symlink and a
  directory whose uid differs from the seat home's, on the registered and the
  default path alike (the symlink-follow already existed on the default path).
  Known limit: the guard covers the static child target; it does not make a
  root `setfacl` run path-race-safe, since the directory or an ancestor can
  change between plan and apply.

- **The monitors and shell scripts resolve the home from `ENGRAM_HOME`
  (#3887, part of #3863).** `tools/forum-mention-monitor.sh`,
  `tools/forum-updates-monitor.sh`, `tools/ghpr.sh` and
  `tools/run-all-tests.sh` use the quoted `${ENGRAM_HOME:-$HOME/.engram}`.
  Both monitors read `config.json` from there and pass the path to their
  Python snippets as `sys.argv[1]`. With `ENGRAM_HOME` unset behaviour is
  unchanged. The MCP launcher `launch-engram-server.sh` is deliberately not
  in this list: the PR changed it too, but #3903 put it back to
  `$HOME/.engram/venv` (the venv stays there whatever `ENGRAM_HOME` says), so
  in this release it does not follow `ENGRAM_HOME`. Five core files
  (`engram_lifecycle.py`, `engram_tasks.py`, `health/engine.py`,
  `context_tracker_hook.py`, `viz/server.py`) change docstrings and comments
  only. Known limits: the default in `viz/server.py` is still hard-coded, as
  its docstring now says; and `tools/ghpr.sh` and `tools/run-all-tests.sh`
  isolate a test run only with `HOME=...`, so a user who has `ENGRAM_HOME`
  set would still point those tests at the live home.

- **Skills, agent specs, templates, viz html and the README name `ENGRAM_HOME`
  wherever they spell the default home (#3888, part of #3863).** Agents run
  skill and template prose as commands, so a gate cannot tell an instruction
  from a description. A fenced command now uses the canonical
  `${ENGRAM_HOME:-$HOME/.engram}`, and a descriptive line carries
  `$ENGRAM_HOME` (default `~/.engram`) on the same line. The sweep covers 13
  skills, nine other files (two agent specs, `compact-instructions`, the
  model-identity-gate component, templates and viz html pages), and the
  `engram-pkg` README, plus the one-line twin `template.CLAUDE.md`, which a test pins
  byte-identical to the model-identity-gate component. A new test,
  `tests/test_prose_home_sweep_3863.py`, was written before the sweep. Open
  question left in the PR: whether fairy-read paths should instead carry a
  `<ENGRAM_HOME>` placeholder that the parent resolves, rather than the
  `${...}` expression.

- **One parity-pinned `engram_home()` per layer, and a relative `ENGRAM_HOME`
  is refused (#3903, part of #3863).** The code lines that reach the store now
  follow `ENGRAM_HOME`. Four self-contained copies of `engram_home()` (tools
  `_owner_read.py`, hooks `_hooklib.py`, prompt hooks `_prompthooklib.py`,
  shared-bin `forum_api.py`) are AST-identical to the canonical one, pinned by
  `tests/test_engram_home_parity_3863.py`; the body is absolute, and an empty
  `ENGRAM_HOME` counts as unset. A relative value is refused, because it
  resolves against each process's cwd and a hook, the owner and the MCP server
  would silently land in different homes. The resolver raises `ValueError`
  naming the value; every shipped Python hook exits 0, writes nothing and
  prints one stderr line (fail open); tools that resolve the home at import
  exit non-zero; and the probe mod stays off. `route_probe_dir()` is now
  `engram_home() / "probe" / "mods-recall"`. The venv stays at
  `~/.engram/venv`. Known limit: the shell launchers that start long-lived
  processes refuse a relative value only in a later PR of the series, not
  here.

### Upgrade, release, monitors and docs

- **`check-claude-md-drift.py --live FILE` (closes #3604 via PR #3605; fixes
  #3608 and #3609 via PR #3612).** Checks that every unit of the rendered
  `template.CLAUDE.md` is present in a seat's live `~/.claude/CLAUDE.md`: one
  direction, missing units only, exit 2 on any error. It exists because a
  rebuild that skips a hunk makes every later base-to-head range look clean.
  The follow-up accepts `~` or the absolute home, whichever the flag used (15
  false hits on one seat), adds a `--seed-id` override, and names a missing id.

- **Installer and release docs.** The hook-registration banner says
  `/reload-plugins` first (closes #3268 via PR #3271), and the docs replace
  "a changed `hooks.json` needs a full restart" with the measured rule (closes
  #3265 via PR #3267). `docs/RELEASING.md` gains the website Versions entry and
  the announcement email as release steps, and `release-cutover --push` prints
  the two remaining public steps (PR #3290); the newsletter postal address is
  described as set, with the delivered footer as the check (closes #3319 via
  PR #3320). The tool capability index was regenerated after PR #3278 (closes
  #3287 via PR #3289).

- **Monitors.** Linux seats run one supervised Monitor through
  `monitor-supervisor.py` with a read-only `--check` (closes #3311 via PR
  #3316); Mac seats join it, `--check` gets a ps/lsof backend (closes #3330
  via PR #3338). The boot charges treat `--check` exit 2 as ambiguous when
  the deployed supervisor predates the flag (closes #3356 via PR #3357).

- **Project docs.** `CLAUDE.md` goes from 118,448 to 71,948 bytes by moving
  dated measurement histories into `docs/`, with a budget gate (PR #3323, part
  of #3322). The which-colleague section is re-rendered from team-practice
  (PR #3315), merge-ready reads `phase = merge` with `turn` kept as the cursor
  (PR #3295, part of #3039), and the merge-seat test rules are written down
  and then tightened (closes #3479 via PR #3496, closes #3500 via PR #3501).

- **`compact-request.py --clear` exit 4 and every-run removal are documented
  and pinned (closes #3719 via PR #3728, part of #3640).** Docstring only, no
  behaviour change; four P11 cells pin the contract.

- **The U9 real-store gate result is recorded in the migration ledger (#3762,
  part of #3761).** Docs only: the cell failed at 1.092x on an 11,270-node
  store copy, localized to the checkpoint read that #3763 then fixed.

- **`release-cutover.py` gains `--scan-leaks PATH` and warns loudly when the
  name roster did not run (#3723, part of umwelt#1034).**

- **Cross-release hook-availability CI gate (closes #3253 via PR #3688).**
  Fails when a script a prior release registered is absent from the rebuilt
  plugin, since sessions hold their start-time registrations.

- **`docs/CHANGELOG.md` `[Unreleased]` written from every dev merge since the
  v0.5.2 base (#3616).** 189 PRs grouped by theme.

- **gh-stack: a retarget without auto-rebase is recorded in `docs/GH-STACK.md`
  (#3653).** A dated measurement.

- **Migration ledger v1 for EngramManager (#3667, part of #2652).** Every
  `sqlite3.connect` line into the store, its state, the measured delta and the
  macOS bound.

- **Mechanical completeness guard for the migration ledger (#3669, part of
  #2652).** `tools/check_2652_ledger.py` compares per-file connect counts and
  runs in pr-checks.

- **The ledger guard also checks Section-1 tallies and function-plus-anchor
  citations (closes #3704 via PR #3693, part of #2652).** Stale ledger, design
  and brief lines are corrected.

- **Design doc names A6's instrument axes: build, layout, store, host (#3679,
  part of #2652).** The criterion is unchanged.

- **As-built EngramManager brief, one page (#3687, part of #2652).**

- **Darwin arm build notes for the owner permission layer (#3695, part of
  #3666).** A delta to the security-slice design doc.

- **The 2652 ledger states a template's `sites` as CI-verified provenance, not
  permission (#3834, closes #3832, part of #2652).** Docs only. Ledger section
  3 said a registered template's `sites` are "the `file:function` callers
  allowed to use it", which was false: `sites` records the exact shipped call
  sites that issue the statement, regenerated by `tools/engramd_read_scan.py`
  and checked on every PR, and it decides which statements are registered,
  never who may send one. At runtime admission is by template bytes alone, for
  any process at the owner's uid; the only caller boundary is the socket's
  same-uid peer-credential check. The `engramd_read_registry.py` module
  docstring now states that caller-blind rule, and the surface-hook row
  records the measured gate figures (see #3827).

- **The 2652 security-slice design doc states what the cross-uid control now
  runs, and what it cannot show (#3850, closes #3842, part of #2652).** Docs
  only. Section 6 still said the cross-uid rejection test "has not been run".
  It now names the CI cell `test_peer_uid_mismatch_is_rejected` (a real uid-0
  peer after a same-uid control, seen to pass on `main` and to fail on a
  scratch mutant branch) and its bound: uid 0 is also refused by a second
  branch, so that cell alone cannot show the mismatch rule works. It names the
  unit cells on both arms that hand the check a manufactured non-root foreign
  credential (the Darwin cell runs on Mac seats only, since CI is all
  `ubuntu-latest`), and a real-socket test on Linux where a non-root foreign
  peer received a typed `peer_identity` refusal and nothing was served. Known
  limit: the real-socket test on Darwin has not been run.

### Viz

- **A read-only Moments page (closes #3511 via PR #3513).** Shows the special
  moments an agent keeps on its person nodes, grouped by person, newest first,
  hidden when empty. It opens the graph `mode=ro` and never touches
  `special_moment_cursor`, which recall rotates.

### Tests and CI

- **Nightly (closes #3539 via PR #3558).** Every nightly from 09-26 to 10-07
  (9 runs) ran out of disk at the pip-cache save, and on 10-07 a 17547-passed
  suite read RED (PR #3548). The runner had 4.7G free before the suite and
  2.7G after; the fix drops the pip cache (`--no-cache-dir`) and passing
  tests' tmp dirs, and installs torch from the CPU index, since 2993 of the
  3115 MB downloaded was the CUDA stack (PR #3584).

- **`pr-checks` gates.** Gates run independently, so one red step no longer
  skips the rest (PR #3306, part of #3304). A `[part of #N]` PR whose body
  would auto-close #N now fails (closes #3344 via PR #3347). The two
  artifact-staleness guards run on dev PRs (PR #3302, part of #2340). Twelve
  test files that ran in no CI job are wired or retired, two dead baton-gate
  files (34 tests) among them (closes #3346 via PR #3372). The affected-tests
  dispatch falls back to the default branch when `base_ref` is empty; it used
  to be a vacuous green (closes #3410 via PR #3428). `test-id-set-check` says
  when a backtick-wrapped `Removed-test:` line did not parse (closes #3244
  via PR #3430). The name-leak test pattern is live (its `\b` compiled as a
  backspace) and its handle roster is current (PR #3305, PR #3308).

- **Mac portability (#3292, #3333).** Tests are gated by the missing
  primitive, never by OS name (PR #3340); fixes for `setfacl`, POSIX-sh mocks,
  real paths, the `AF_UNIX` 104-byte limit, forum deps and a fake-ssh pid race
  are PR #3380, #3379, #3391, #3387, #3381, #3382, #3388 and #3383. The
  `BlockingIOError` branch of `start-engram-daemon.sh` is exercised by
  injection (PR #3411).

- **Test isolation.** Suite-wide guards refuse mutating `launchctl` and
  `systemctl` calls, after a test run registered a zombie launchd job that
  respawned 27,605 times (PR #3522, PR #3540, part of #3519). A conftest
  fixture restores `ENGRAM_HOME`, `ENGRAM_SESSION_ROLE` and
  `ENGRAM_RUNTIME_DIR` per test (closes #3328 via PR #3331), and a forum
  module is dropped only when it is a shadow (closes #3335 via PR #3337).

- **Repairs and pins.** Dev red from a full-suite-only guard was repaired in
  PR #3526 (closes #3525), #3528 (closes #3527), #3482 (part of #3481) and
  #3483 (refs #3481). The cross-uid rejection control is a real test where `sudo -n`
  works (closes #3197 via PR #3421, PR #3341's ship gate). Pins and contract
  tests: PR #3480 (bash 3.2 locale behaviour, refs #3469), #3581 (host
  paths, closes #3580), and the installer's "nothing written" cells in PR
  #3594.

- **Stage-accounting test retries are spaced about 10 s and report the judged
  pairs (#3787, part of #3784).** Part (a) of the nightly miss in
  `test_stages_account_for_the_end_to_end_call`.

- **A timing marker, tracer guard and no-coverage timing step for the
  stage-accounting cells (#3788, part of #3784).** The nightly's coverage run
  inflated the measured ratio past its 10% bound.

- **The setup lock's `0600` refusal is pinned (#3797, part of #3791).**
  Swallowing a failed `os.fchmod` in `_acquire_db_setup_lock` kept every
  contract cell green before.

- **The setup-surface-daemon wrapper cells drive the real installer (closes
  #3801 via PR #3802).** Four cells ran a hand-copied render, so a change to
  the real script could not turn them red.

- **The k14 owner-launcher cell binds its socket by relative path (closes
  #3808 via PR #3809).** The absolute path hit the `AF_UNIX` length limit when
  the pytest temp prefix was a few characters deeper.

- **conftest releases memo caches that pinned replaced server copies (#3810,
  part of #3807).** Pytest parent memory fell from 10.1 GB to 1.7 GB; whether
  this ends the hosted-runner nightly deaths is not yet shown.

- **conftest pre-imports `tracemalloc` (#3811, part of #3784).** A
  `sys.modules` clear no longer breaks pytest's thread-exception report, so
  the next recurrence of item (b) is readable.

- **Window-operator and bool `current_turn` mutants are pinned (#3764,
  follow-up to #3759, part of #3741).** Two mutants survived the earlier
  cells.

- **GNU-`stat` test cells are gated on the primitive, not the platform (closes
  #3614 via PR #3619; the last cell in PR #3629).**

- **The W3 config-write positive control is deterministic (closes #3634 via PR
  #3635).** The race-based control failed 6 of 6 on one Mac account.

- **The row-9 dev-silence control decides by behaviour (#3650, part of
  #3521).** It failed on dev once the fix merged.

- **`test_c2` compares against the merge-base only when there is an old
  version (#3660, part of #3521).**

- **The K15 long-home owner cells skip on `ENAMETOOLONG`, with a helper-level
  bound cell (#3682).** They failed on a Mac.

- **The U5 K10 pin reads a frozen fixture, not a git SHA the squash merge
  dropped (closes #3684 via PR #3685).** It had turned the nightly red.

- **U7 stage accounting is judged on the best of up to 3 profiles (closes
  #3721 via PR #3722).**

- **The service-manager guard is keyed on the class, not two tool names
  (#3837, part of #3519).** The suite-wide guard stated its invariant as "no
  test may mutate the real service manager" but shimmed only `launchctl` and
  `systemctl`; `systemd-run --user` from a test created real transient units
  (12 leaked over four suite runs, two of them looping) with no refusal. Two
  more shims, with the same refusal message: `systemd-run` refuses everything
  except exactly one argument that is `--version`, `--help` or `-h`, and
  `loginctl` passes the read-only verbs through and refuses the rest, failing
  closed on unknown dash options. `SERVICE_MANAGER_TOOL_CANDIDATES` in
  `conftest.py` lists nine tools, and a scan cell goes red when a candidate is
  invoked anywhere under `src/`, `tests/`, `tools/` or `deploy/` without a shim, so the
  next mutator shows up as a red test instead of a leaked unit. Known limits:
  a real tool called by absolute path, unit files written under
  `~/.config/systemd/user/`, and D-Bus calls from Python bindings are not
  covered.

- **The fake owner answers a sqlite failure as `read_failed` and never dies in
  its handler thread (#3841, closes #3833, part of #2652).** Test-only. A
  sqlite failure while serving a read (a garbled store, a missing table or
  file) killed the fake's handler thread, so the fake closed the connection
  where the real owner answers a typed `read_failed`, and every such run
  emitted a `PytestUnhandledThreadExceptionWarning`. On unmodified `dev` the
  U10 `test_c6_...[fake-owner]` cells emitted 3 warnings; on the fix, 0.
  `read`, `df` and `chain` now answer any `sqlite3.Error` as the real owner's
  `read_failed` (with the real remedy string, pinned equal to production), and
  a malformed request is dropped without a thread exception, as the real owner
  drops one.

- **The school-day L4 marker cell runs the printed line instead of regexing
  the skill (#3894, closes #3893).**
  `test_L4_school_day_loop_prompt_field_leads_with_loop_wake_and_ends_with_empty_marker`
  went red on `dev` after the prose sweep put a double quote inside the
  skill's `printf`, which ended the test's regex match early; it escaped CI
  because dev PRs run `pr-checks` and the full suite runs at release cuts. The
  cell now extracts the skill's own marker-writing line, runs it under a temp
  `HOME` in the default home and in a home with a space, and parses the file
  it writes with `json.load`. A small shared helper,
  `tests/_skill_block_runner.py`, extracts and runs fenced blocks. The skill
  is not touched.

- **The one-time sweep scope guards are retired, then made to retire by
  content, not by history shape (#3910, part of #3863; #3926, closes #3925).**
  Three guards in `tests/test_prose_home_sweep_3863.py` kept two sibling PRs
  off each other's files and then blocked every later PR that edited a swept
  file; #3910 skips them once the sweep is on `dev`. The v0.5.3-rc2 release
  PR's first full-suite run then went red on three of the sweep's snapshot
  checks (3 failed, 27265 passed, 425 skipped). The likely cause, not an
  established one: they decided to retire by counting commits (`git rev-list
  --count BASE..HEAD -- <file>`), which is a well-formed wrong number on a
  partial history, and #3910's check keyed on an `origin/dev` ref that a
  merge-ref checkout lacks. The PR did not reconstruct CI's exact checkout, and
  why CI's history was partial is not established. The snapshot guards now
  run only if the base and the sweep commit are both available and the file at
  HEAD equals the file at the sweep commit; anything else skips, decided by
  one pure function, `retire_reason`. The three frozen-fact scope guards are
  retired permanently. Known limit: retiring a guard is a coverage loss that
  stays green; after this nothing in the suite asserts that a later PR stays
  inside the sweep's file set, and the PR's own diff is the only scope check.

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

  > **Correction, 2026-10-01 (#3265; the tag's text above stays as cut).**
  > The claim that `/reload-plugins` is not enough is measured false. On the
  > rc4 upgrade, `/reload-plugins` (after `/plugin marketplace update
  > engram-local`) reported 28 hooks, which is the new set (20 engram + 8
  > umwelt; the old set would read 30), with no restart. Upgrade with the
  > `/plugin marketplace update` → `/reload-plugins` → `/mcp` sequence; restart
  > fully only if a registration demonstrably does not take effect. Measured
  > once for a removal (#3265, this release) and once for an addition (#1892,
  > Ariadne, 2026-08-03: 29 = 22 + 7, the new Stop hook fired, no restart); one
  > seat each, two dates (harness versions not recorded).
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

