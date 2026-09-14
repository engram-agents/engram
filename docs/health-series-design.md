## Health series engine — design v1 (Kepler, 2026-09-05 14:1xZ) — for Borges + Ariadne design read before any builder is dispatched

**Lei's ruling (13:43Z):** wait for REAL cross-day history before any curve decision; the git-history replay is "a really powerful REGULAR major version bump backfill mechanism, so it's worth the effort to build it right, not a one-off analysis … it becomes part of our health data bundle tightly coupled with our snapshots database." This supersedes the "offline replay tool writes series files" sketch in my #2518 contract §1. Everything below is designed to that ruling.

### 0. Measured ground (my seat, today — numbers a reviewer can re-take)

| fact | value | how |
|---|---|---|
| knowledge.sql commits | **136**, 2026-06-09 → 2026-09-05, 1–11 per day | `git -C ~/.engram log --format=%h -- knowledge.sql \| wc -l` |
| full-history tolerant L0 replay | **19.2 s** total: 17.3 s restore, 1.9 s compute, 0.14 s/commit | probe: `git show <sha>:knowledge.sql` → in-memory sqlite (`row_factory=Row`) → every `MeasurementDef.compute_fn` |
| commits predating `cascade_markers` | **102 of 136** — the 2 marker measurements raise `no such table` there | same probe; `compute_all_measurements` does NOT catch it, so a naive replay crashes on commit 1 |
| embeddings in the dump | **NULL on every commit** — `engram_backup.dump_stripped` runs `UPDATE nodes SET embedding=NULL` before dumping | `engram_backup.py:145` |
| `user_version` across history | 1:4 · 2:4 · 3:73 · 4:21 · 5:34 | probe |
| turn number | **not in the dump** — lives in `config.json` | `engram_core.py:444` |
| stored snapshot `anchor` | **off by one**: both of today's snapshots name the commit BEFORE their own checkpoint (`…134317Z.json` → `71c9956`; the checkpoint's own commit `8d7bfc3` landed the same second) | `_checkpoint_internal` calls `_commit_snapshot` (async enqueue since #1673) THEN `write_snapshot`, whose `_anchor_commit_sha` reads HEAD |
| my stored snapshots | **old-format** (no `measurements`, no `health_version`) — live plugin built 09-04 08:19 EDT, before the `health/` fold and every v2 merge | corrected on #2518 at 13:58Z |

The last two rows are what turned this from "a tool" into "a subsystem": the stored source is empty on any seat that has not rebuilt, and the field that was supposed to join stored points to git history points one commit early.

### 1. The one idea: a point is `(knowledge.sql commit, health MAJOR)`. `stored` and `git_replay` are two ways of computing the same row.

Every checkpoint that writes a health snapshot also commits `knowledge.sql`. So a stored snapshot's L0 block and a replay of that commit's dump are **the same measurement of the same graph state** — except for the declared unreplayable measurements (§3). That gives the engine:

- **A single store keyed by `(commit_sha, health_major)`**, not two series formats stitched together. `source` says how the row was computed; it is provenance, not a partition.
- **A built-in equality control.** For every commit that has BOTH a stored snapshot and a replay, the rows must agree on every replayable measurement. This is the engine's correctness test, in the same spirit as `_verify_l1_against_live_signal_fns`. It runs on real data every night, not only in CI.
- **MAJOR bump = "rows under the new MAJOR are missing for every commit" = the same code path as gap-fill.** No special backfill mode. The regular mechanism Lei asked for falls out of the key.

**Anchor resolution (because of the off-by-one):** a stored snapshot's true commit is *the first `knowledge.sql` commit with committer time ≥ the snapshot's timestamp*, bounded to the async worker's lag (fail loud if none lands within, say, 10 minutes — that is a worker-health finding, not a series row). The snapshot's own `anchor` field is recorded as `anchor_pre_commit` for audit and never used as the key. Upstream fix is a follow-up (§8), and the engine must keep tolerating the old field forever because those files exist.

### 2. Store: `DATA_DIR/health-snapshots/series.db` (sqlite), derived from snapshots + git, rebuildable from scratch

Why sqlite and not per-version JSON files (what #2518 §1 said): the regular backfill needs *"which commits have no row under the current MAJOR"* — a set difference. With JSON files that is read-everything-then-diff; with a table it is one query. Idempotent upserts and per-`(version, signal)` reads are the other two query shapes, and both are native. The JSON snapshot files remain the SSoT of stored checkpoints; `series.db` is an index plus the replay results, and `series rebuild` must reproduce it from `(snapshots/*.json + git)` — that rebuildability is the safety property, and a test.

```
l0_points        (commit_sha TEXT, health_major INT, commit_time TEXT, turn INT NULL,
                  source TEXT CHECK(source IN ('stored','git_replay')),
                  snapshot_file TEXT NULL,              -- stored only
                  anchor_pre_commit TEXT NULL,          -- the snapshot's own (off-by-one) anchor, audit only
                  computed_at TEXT, engine_build TEXT,  -- plugin/source commit that computed the row
                  measurements JSON,                    -- the L0 block, ints/bools/lists; null for unreplayable
                  unreplayable JSON,                    -- {name: reason} — see §3; {} for stored rows
                  PRIMARY KEY (commit_sha, health_major, source))
derived_points   (commit_sha, health_version TEXT,     -- 'health-v1.0' — MAJOR.MINOR
                  l0_source TEXT CHECK(l0_source IN ('stored','git_replay')), -- (PR C1) which l0_points row this was derived from
                  replay_decl_hash TEXT DEFAULT '',     -- (PR C1) '' for stored-derived rows, the l0 row's hash for replay-derived
                  signals JSON, scores JSON, computed_at, engine_build,
                  PRIMARY KEY (commit_sha, health_version, l0_source, replay_decl_hash))
derive_checks    (commit_sha, health_version, verdict TEXT CHECK(verdict IN ('agree','disagree')), -- (PR C1)
                  diff JSON, checked_at)               -- derive's own equality control, mirrors equality_checks
equality_checks  (commit_sha, health_major, verdict TEXT CHECK(verdict IN ('agree','disagree')),
                  diff JSON, checked_at)
replay_runs      (run_id, started_at, finished_at, health_major, reason TEXT
                  CHECK(reason IN ('initial','gap_fill','major_bump','verify','rebuild','derive')),
                  commits_total, commits_done, engine_build)
```

Rules that make this a "database tightly coupled with the snapshots", not a cache:

- **Rows are never mutated.** A new MAJOR adds `l0_points` rows; a new MINOR adds `derived_points` rows. Old versions stay readable byte-for-byte (a test diffs them across a bump).
- **`derived_points` is derivation, not truth.** Signals and scores are computed from `l0_points.measurements` through the *declared* `SIGNAL_DEFS` / `CurveSpec` / model of the named `health_version` — the same functions the live engine uses, on the same inputs. `rescored` from #2518 §1 is not a third source; it is "derived_points for a newer MINOR."
- **A null L0 value derives to `UNAVAILABLE`**, never to 0 and never to a crash. Today `_rate(None, n)` would raise; the deriver maps null → UNAVAILABLE before the formula (or `_rate` grows the guard — reviewer's call, I lean to the deriver so `signals.py` stays exactly the live path).
- **Old-format snapshot files are refused, loudly and per file** (`schema_version` present / `measurements` absent → logged as `skipped_old_format`, counted in the run record, never a row). Today every file on my seat is one of these.

### 3. Replayability is declared on `MeasurementDef`, not discovered by crashing

New field `replay: Literal["graph", "stripped", "environment"]` — L0 *metadata*, outside `canonical_l0_hash` exactly like `label` (#2582), so no MAJOR bump:

| value | measurements today | replay behaviour |
|---|---|---|
| `graph` | 14 | run `compute_fn` on the restored dump; on `sqlite3.OperationalError` (missing table/column at that commit) record `null` with reason `schema:<message>` — **never 0** |
| `stripped` | `claim_bearing_null_embedding` | `null`, reason `stripped_from_dump` — the dump would read 100 % null and lie |
| `environment` | the two embedder booleans | `null`, reason `environment_fact` — history of an install fact is not in the graph |

Consequence, stated so nobody is surprised at the first plot: over git replay, `embedding_coverage` is UNAVAILABLE on every commit, and the two marker signals are UNAVAILABLE before 2026-08-30 22:49Z (first commit carrying `cascade_markers`: `244bede`; last without: `d124bd3`, 16:54Z the same day). Stored v2 snapshots carry all 17, so once seats rebuild, those three signals get real history from the stored source forward. That is the honest shape of the data and the page should show it as "not measurable before <date>", not as a gap in a line.

### 4. Operations (one module, `health/series.py`, plus a `python -m engram.health.series` CLI; all idempotent)

- `ingest` — scan `health-snapshots/*.json`; refuse old-format; resolve the commit (§1); upsert `stored` rows. `ingest` does not run the equality check — its second operand, the `git_replay` row, does not exist until `backfill` writes it, so the comparison runs in `backfill`.
- `backfill` — the regular mechanism. For the current MAJOR: `commits(knowledge.sql) − {commit_sha in l0_points where health_major = current}` → replay each missing commit → upsert. First run on a seat = `initial` (all 136); after a MAJOR bump = `major_bump` (all, again, under the new number — old rows untouched); every other night = `gap_fill` (usually the commits of seats whose plugin was stale, or nothing). Then derive `derived_points` for the current `health_version` where missing.
- `derive` (PR C1) — for the current `health_version`, compute `derived_points` for every L0 row lacking one, from the STORED declarations (never HEAD's live `SIGNAL_DEFS`/`MODEL` for a non-current version — D4.1); the equality control compares each stored-derived row against its own snapshot's `signals`/`scores`.
- `verify` — replay the commits that have stored rows and record `equality_checks`. Any `disagree` is surfaced as a finding (it means either the dump, the measurement, or the anchor resolution is wrong — all three are things we want to know the day they break).
- `rebuild` — delete `series.db`, run `ingest` + `backfill` from scratch, diff against the previous db. The safety net for schema changes to the store itself.
- `export --version health-v1.0 [--signal X]` — the JSON shape `/api/health/v2/series` serves (`{version, points: [{t, commit, turn, source, num, den, raw, score, unreplayable}]}`), so the API is a thin reader.

**Where it runs:** replay uses `git show <sha>:knowledge.sql` into an **in-memory** sqlite — it never opens the live `knowledge.db`, never writes a temp DB, never touches the WAL. Environment measurements are never called during replay (§3), so `engram_core` is not imported on that path.

**When it runs (making "regular" mechanical — what a machine can check should be a mechanism, not a vigilance rule):** `backfill` is called best-effort from the post-dream checkpoint (`engram_lifecycle`, same posture as `write_snapshot` — a failure can never break the checkpoint). Nightly cost is the gap only: seconds. A MAJOR bump therefore backfills itself the first night after it ships, and the run record says so. The CLI stays for on-demand use and for the first run on a seat.

`derive` runs from the same post-dream checkpoint, not just `backfill`: `checkpoint_hook` (§4's lifecycle hook, `health/series.py`) runs `ingest` → `backfill` → `derive(..., snapshots_dir=snapshots_dir)` in that order, every time, each in its own `try`/`except` so one step's failure never blocks the next. So a seat whose CURRENT `health_version` has stale or never-derived rows (e.g. right after a rebuild, or a version ingested before its `derive` ever ran) self-heals automatically at its next sleep cycle, with no operator action — this is the normal path, not the exception. Manually running `derive` (the `python -m engram.health.series` CLI's `derive` subcommand, §4) is the same-day bridge for someone who wants the panels populated before the seat's next sleep, not the standard procedure.

### 5. Time axis and turn

`t` = the commit's **committer** time (a rebase-safe choice is irrelevant here — this repo is never rebased — but committer time is when the checkpoint landed, which is what we mean). `turn` is populated for stored rows (the snapshot carries it) and for replay rows only when the commit message states it (the sleep commits do: "turn 54->55"); otherwise `NULL`, never guessed. The detail view's history strip is time-based, so a null turn costs nothing.

### 6. API and page (Sol/Ariadne's side — stating the contract, not building it here)

- `GET /api/health/v2/series?agent=X&version=health-v1.0[&signal=…]` → `export` output. Read-only, fail-honest JSON per the #2571 rule (`{error, reason}` with distinct reasons: no db / version absent / seat unreadable). **(PR C1, built)**: the payload also carries a top-level `scores_available: bool` — `false` exactly when that version's `declarations.model_defs` is absent (a version ingested before PR C0), in which case every point's `score` is `null` and `aspects`/`composed` are empty — the page reads this flag rather than inferring "no scores" from empty arrays.
- `GET /api/health/v2/fleet` reads each seat's `series.db` latest row — same permission class as #2571 (the :5001 process reads other uids' data dirs).
- The page renders `unreplayable` as a labelled region ("not measurable before 2026-08-30: cascade_markers did not exist"), not as missing points.

### 7. Tests (the ones that make the claims falsifiable)

1. **Equality control with a positive control** — replay HEAD's dump vs `compute_all_measurements` on the same graph: agree on all `graph` measurements; then mutate one node row in the restored copy and assert `disagree` fires. (A control that cannot fail is not a control — the #2578 lesson.)
2. **Schema-missing → null with reason, never 0** — restore a dump with `cascade_markers` dropped; assert the two marker rows are `null`/`schema:…` and every other value equals the un-dropped restore.
3. **Old-format refusal** — a real old-format file from my seat as fixture; asserted skipped and counted.
4. **Idempotence** — second `backfill` performs zero replays (run record `commits_done = 0`).
5. **MAJOR bump leaves old rows byte-identical** — bump `MAJOR` in a test double, backfill, diff the old-major rows.
6. **Anchor resolution** — a snapshot whose `anchor` names commit N−1 resolves to commit N when its timestamp ≥ N's committer time; fails loud when no commit lands within the lag bound.
7. **Rebuild reproduces** — `rebuild` output equals the incrementally built db (ordered dump compare).

### 8. Upstream follow-ups this design depends on (separate small PRs, filed when this is accepted)

- **Anchor off-by-one:** have the async snapshot worker write `anchor_landed: <sha>` into the just-written snapshot file after its commit lands (same process, it knows the file), or record the pending `(turn, seq)` job key in the snapshot. The engine keeps §1's time-based resolution as the fallback for every file written before that fix.
- **`MeasurementDef.replay` field** (§3) — additive, unhashed, one PR.
- **Null-tolerant derivation** (§2) — deriver-side guard, no change to `signals.py`'s live path.

### 9. Ownership and slicing

I lead; coder-fairies build to spec; reviewer-fairy every push; Borges or Ariadne colleague per PR; I merge under the day-grant (dual role stated on each PR, as with #2577/#2582).

- **PR A — store + ingest + refusal + anchor resolution** (`series.py` schema, `ingest`, `rebuild` skeleton; tests 3, 6, 7).
- **PR B — replay driver + `replay` field + `backfill`/`verify` CLI** (tests 1, 2, 4, 5); depends on A.
- **PR C — derived_points + `export` + lifecycle hook**; `/series` wiring paired with Sol on his loader (#2581 just merged, so the endpoint has a home).

### 10. Questions for the design read (defaults stated; I proceed on them unless overruled)

1. **sqlite `series.db` vs JSON series files** — default sqlite for the set-difference and idempotence reasons in §2. If either of you sees the JSON-files shape winning on inspectability, say so; a `export` dump covers most of that.
2. **Derived cache or derive-at-read** — default: `derived_points` table filled at backfill for the current version, derive-at-read for any other requested version (rare, developer-tier).
3. **`_rate` None guard vs deriver-side mapping** — default deriver-side, keeping `signals.py` identical to the live path.
4. **Nightly `backfill` from the post-dream checkpoint** — default yes, best-effort; Lei's "regular" is the requirement this satisfies mechanically.
5. **For Lei:** the first real cross-day history any seat can show is 136 points of git replay on mine (with three signals honestly UNAVAILABLE over it). Is that the history you want the curves session run on, or do you want stored-v2 points to accumulate first? I read your ruling as the former; the engine serves both.

---

## Deltas accepted after the design read (2026-09-05, 14:1xZ–16:3xZ) — these amend the sections above

The design of record is this file. Where a delta below contradicts a section above, the delta wins; the section text is kept so the reasoning that led here stays readable.

### D1. Anchor resolution is `child_of_anchor`, not a job key and not time-first (amends §1, §8)

Read off `engram_snapshot_worker.py` and `engram_lifecycle.py` rather than reasoned: `write_snapshot` (lifecycle, after `_commit_snapshot` enqueues) is never handed the worker's `(turn, seq)`, so the snapshot file carries no job key; and `queue.jsonl` is compacted (job+done pairs dropped by `_compact_queue_locked`), so the key has no durable home. What the file does carry is `anchor` = HEAD of `~/.engram` at capture time = **the parent of the snapshot's own `knowledge.sql` commit**. Measured 6/6 on Kepler's six nap snapshots of 2026-09-05: each `anchor` is exactly the previous `knowledge.sql` commit; the true commit is the first `knowledge.sql` commit after `anchor` (`git log --reverse --format=%H <anchor>..HEAD -- knowledge.sql`, first line). Note that `anchor` may be a history-only commit (it was on the first snapshot of the day) — the walk is on the `knowledge.sql` path, so that is fine.

`anchor_resolution ∈ {child_of_anchor, by_time, ambiguous, unresolved}`, recorded on every stored row:

- `child_of_anchor` — exactly one snapshot file claims this `anchor` and a child `knowledge.sql` commit exists. Primary, deterministic.
- `ambiguous` — two or more snapshot files share one `anchor` (the worker lagged past two checkpoints). None of them is keyed; all are recorded in `unresolved_snapshots` with reason `shared_anchor`. The equality control (PR B) cannot adjudicate this, and a guessed key would be a false row.
- `by_time` — only for files with **no** `anchor` at all: the first `knowledge.sql` commit with committer time ≥ the file's timestamp, within a 10-minute lag bound.
- `unresolved` — no child commit yet (worker has not committed, or never will), or `by_time` found nothing within the bound. Recorded in `unresolved_snapshots(snapshot_file, anchor, reason, seen_at)`; re-`ingest` retries, since the worker may land the commit later.

The §8 upstream fix becomes: hand `write_snapshot` the `(turn, seq)` from `_commit_snapshot`'s result and have the worker stamp `anchor_landed` into the file after its commit. The engine tolerates the parent-pointer form forever — the 2026-09-05 files exist.

### D2. Replay declarations are fingerprinted into the `git_replay` key (amends §2, §3, §4 — Ariadne's §3 finding)

A `MeasurementDef.replay` flip (`stripped → graph`, when dumps start carrying embeddings) would leave every historical replay row saying `null / stripped_from_dump` with nothing firing: no MAJOR, `replay` is (correctly) unhashed, rows never mutate. Permanent holes indistinguishable from measured absences — the one thing §3 set out to avoid.

Fix, preserving §1's one-mechanism property and §2's never-mutate rule:

- `unreplayable` reasons carry a **class**: `{name: {reason, class}}` with `class ∈ {declared, discovered}`. `stripped_from_dump` / `environment_fact` are *declared* (predictable from `replay` without running anything); `schema:<message>` is *discovered* (known only by running against that commit's dump).
- `replay_decl_hash TEXT NOT NULL` = sha256 over the sorted `(name, replay)` pairs of the current `MeasurementDef` declarations, **part of the `l0_points` primary key** for `git_replay` rows; `''` for `stored` rows (they carry all 17 measurements). A flip changes the hash, every commit is now "missing" under `(MAJOR, current decl hash)`, and gap-fill fills them through the same path with `reason='gap_fill'` — the audit trail Ariadne asked for, with no mutation and no hand-bumped integer to forget. Old rows stay for audit; `export` serves the current hash.

### D3. §7 test 1 tightened (Ariadne)

The mutation names its target: retract one claim-bearing node in the restored copy, assert `claim_bearing_current` drops by exactly one, and assert the diff **names** `claim_bearing_current` and nothing else. Plus a false-positive control: mutate a column no `MeasurementDef` ranges over (chosen at test-writing time by reading `definition_inputs`, not assumed) and assert `agree`. A deriver that reported `disagree` on any change would pass the original test 1 and be useless.

### D4. Versions must be executable, not merely recorded (amends §2, §4 — Ariadne, #393)

Recording which instrument produced a row tells you rows are incomparable; it does not let you compare them.

1. `derived_points` for `health_version = X` are recomputed from `l0_points` through the declared `SIGNAL_DEFS` / `CurveSpec` / model **as of X**, not as of HEAD. The store keeps a `declarations(health_version PK, signal_defs, curve_defs, config, first_seen_commit, engine_build)` row per version, populated from the snapshots' own emitted blobs at ingest. The blobs are `signal_defs`, `curve_defs`, `config` **and `model_defs`** (PR C0: the per-signal curve/aspect/importance bindings and per-aspect points the scoring layer reads — without it a version's scores are recorded but not executable). A second snapshot under the same `health_version` with different blobs is a loud ingest failure (a version-discipline violation, never silently averaged). Test: derive `health-v1.0` rows after a synthetic MINOR bump; byte-identical to the pre-bump derivation.
2. `rebuild`'s diff is **two diffs**: regenerate under the stored `engine_build` (checkout of that sha's `health/` in a scratch tree) AND under HEAD; report *content changed* (same build, different rows — a data problem) separately from *build changed* (same content, different rows — a code problem). `engine_build` must therefore be a checkout-able sha, which makes the column load-bearing. That sha is resolvable in the first place because `engine_build` resolves by the executing module's own location (plugin tree → deployed `alpha_sha`; health checkout → its own HEAD; else `unknown`) — the full rule lives in `series.py`'s `detect_engine_build` docstring, not restated here.

### D5. Equality-control ambiguity retired (Borges, 14:43Z)

The binary capture is synchronous under the WAL read-mark and the residual span inside `_checkpoint_internal` holds no graph write, so a `disagree` verdict has one cause. Borges withdrew the finding after checking the capture path himself.

### Slicing after the deltas

- **PR A** — store schema (all tables above, including `replay_decl_hash`, `unreplayable` class shape, `anchor_resolution`, `declarations`, `unresolved_snapshots`), `ingest`, old-format refusal, anchor resolution (D1), `rebuild` (single diff — the two-diff form lands with replay in PR B), CLI skeleton, this document. Tests 3, 6, 7 + idempotence of `ingest` + the declarations gate.
- **PR B** — replay driver, `MeasurementDef.replay` field, `replay_decl_hash`, `backfill` / `verify`, two-diff `rebuild`. Tests 1 (as tightened in D3), 2, 4, 5. **Scope split, decided during PR B's own build**: PR B is further divided into **B1** (replay driver, `MeasurementDef.replay`, `replay_decl_hash`, `backfill` / `verify`, the `equality_check` control, and the CLI's `backfill`/`verify` subcommands — tests 1 as tightened by D3, 2, 4, 5, plus a D2 fingerprint-flip test) and **B2** (the two-diff `rebuild` from D4.2, a follow-up PR — this PR) — B2 needs a checkout of a historical `engine_build` into a scratch tree to compute the build-changed side of the diff, a real jump in blast radius from "replay a dump into `:memory:`" that does not belong in the same PR as the rest of B1's scope.
- **PR C** — `derived_points` from stored declarations (D4.1), `export`, lifecycle hook, `/series` wiring with Sol. **Split during PR C's own build, same reasoning as PR B's B1/B2 split above**: **PR C0** (BUILT — `health/model.py`'s `model_defs_jsonable`/`model_from_defs`, `health/engine.py`'s shared `score_signals(l1_signals, model)`, and the snapshot's new `model_defs` key — the model-layer declaration D4.1 needed but §2's original table list didn't yet have a home for) and **PR C1** (BUILT — this module's `derive`/`derive_point`/`export`/`checkpoint_hook`, the `derived_points`/`derive_checks` schema additions, and the `/api/health/v2/series` route, built on top of C0's `model_defs`).
