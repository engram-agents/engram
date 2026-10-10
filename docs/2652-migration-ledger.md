# 2652 migration ledger — every channel into the store, its state, its measured delta

*Epic: engram-alpha#2652 (EngramManager). First version, measured at dev `21ec2944`.*

## Summary (for Lei)

- **Population:** 76 lines match `sqlite3.connect(` in tracked `src/engram` at `21ec2944`, in 34 files. 65 are executed calls, 11 are prose (comments, docstrings, instructions to an agent).
- **State of the 43 live-store lines** (calls and doc instructions that reach `knowledge.db`): **owner 3** (the owner's own connections) · **direct 39** (35 calls + 4 doc instructions) · **behind-flag 1** (user-identity read, U4d) · **flag-on 0** · **retired 0**. The other 33 lines are out of scope (other databases, or prose).
- **Measured delta so far: one.** User-identity hook, owner round trip vs its own direct path, A6 on SQLite 3.53.0: **sequential 0.90x p50 / 0.95x p99 (the gate holds)**, contended 0.26x, cold 0.19x. One host, synthetic 5,005-node store, N=1000 (details in the A6 section).
- **Next three units:** U5 owner launcher, Linux (#3663) · U6a stop hook behind `engramd.stop_reads_via_owner` (#3664) · U6b lesson-tripwire hook behind `engramd.lesson_tripwire_reads_via_owner` (#3665).
- **Before any flag goes ON in production:** the U5 launcher (#3663). At `21ec2944` no launcher for the owner exists in the tracked tree (searched: `git grep -l -E 'engramd_owner|engramd-owner' -- src/engram` finds only the owner, client, pidfile and registry modules and the surface daemon; the service and plist templates under `src/engram/templates/` cover the surface daemon and viz only), so the owner is started by hand; a seat's own hand-made unit is not visible to this search. **On Mac seats** there is no path at all until the Darwin permission arm lands (#3666, Aleph designs, Mira reviews).
- **Mac seats:** the owner refuses to start on macOS today, so any owner path is unreachable there. Every live-store row says so in its `macOS` column, in one of two forms: `unreachable via owner (Darwin arm #3666)` for channels with a planned owner path (the hooks and the MCP server's `_get_db`, the sites in the Section 3 registry), and `n/a — no owner path planned yet` for channels with none (tools, viz, backup, doc instructions, other server connects). The second form is not a promise that those channels will migrate.

## 1. Population — what is counted, and at which ref

**Instrument.** Every line matching `sqlite3.connect(` in TRACKED files under `src/engram`, at `21ec2944` (dev tip; `git grep -n 'sqlite3\.connect(' 21ec2944 -- src/engram`). Count: **76 lines in 34 files**. This counts lines, so comments, docstrings and markdown text are included; each row below is one line.

**Two counts, two scopes — keep both and do not reconcile them:**

| count | ranges over | ref / instrument |
|---|---|---|
| **76 lines / 34 files** (this ledger) | every line matching `sqlite3.connect(` in tracked files under `src/engram`: Python, shell, and markdown | `21ec2944`; `git grep` on the source tree |
| **58** (the epic #2652's figure) | the *deployed plugin tree* on Kepler's seat | an older ref, a different instrument; design §4.4 records "58/28 on Kepler's seat and 53/23 on mine" for the deployed trees |

The two are different populations measured by different instruments at different refs. 58 is not 76 minus anything. Design §4.1 also shows the same source-tree grep split by file type (56 Python sites / 29 files at the epic's ref `1031ec21` and at dev `7b68d385`, plus 4 non-`.py` files); this ledger's 76 is the all-file count on the later tip.

**What the instrument cannot see (stated so it is not read as covered):**

- Reads that reach the store through a helper are ONE line here, however many callers use it: `hooks/claude/_hooklib.py:1109` (`connect_regular_ro`) is the shared read-only opener that most hook reads of `knowledge.db` go through (callers in row 33), and `engram_core.py:4936` (`_get_db`) is the process-wide opener for the tool bodies that use it. Neither is exhaustive: six hook sites open their own connection (rows 34-39), three other `engram_core.py` sites do too (rows 10-12), and so does `server.py:1225` (row 40). The per-hook migration rows therefore live in Section 3 (the read-template registry) as well as in Section 2.
- Non-matching spellings. One bare `connect(":memory:")` exists (`hooks/claude/engram-session-start-hook.py:538`, an in-memory JSON1 probe, declared out of scope in the registry). I searched for `from sqlite3 import`, `import sqlite3 as` and spaced forms and found no others; I did not search for aliasing through another module.
- The deployed tree (a stale plugin tree keeps its channels until it rebuilds), and the tests tree (design §4.2: a path-keyed allowlist, not a directory-keyed one, is the sound guard).

### Tallies (this tip)

- **unit:** one row per matching LINE, deliberately. Row 55 is one line with two connects, so the 65 `call` rows are 65 lines and 66 connects.
- **kind:** call 65 · prose 11
- **target:** live store 28 · parameter 15 · other DB 26 · n/a (prose) 7 · unresolved 0
- **state:** owner 3 · direct 39 · behind-flag 1 · flag-on 0 · retired 0 · out of scope 33
- Of the 39 `direct` lines, 35 are calls and 4 are documents instructing an agent to open the store (`engram-person-node-fairy.md:85`, `engram-first-session/SKILL.md:46`, `engram-health-exam/SKILL.md:220` and `:249`). Design §4.2 counts such documents as channels because following them regenerates the channel; they are kind `prose` and state `direct`.
- `parameter` rows are counted live when at least one caller passes the seat's store (named in each row). `engram_backup.py:673` is the one judgement call: its only in-tree caller is its own CLI, so it is counted `direct` on the basis that an operator may pass the live store.

## 2. The ledger — one row per matching line (row 55 holds two connects; rows count lines, not connects)

Columns. **kind:** `call` or `prose`. **target:** `live store` (the seat's `$ENGRAM_HOME/knowledge.db`), `parameter` (path passed in; live callers named), `other DB` (named). **access:** the open mode — `ro` is `mode=ro`; `rw` means opened without `mode=ro` (a read-only use is said in the note). **state** (live-store rows only): `owner` · `direct` · `behind-flag` · `flag-on` · `retired`. **A6 delta:** only the user-identity row has one. **macOS:** per row, for every live-store row; `unreachable via owner (Darwin arm #3666)` = a planned owner path exists and Mac blocks it, `n/a — no owner path planned yet` = no owner path is planned for this channel (see the Mac seats paragraph).

**Mac seats, applies to every `macOS` cell below.** The owner refuses to start on macOS: `EngramdOwner._check_platform_support` (`src/engram/engramd_owner.py:1168`, called from `start()` at `:1038`) raises `OwnerPermissionUnsupported` with the reason from `platform_unsupported_reason()` (`:881`; the Linux test is at `:890`), because U4a's permission layer (`SO_PEERCRED`, the `run/` ACL strip) is Linux-only. So the one `*_via_owner` flag in tree today, `engramd.user_identity_reads_via_owner`, has no effect on a Mac seat except to make the hook report `Unavailable`; the U6a and U6b flags, once they exist, would behave the same. This holds until the Darwin permission arm lands: **#3666** (Aleph designs, Mira reviews). Per #3666 the affected seats are Aleph, Clio and Luria.

| # | site (function) | kind | target | access | component | state | A6 delta | macOS |
|---|---|---|---|---|---|---|---|---|
| 1 | `agents/claude/engram-person-node-fairy.md:85` ((doc example, step: uniform sample)) | prose | parameter (`<db path>` placeholder; the fairy is handed the seat's knowledge.db) — markdown snippet instructing an agent to open `<db path>` ro; placeholder is the seat's knowledge.db | ro | agent-doc | direct | — | n/a — no owner path planned yet |
| 2 | `bootstrap.py:406` (_db_node_count) | call | live store — `COUNT(*)` only, but opened without mode=ro; sole caller bootstrap.py:448 passes `ENGRAM_HOME/knowledge.db`; `timeout=0` | rw | other (bootstrap) | direct | — | n/a — no owner path planned yet |
| 3 | `engram_backup.py:128` (dump_stripped) | call | parameter — source of `Connection.backup`; live callers: engram_core.py:8564 (`str(DB_PATH)`), tools/backup_knowledge_db.py:399; non-live caller: engram_snapshot_worker.py:385 (captured snapshot) | rw | backup/snapshot | direct | — | n/a — no owner path planned yet |
| 4 | `engram_backup.py:134` (dump_stripped) | call | other DB: `.engram-bk-*.snap.db` temp copy beside the source | rw | backup/snapshot | out of scope | — | — |
| 5 | `engram_backup.py:555` (rebuild_fts_index) | call | parameter — rewrites the FTS index; live callers: engram_core.py:3750 and :4353 (path from the caller's `PRAGMA database_list`), tools/engram-regenerate-embeddings.py:308,361; also reconstructed DBs from engram_backup.py:731 | rw | backup/snapshot | direct | — | n/a — no owner path planned yet |
| 6 | `engram_backup.py:673` (verify_roundtrip) | call | parameter — only in-tree caller is this module's CLI (engram_backup.py:828, `args.src_db`); reaches the live store only if an operator passes it | rw | backup/snapshot | direct | — | n/a — no owner path planned yet |
| 7 | `engram_backup.py:710` (verify_roundtrip) | call | other DB: reconstruction DB restored from the dump (`recon_path`) | rw | backup/snapshot | out of scope | — | — |
| 8 | `engram_backup.py:732` (verify_roundtrip) | call | other DB: same reconstruction DB, reopened after FTS rebuild | rw | backup/snapshot | out of scope | — | — |
| 9 | `engram_core.py:4936` (_get_db) | call | live store — the process-wide opener; `DB_PATH = DATA_DIR/knowledge.db` (engram_core.py:362); tools reach the store through it | rw | MCP server/core | direct | — | unreachable via owner (Darwin arm #3666) |
| 10 | `engram_core.py:7311` (_run_embedding_restore_regeneration) | call | parameter — sole caller engram_core.py:5061 passes `str(DB_PATH.resolve())` (:5035); daemon thread, `timeout=30.0` | rw | MCP server/core | direct | — | n/a — no owner path planned yet |
| 11 | `engram_core.py:7670` (verify_write_durability) | call | parameter — fresh second connection to the file named by the caller's own connection (`PRAGMA database_list`); callers are the write tools (engram_derive, engram_epistemic, engram_observation, engram_tasks, engram_trust, engram_cornerstone) passing the live connection; reads only | rw | MCP server/core | direct | — | n/a — no owner path planned yet |
| 12 | `engram_core.py:8039` (_write_binary_backup) | call | parameter — source of `Connection.backup`; callers: engram_core.py:3210 (pre-migration path from the connection), :8382 and :8585 (`str(DB_PATH)`) | rw | backup/snapshot | direct | — | n/a — no owner path planned yet |
| 13 | `engram_core.py:8041` (_write_binary_backup) | call | other DB: destination copy under `db-backup/` | rw | backup/snapshot | out of scope | — | — |
| 14 | `engram_log_indexer.py:614` (run_once) | call | other DB: `logs/index.db` (log index), `logs_dir/index.db` | rw | other (log indexer) | out of scope | — | — |
| 15 | `engram_snapshot_worker.py:246` (write_durable_snapshot) | call | parameter — file path is taken from the caller's connection (`PRAGMA database_list`, main); sole caller engram_core.py:8361 passes the live connection | ro | backup/snapshot | direct | — | n/a — no owner path planned yet |
| 16 | `engram_snapshot_worker.py:248` (write_durable_snapshot) | call | other DB: `.pending/` staging snapshot (`tmp`) | rw | backup/snapshot | out of scope | — | — |
| 17 | `engram_snapshot_worker.py:390` (_regenerate_derived_files) | call | other DB: captured snapshot (`mode=ro&immutable=1`) | ro | backup/snapshot | out of scope | — | — |
| 18 | `engram_telemetry_store.py:391` (open_store) | call | other DB: telemetry `records.db` (WAL off, `journal_mode=DELETE`) | rw | other (telemetry) | out of scope | — | — |
| 19 | `engram_telemetry_verdicts.py:616` (summary_line) | call | other DB: telemetry `records.db` | ro | other (telemetry) | out of scope | — | — |
| 20 | `engramd_owner.py:1084` (start) | prose | n/a — comment: "sqlite3.connect() creates an empty file" | n/a | owner | out of scope | — | — |
| 21 | `engramd_owner.py:1092` (start) | call | live store — `_owner_conn`, permissive owner-internal authorizer | rw | owner | owner | — | n/a — owner refuses to start |
| 22 | `engramd_owner.py:1103` (start) | call | live store — `_client_read_conn`: opened without mode=ro but permanently restrictive authorizer + `SQLITE_LIMIT_LENGTH`; serves client `read` | rw | owner | owner | — | n/a — owner refuses to start |
| 23 | `engramd_owner.py:1155` (_load_read_registry) | call | live store — short-lived dedicated connection that classifies the read templates | ro | owner | owner | — | n/a — owner refuses to start |
| 24 | `engramd_owner.py:2116` (_handle_snapshot) | prose | n/a — trailing comment on `os.close(temp_fd)` | n/a | owner | out of scope | — | — |
| 25 | `engramd_owner.py:2126` (_handle_snapshot) | call | other DB: snapshot staging file (O_EXCL temp beside `dest`) | rw | owner | out of scope | — | — |
| 26 | `health/rows.py:6` ((module docstring)) | prose | n/a | n/a | health | out of scope | — | — |
| 27 | `health/series.py:683` (open_store) | call | other DB: health `series.db` (`db_path` parameter; own file, design §4.3) | rw | health | out of scope | — | — |
| 28 | `health/series.py:2474` (replay_commit) | prose | n/a — docstring | n/a | health | out of scope | — | — |
| 29 | `health/series.py:2518` (replay_commit) | call | other DB: in-memory DB restored from a git `knowledge.sql` dump | rw | health | out of scope | — | — |
| 30 | `health/series.py:3583` (export) | prose | n/a — docstring | n/a | health | out of scope | — | — |
| 31 | `health/series.py:3657` (export) | call | other DB: health `series.db` | ro | health | out of scope | — | — |
| 32 | `hooks/claude/_hooklib.py:1080` (connect_regular_ro) | prose | n/a — docstring | n/a | hook | out of scope | — | — |
| 33 | `hooks/claude/_hooklib.py:1109` (connect_regular_ro) | call | parameter — the shared hook opener; `uri_params` default `mode=ro`. Callers (grep of `connect_regular_ro(` over `src/engram`; all in hooks) pass the seat's knowledge.db: stop-hook :377, lesson-tripwire :318/:570/:854/:1452, session-start :460/:1419/:1622/:1870, surface :159/:318/:1388/:1528/:3688, `_hooklib._focused_ids` :1463. One caller opens a pack DB instead: surface :1684 (attached pack, `uri_params="mode=ro&immutable=1"`, not the store). The per-hook rows are in the registry table (Section 3) | ro | hook | direct | — | unreachable via owner (Darwin arm #3666) |
| 34 | `hooks/claude/engram-session-start-hook.py:390` (_check_backup_staleness) | call | live store — `COUNT(*)` only, opened without mode=ro, `timeout=1.0`; registry site (template `SELECT count(*) FROM nodes`) | rw | hook | direct | — | unreachable via owner (Darwin arm #3666) |
| 35 | `hooks/claude/engram-session-start-hook.py:2773` (sleep_status_block) | call | live store — `COUNT(*)`, `ENGRAM_DB_PATH`, `timeout=2.0`; registry site | rw | hook | direct | — | unreachable via owner (Darwin arm #3666) |
| 36 | `hooks/claude/engram-session-start-hook.py:2796` (sleep_status_block) | call | live store — `COUNT(*) WHERE created_at > ?`, `timeout=2.0`; registry site | rw | hook | direct | — | unreachable via owner (Darwin arm #3666) |
| 37 | `hooks/claude/engram-surface-hook.py:1137` (_advance_person_moment_cursor) | call | live store — READ-MODIFY-WRITE: `SELECT metadata` then `UPDATE nodes SET metadata`; a registered write site | rw | hook | direct | — | unreachable via owner (Darwin arm #3666) |
| 38 | `hooks/claude/engram-user-identity-hook.py:94` (get_person_nodes) | call | live store — the OLD path, kept live for flag OFF; flag `engramd.user_identity_reads_via_owner` (default OFF) routes the read through the owner instead (U4d, #3341) | rw | hook | behind-flag | 3.45.1: seq 0.90x / cont 0.24x / cold 0.20x; 3.53.0: seq 0.90x p50, 0.95x p99 (gate holds) / cont 0.26x / cold 0.19x — see A6 | unreachable via owner (Darwin arm #3666) |
| 39 | `hooks/claude/engram-utility-credit-mention-stop.py:209` (bump_utility) | call | parameter — WRITES (`bump_utility`, WAL pragma, commit); `db_path` defaults to `KNOWLEDGE_DB` = `ENGRAM_HOME/knowledge.db`; a registered write site | rw | hook | direct | — | unreachable via owner (Darwin arm #3666) |
| 40 | `server.py:1225` (_get_timing_conn) | call | live store — long-lived tool-timing connection on `core.DB_PATH.resolve()`; one INSERT per tool call | rw | MCP server/core | direct | — | n/a — no owner path planned yet |
| 41 | `server.py:1394` (_log_engram_tool_event) | call | other DB: `logs/index.db` (log index) | rw | MCP server/core | out of scope | — | — |
| 42 | `skills/claude/engram-first-session/SKILL.md:46` ((doc, seed-empty check)) | prose | live store — inline `python3 -c` read of `$ENGRAM_HOME/knowledge.db` | rw | skill-doc | direct | — | n/a — no owner path planned yet |
| 43 | `skills/claude/engram-health-exam/SKILL.md:220` ((doc, schema-version check)) | prose | live store — `~/.engram/knowledge.db`, opened without mode=ro, reads `PRAGMA user_version` | rw | skill-doc | direct | — | n/a — no owner path planned yet |
| 44 | `skills/claude/engram-health-exam/SKILL.md:249` ((doc, FTS integrity check)) | prose | live store — `?mode=ro` on `~/.engram/knowledge.db` | ro | skill-doc | direct | — | n/a — no owner path planned yet |
| 45 | `tools/ack_recompute_sweep.py:110` (main) | call | live store — WRITES with `--apply`; `--db` defaults to `ENGRAM_HOME/knowledge.db` (`_default_db`, :43) | rw | tool (CLI) | direct | — | n/a — no owner path planned yet |
| 46 | `tools/anchoring_report.py:57` (_open_readonly) | call | parameter — sole caller :200 passes `--db` or `core.DATA_DIR/knowledge.db` | ro | tool (CLI) | direct | — | n/a — no owner path planned yet |
| 47 | `tools/backup_knowledge_db.py:207` (_read_live_watermark) | call | parameter — named for the live store; `--db` defaults to `~/.engram/knowledge.db` (:93-94, :326) | ro | backup/snapshot | direct | — | n/a — no owner path planned yet |
| 48 | `tools/cites_step3.py:675` (open) | call | live store — dry-run branch; `--apply` branch goes through `core._get_db()` (a call-through to engram_core.py:4936, not a connect on this line); path `core.DB_PATH` | ro | tool (CLI) | direct | — | n/a — no owner path planned yet |
| 49 | `tools/cites_step3.py:1344` (_backup_db) | call | parameter — source of the pre-apply backup; sole caller `take_backup` (:1373) is called with `core.DB_PATH` (:1472) | ro | backup/snapshot | direct | — | n/a — no owner path planned yet |
| 50 | `tools/cites_step3.py:1346` (_backup_db) | call | other DB: pre-apply backup file `knowledge-before-cites-step3-*.db` | rw | backup/snapshot | out of scope | — | — |
| 51 | `tools/cites_step3.py:1356` (_edge_count) | call | parameter — counts edges on the live store and on the backup (:1375); the live call passes `core.DB_PATH` | ro | tool (CLI) | direct | — | n/a — no owner path planned yet |
| 52 | `tools/cites_step3.py:1456` (_main) | call | live store — reads `core.DB_PATH` for label validation | ro | tool (CLI) | direct | — | n/a — no owner path planned yet |
| 53 | `tools/cohort_dispatch.py:142` (_fetch_nodes) | call | live store — docstring says read-only but opened without mode=ro; `--db` defaults to `~/.engram/knowledge.db` (:82) | rw | tool (CLI) | direct | — | n/a — no owner path planned yet |
| 54 | `tools/engram-fix-git-backup.sh:124` ((dry-run echo)) | prose | n/a — echo of the command that would run | n/a | tool (CLI) | out of scope | — | — |
| 55 | `tools/engram-fix-git-backup.sh:127` ((step 1: safety snapshot)) | call | live store — TWO connects on one line (the one row that holds two): `s` = `$DB_PATH` (`$ENGRAM_HOME/knowledge.db`, live, source of `s.backup(d)`) and `d` = `$SAFETY_SNAPSHOT` (other DB). Design §3 names this as the A5 shell channel | rw | tool (CLI) | direct | — | n/a — no owner path planned yet |
| 56 | `tools/engram-fix-git-backup.sh:133` ((step 1: node-count check)) | call | live store — `COUNT(*)` on `$DB_PATH` | rw | tool (CLI) | direct | — | n/a — no owner path planned yet |
| 57 | `tools/engram-fix-git-backup.sh:134` ((step 1: node-count check)) | call | other DB: `$SAFETY_SNAPSHOT` copy | rw | tool (CLI) | out of scope | — | — |
| 58 | `tools/engram-pkg/engram-pkg:204` ((`_DUMP_SH_TEMPLATE`, rendered into a package's scripts/dump.sh)) | call | other DB: package `$PKG_DIR/knowledge.db` — line sits inside a template string | rw | tool (CLI) | out of scope | — | — |
| 59 | `tools/engram-pkg/engram-pkg:226` ((`_DUMP_SH_TEMPLATE`)) | call | other DB: in-memory DB | rw | tool (CLI) | out of scope | — | — |
| 60 | `tools/engram-pkg/engram-pkg:253` ((`_DUMP_SH_TEMPLATE`)) | call | other DB: package `$DB_FILE` | rw | tool (CLI) | out of scope | — | — |
| 61 | `tools/engram-pkg/engram-pkg:282` ((`_BUILD_SH_TEMPLATE`, rendered package scripts/build.sh)) | call | other DB: package `$DB_FILE` (rebuilt from the package's .sql) | rw | tool (CLI) | out of scope | — | — |
| 62 | `tools/engram-pkg/engram-pkg:284` ((`_BUILD_SH_TEMPLATE`)) | call | other DB: package `$DB_FILE` | rw | tool (CLI) | out of scope | — | — |
| 63 | `tools/engram-pkg/engram-pkg:285` ((`_BUILD_SH_TEMPLATE`)) | call | other DB: package `$DB_FILE` | rw | tool (CLI) | out of scope | — | — |
| 64 | `tools/engram-pkg/engram-pkg:887` (_write_canonical_knowledge_sql) | call | other DB: package DB (`pkg_db_path`) | rw | tool (CLI) | out of scope | — | — |
| 65 | `tools/engram-pkg/engram-pkg:1007` (_populate_package_db) | call | other DB: package DB (`pkg_db_path`), written | rw | tool (CLI) | out of scope | — | — |
| 66 | `tools/engram-pkg/engram-pkg:1101` (cmd_scope_export) | call | live store — `source_db = ENGRAM_HOME/knowledge.db` (engram-pkg:1086-1089) — the design doc §4.2 table calls engram-pkg's sites package DBs; THIS one is the seat's live store, read-only | ro | tool (CLI) | direct | — | n/a — no owner path planned yet |
| 67 | `tools/engram-pkg/engram-pkg:1230` (_render_read_pkg_node) | call | other DB: package DB (`pkg_db_path`) | ro | tool (CLI) | out of scope | — | — |
| 68 | `tools/engram-pkg/engram-pkg:1392` (cmd_render) | call | other DB: package DB (`pkg_db`) | ro | tool (CLI) | out of scope | — | — |
| 69 | `tools/engram-regenerate-embeddings.py:280` (backfill_embeddings) | call | live store — WRITES embeddings; `engram_home/knowledge.db` (:237) | rw | tool (CLI) | direct | — | n/a — no owner path planned yet |
| 70 | `tools/inspect_raw.py:70` (_get_row) | call | live store — reads one row; `DATA_DIR/knowledge.db` (`ENGRAM_HOME` or ~/.engram) | rw | tool (CLI) | direct | — | n/a — no owner path planned yet |
| 71 | `tools/migrate_person_nodes.py:213` (_get_db) | call | live store — WRITES (migration); `--db` default `~/.engram/knowledge.db` | rw | tool (CLI) | direct | — | n/a — no owner path planned yet |
| 72 | `tools/migrate_prediction_pairing_edges.py:79` (_get_db) | call | live store — WRITES (migration); `--db` default `~/.engram/knowledge.db` (:177) | rw | tool (CLI) | direct | — | n/a — no owner path planned yet |
| 73 | `tools/migrate_stranded_supported_by.py:70` (_get_db) | call | live store — WRITES (migration); `--db` default `~/.engram/knowledge.db` (:197) | rw | tool (CLI) | direct | — | n/a — no owner path planned yet |
| 74 | `tools/reconcile_lesson_demotions.py:223` (find_candidates) | call | live store — READ-ONLY; `--db` defaults to `ENGRAM_HOME/knowledge.db` (`DEFAULT_DB_PATH`, :124); sole in-tree caller :272 | ro | tool (CLI) | direct | — | n/a — no owner path planned yet |
| 75 | `tools/surgical.py:87` (_get_db) | call | live store — WRITES (DB surgery tool); `DATA_DIR/knowledge.db` (:62) | rw | tool (CLI) | direct | — | n/a — no owner path planned yet |
| 76 | `viz/data/sqlite_ro.py:28` (connect_ro) | call | parameter — the single viz read-only opener; callers pass each agent's knowledge.db (viz/agents.py:463, data/graph.py, calibration.py, health.py, health_v2.py, moments.py) and, for stats, `logs/index.db` (viz/data/stats.py:173, not the store) | ro | viz | direct | — | n/a — no owner path planned yet |

Paths are relative to `src/engram/`.

## 3. The read-template registry — the migration queue

Source: `src/engram/engramd_read_templates.json` at `21ec2944` (schema 1, 26 template entries). A template is one registered SQL shape; `sites` are the `file:function` callers allowed to use it. This is the per-caller view the hook-migration PRs update. Counts are **distinct templates per file**; one template can be registered for several callers. Read-only hooks first, fewest templates first.

| order | caller (file) | distinct templates | functions (template count) | state | unit | A6 delta | macOS |
|---|---|---|---|---|---|---|---|
| done | `hooks/claude/engram-user-identity-hook.py` | 1 | `get_person_nodes` (1) | behind-flag (`engramd.user_identity_reads_via_owner`, default OFF) | U4d, #3341 (merged 17f69f5a0) | 3.45.1: seq 0.90x / cont 0.24x / cold 0.20x; 3.53.0: seq 0.90x p50, 0.95x p99 (gate holds) / cont 0.26x / cold 0.19x — see A6 | unreachable via owner (Darwin arm #3666) |
| 1 | `hooks/claude/engram-stop-hook.py` | 1 | `_maturity_muted` (1: `SELECT count(*) FROM nodes WHERE is_current = 1`) | direct | U6a, #3664 (flag `engramd.stop_reads_via_owner`) | — | unreachable via owner (Darwin arm #3666) |
| 2 | `hooks/claude/_hooklib.py` | 1 | `_focused_ids` (1) | direct | shared library; sequenced after the single-hook units | — | unreachable via owner (Darwin arm #3666) |
| 3 | `hooks/claude/engram-lesson-tripwire-hook.py` | 3 | `load_tripwires` (1), `load_prevention_lessons` (1), `check_in_turn_recall` (1; template shared with `engram_idf`) | direct | U6b, #3665 (flag `engramd.lesson_tripwire_reads_via_owner`) | — | unreachable via owner (Darwin arm #3666) |
| 4 | `hooks/claude/engram-surface-hook.py` | 4 | `_check_embedding_restore_flag` (1), `_dormant_focus_set_candidates` (2), `_advance_person_moment_cursor` (1, also a registered write site) | direct | no unit filed that I found | — | unreachable via owner (Darwin arm #3666) |
| 5 | `hooks/claude/engram-session-start-hook.py` | 8 | `sleep_status_block` (2), `session_checkpoint_block` (1), `_check_embedding_restore_flag` (1), `saved_focus_sets_block` (2), `_check_backup_staleness` (1), `focus_block` (1) | direct | no unit filed that I found | — | unreachable via owner (Darwin arm #3666) |
| n/a | `engram_core.py` | 8 | `generalization_chain` (5), `_current_successor` (2), `_resolve_to_current` (1) | direct | non-hook; per design §6 the MCP server migrates last | — | unreachable via owner (Darwin arm #3666) |
| n/a | `engram_idf.py` | 2 | `extract_keywords` (1), `idf` (2): 3 function-template pairs over 2 distinct templates; one of them is the COUNT template shared with the tripwire hook | direct | non-hook; see U6b's shared-template requirement | — | unreachable via owner (Darwin arm #3666) |
| n/a | `engram_restamp.py` | 1 | `bump_utility` (1, also a registered write site) | direct | non-hook | — | unreachable via owner (Darwin arm #3666) |

**Registered write sites (3):** `engram_restamp.py:bump_utility` · `hooks/claude/engram-surface-hook.py:_advance_person_moment_cursor` · `hooks/claude/engram-utility-credit-mention-stop.py:bump_utility`.

**Authorizer-governed (1):** `hooks/claude/engram-surface-hook.py:_check_db_liveness`.

**Declared out of scope (2):**

- `hooks/claude/engram-session-start-hook.py:_check_json1`: a JSON1 probe on a fresh in-memory connection (#3327); it never touches the owner-held store.
- `hooks/claude/engram-surface-hook.py:_query_attached_packs`: a package-DB read (an attached pack opened `mode=ro&immutable=1`); not the owner-held store.

Both reasons cite `docs/2652-security-slice-design.md` §3.5.

## 4. A6 — the one measured delta

Criterion (design §6.1, stated there once): the hook's owner round trip must not exceed the same hook's direct-connect path. Applies to the user-identity hook (U4d, #3341, merged 17f69f5a0). Both runs: Linux WSL2, 32 CPUs, synthetic 5,005-node store copy (2.7 MB, 5 person rows), N=1000 per path per arm, a real owner process. Ratio = owner p50 / baseline p50.

| run | SQLite | sequential, warm (the gate) | contended, 2 threads (owed, not a gate) | cold cache (owed, not a gate) | source |
|---|---|---|---|---|---|
| PR body | 3.45.1 (Python 3.12.3) | **0.90x** (0.720 -> 0.650 ms p50) | 0.24x | 0.20x | #3341 PR body |
| operative run under the plugin venv | **3.53.0** (Python 3.14.4) | **0.90x p50 / 0.95x p99** (0.741 -> 0.670 ms p50; 1.068 -> 1.015 ms p99) | 0.26x (p99 0.22x) | 0.19x (p99 0.18x) | Borges, #3341 comment 5972328311, 2026-10-03, head `73fc361c` |

The gate holds under 3.53.0, the build hooks actually run under; no arm loses. **Bounds, as stated in those two places:** one host, one synthetic store size (smaller than a real seat's); function-level timings exclude interpreter start; the contended threads share one GIL on both paths; the cold arm evicts only the OS page cache, so the owner's own SQLite cache stays warm and that arm partly measures the held connection. These are not measurements of any other hook. This ledger records no other delta because none has been taken. Not taken on any Mac: the owner cannot start there.

## 5. How this ledger stays current

- **Each migration PR updates its own rows in the same PR:** its Section 3 row (state, unit, A6 delta) and, when the hook's own `connect` line changes state, the matching Section 2 row. U6a (#3664) and U6b (#3665) both name this file in their contract. A PR that moves a hook and does not touch this file is incomplete.
- **State changes to record:** `direct` -> `behind-flag` when the flag lands default OFF; `behind-flag` -> `flag-on` only when the flag is ON in production (needs the U5 launcher, and a Linux seat); `flag-on` -> `retired` when the old path is deleted.
- **A6 deltas:** record the venv SQLite version and the bounds with the number. Never carry a ratio from one SQLite build to another (design §6.1).
- **Re-measure the population** at the PR's own tip with these commands, and update the ref and counts in Sections 1-3 if they moved:

```
git grep -c 'sqlite3\.connect(' -- src/engram        # per-file line counts; sum = lines, row count = files
git grep -n 'sqlite3\.connect(' -- src/engram        # the lines, to diff against Section 2
python3 -I - <<'EOF'   # registry: distinct templates per caller file
import json, collections
d = json.load(open('src/engram/engramd_read_templates.json'))
by = collections.defaultdict(set)
for i, t in enumerate(d['templates']):
    for s in t['sites']:
        by[s.split(':')[0]].add(i)
print(sorted((len(v), k) for k, v in by.items()))
print(d['write_sites']); print(d['authorizer_governed']); print([x['site'] for x in d['declared_out_of_scope']])
EOF
```

- `tools/check_2652_ledger.py` (run in `pr-checks`, tested by `tests/test_2652_ledger_completeness.py`) checks per-file count completeness: for each tracked file under `src/engram`, the number of Section-2 rows naming it must equal the number of `sqlite3.connect(` lines in it. It does not check row contents (target, state, function), line numbers, or Section 1's tallies, and a swap inside one file (one connect removed, another added) passes. When it fails, add or remove a Section-2 row for each connect line you added or removed.

*Sources read for this version: `docs/2652-engrammanager-design.md` §3, §4.1-4.2, §5.5, §6, §6.1; `src/engram/engramd_read_templates.json`; #3663, #3664, #3665, #3666; the #3341 PR body and comments.*
