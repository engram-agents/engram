# Alpha-release open-thread inventory — 2026-05-07

**Purpose:** prioritization-ready list of every alpha-side thread that's planned-but-not-implemented, planned-but-stalled, or in-flight, so the morning's thread 2 (alpha-release planning with Lei) can run against a complete picture rather than a partial recall.

**Compiled:** 2026-05-07 morning, autonomous burst while Lei is out (breakfast → school → home).

**Sources crossed:** ~/engram-alpha/active-work/ (8 docs), recent dev commits (28 since the multi-agent runtime base), open PRs (1: #34), ask-lei.md queue, ENGRAM open questions/tasks, yesterday's history file.

---

## §1 — What's shipped (so prioritization isn't redundant with completed work)

Sorted by dev commit order (newest first):

| # | Title | Status note |
|---|---|---|
| #35 | context_tracker: post-compact byte-offset marker | Merged + deployed today 08:25 EDT |
| #27 | hooks: antml-repair invocation counter + design docs | Telemetry baseline accumulating from 2026-05-06 onward |
| #20 | engram-sleep: dispatch dream-fairies in parallel during sleep cron | First successful run 2026-05-06 night (6 fairies, 91 nodes) |
| #32 | deference-detector: catch future-promise patterns | Stack on #31 |
| #31 | hooks: deference-detector (phrase + structural-intent) | Foundation for qu_0242 longitudinal validation |
| #30 | deploy.sh: 3-way conflict check for SKILL.md | Stops silent overwrites |
| #29 | engram_query_pattern: 6-pattern library + telemetry | Closes design from active-work/engram-query-pattern-design-2026-05-06.md |
| #28 | tests: drop hardcoded /home/user sys.path entries | Cleanup |
| #26 | tests: rewrite 5 skipped tests for engram-alpha API drifts | 168/0/0 passing |
| #25 | tests: pytest suite + GitHub Actions CI on PR→dev/master | Foundation |
| #24 | context_tracker: detect 1m mode from active-session.json marker | 5× over-report fix |
| #22 | viz_server: collapse detail-panel to 0 width when no node selected | UX polish |
| #21 | viz_server: add ?immutable=1 for cross-agent profile switching | SQLite WAL fix |
| #19 | spawn-implementation v0.1: agent-bootstrap + agentctl spawn/finalize-name | Phases 1-4 (claude only); Phase 5 real-spawn validation pending |
| #18 | agents: add engram-dream-fairy (read-only consolidation scanner) | Used in #20's dispatch |
| #17 | engram-config-surface: schema v2 + sync-cron + install integration | Phases 1-3 of active-work/engram-config-surface-2026-05-04.md; Phases 4-6 pending |
| #13 | viz-server: multi-agent refactor | Replaces systemd-per-agent split |
| #12 | feat: Gemini sidecar migration | hooks/gemini + skills/gemini |
| #11 | feat: layered sidecar refactor (hooks/claude + skills/claude) | Per dv_0557 |
| #10 | feat: split engram_checkpoint into engram_nap + engram_advance_turn | Sleep/nap modal split |
| #9 | fix: raise drowsiness thresholds to 80%/90% | Calibrated for both context modes |
| #7 | tools/deploy.sh — deliberate alpha → ENGRAM_HOME deploy | Foundation |
| #6 | feat: port file-versioning + personal_communication enum from kg_proto | Provenance |
| #5 | fix: drowsiness mode-mixing + engram_stats response shape | |
| #4 | feat: engram-time-bar-hook for ambient time awareness (MECH-1) | |
| #3 | feat: session-start hook reads ~/.engram/history/ | Replaces daily-highlights.md |
| #2 | feat: promote 11 skills into alpha | Universal + research-loop + school-day |
| #1 | feat: multi-agent runtime — heartbeat, dispatcher, sleep, install support | |

---

## §2 — Pending work

### §2.A — Lei-blocked (waiting on Lei input/sudo/decision)

| ID | Item | What's needed from Lei | Leverage | Effort once unblocked |
|---|---|---|---|---|
| L1 | **PR #34 OpenClaw plugin Phase 1** — scoped imports + bootstrap-marker fixes shipped; 4 SDK surfaces flagged inline as TODO | **Q1 from earlier still open: where does the plugin live? Inside the OpenClaw monorepo or in engram-alpha as a separate package? `workspace:*` only resolves in the former.** Plus review of the 4 SDK TODOs (`api.log` vs `api.logger`, `mutateConfigFile` chain, `workspace.dir`, devDep `workspace:*`). | High — blocks alpha-as-OpenClaw-plugin shipping vector | ~2-4 hr to flesh out the 4 SDK surfaces once decided |
| L2 | **Phase 5 spawn-implementation real-world validation** | Lei sudo to spawn a third agent end-to-end. Lei runs the 4 commands; agent picks its name; Lei finalizes. Sub-agent reviewer flagged: design doc lists a one-shot smoke-test script as v0.1 deliverable but the branch ships none — could be written first if you want. | High — first multi-agent helper unlocks paper drafting velocity per dv_0559 | ~30 min interactive; needs ~2 hr of pre-work to write the smoke-test script if we want one |
| L3 | **Spawn-flow open questions Q2-Q4** (active-work/spawn-implementation-2026-05-05.md §"Open questions for Lei") | Q2: newborn engram-alpha source — clone fresh per-agent vs symlink to shared (Lei's lean: clone fresh). Q3: OAuth bootstrap — `claude login` first thing inside `agentctl shell`. Q4: helper agent's first goal — suggest "ENGRAM ops helper" framing or let it discover. | Medium — informs Phase 5 ceremony | Decisions only; no code |
| L4 | **Paper proofread findings (8 items, 2026-05-05)** | Each needs a voice/scope decision: "fails loudly" overstating automation; missing third failure mode (volatile-errors); third-person→first-person in §7.3; text-layer architectural reframe; §7.1-before-§7.3 reordering; working-draft-artifact purge; inline-IDs→footnotes; §7.3.3 example evidence. | High — gates pre-arxiv pass | Per-item: 15-60 min after Lei calls each one |
| L5 | **Mneme migration script gap** | Review when convenient — `migrate-borges-to-uid` left `{{USER_NAME}}` / `{{AGENT_NAME}}` placeholders unresolved on new uid | Low (one-off) | ~30 min fix |
| L6 | **PR #28** in ask-lei.md history | ✓ Already merged 2026-05-07 00:06 UTC — drop from queue (done in this morning's cleanup) | — | — |

### §2.B — Time-gated (waiting on data accumulation, not decision)

| ID | Item | Gate | Earliest action date | Effort once unblocked |
|---|---|---|---|---|
| T1 | **antml-fix migration to single-JSON-payload** (active-work/antml-fix-migration-plan-2026-05-06.md) | Baseline accumulation in `~/.engram/toolcall-invocations.jsonl`. Need ≥1000 invocations + ≥2 weeks calendar time. Current rate: 22.45% (ob_1531 borges_session_eval read). | ~2026-05-20 (2 weeks from telemetry start) | ~200-400 LOC server.py + client.py + test shim + new test file. ~4-6 hr focused work. ls_0023 prescribes rollback discipline. |
| T2 | **qu_0242 longitudinal deference-detector validation** | ~30 days of detector log data | ~2026-06-05 | Mostly analytical — read the logs, characterize per-pattern fire rates, decide whether to add new patterns or close qu_0242 |

### §2.C — Autonomous-doable (no blockers; just needs work)

| ID | Item | Why now / leverage | Effort | Outputs |
|---|---|---|---|---|
| ~~A1~~ | ~~**tools/deference_baseline.py**~~ — DONE 2026-05-07 morning, **PR #36** opened against dev. Smoke-tested on the 1-entry log; awaiting volume to characterize per-label patterns. | — | — | — |
| ~~A2~~ | ~~**Stale-cascade cleanup**~~ — CLOSED 2026-05-07 morning as phantom task. Empirical investigation: 0 stale-status nodes; 6 unrelated tainted markers; goal supersedes properly wired; text references to old IDs are correct as historical context per warm-briefing. Dream-fairy already resolved the one Tier-A actionable (tk_0001). | — | — | — |
| A3 | **Config-surface Phase 4-5: Settings tab in viz_server** (active-work/engram-config-surface-2026-05-04.md §Phase 4-5) | Phases 1-3 already shipped (#17). The UI tab makes runtime tunables (memory tiers, drowsiness thresholds, cadence) editable without hand-editing config.json. | ~3 hr (Phase 4 + 5 estimate) | New `/api/config` GET/POST + `/settings` HTML + "Apply schedule changes" button shelling to sync-cron.sh with diff |
| A4 | **Config-surface Phase 6: USER_GUIDE on cadence tuning** | Doc-only; ~30 min. Closes the active-work doc cleanly. | ~30 min | README section + USER_GUIDE entry |
| ~~A5~~ | ~~**Config-surface live `~/.engram/config.json` v2 migration**~~ — DONE 2026-05-07 morning. Ran `tools/migrate_config_v2.py` (already existed); backup auto-created; schema now v2; cadence block matches live crontab. | — | — | — |
| ~~A6~~ | ~~**MECH-N proposal: `engram_audit_string(needle)`**~~ — DONE 2026-05-07 morning, **PR #37**. 125 LOC tool + 10 tests + SKILL.md entry; 195/195 tests pass. Used as design-validation: confirmed Mao-Cao cleanup held (21 current claim-text hits all intentional). | — | — | — |
| ~~A7~~ | ~~**Lit-scan name-leak sweep**~~ — DONE 2026-05-07 morning. Comprehensive grep across alpha + kg returned zero residual leak sites. All "Mao-Cao" hits are intentional historical/case-study references. The 2026-05-05 cleanup held empirically. | — | — | — |
| A8 | **gl_0027 syllabus growth — pick a domain gap and dig** | Standing implicit-queue work per CLAUDE.md generative-curiosity section. No specific deliverable; the work IS the syllabus. | Open-ended (1-3 hr) | New ENGRAM nodes in chosen domain; possible new lessons; possible feedback memories |
| A9 | **Paper revision — apply mechanical fixes from §1.5 / §1.3** | Lei already endorsed these last session; they're pre-applied. Verify clean. | ~5 min check | Verification only |

### §2.D — Forthcoming epics (post-paper, parked but logged)

| ID | Item | Why post-paper |
|---|---|---|
| E1 | **Sub-agent MCP per-tool granularity** (`agentctl create-agent` flow OR docs OR Anthropic feature request) | YAML-only authoring path is fine for internal use; not v1-blocking |
| E2 | **Config-data file split** (`engram-alpha/templates/config.default.json` ↔ `~/.engram/config.json`) | Lei's 2026-05-05 PM comment; clean split for install-time-generic vs per-agent-runtime. Not v1-blocking. |
| E3 | **Multi-runtime spawn (gemini-cli + codex)** | v0.1 dropped non-claude support per 2026-05-05 PM scoping. Real second-runtime use case is post-paper. ~500-1000 LOC scoped. |
| E4 | **Slim `/home/user/.claude/CLAUDE.md`** | Touches identity-loading; defer until post-paper |

---

## §3 — Suggested prioritization (my read; Lei adjusts)

**Highest leverage if Lei gates land first:**
1. **L1** (PR #34 OpenClaw decision) — opens the OpenClaw distribution vector
2. **L2** (Phase 5 spawn validation) — third agent unlocks paper drafting velocity per dv_0559
3. **L4** (paper proofread decisions) — gates pre-arxiv pass

**While waiting on Lei gates, autonomous-doable order:**
1. **A1** (deference_baseline.py) — front-loads T2; small + scoped
2. **A5** (live config.json v2 migration) — small + closes a known-broken state
3. **A6** (engram_audit_string MECH-N) — concrete value, already-burned-us
4. **A2** (stale-cascade cleanup) — graph-quality investment
5. **A3-A4** (config-surface Phase 4-6 UI) — closes the active-work doc cleanly; medium effort
6. **A7** (lit-scan name-leak sweep) — depends on A6 timing; if A6 lands first this is trivial
7. **A8** (gl_0027 syllabus growth) — open-ended; pick when above are done

**Time-gated (set-and-forget):**
- T1, T2 are calendar-bound; nothing to do until the dates arrive

**Forthcoming epics:** E1-E4 — file under post-paper

---

## §4 — Discrepancies / things worth a sanity check with Lei

1. **Q1 from PR #34** has been open for at least a week (per ask-lei). Is the OpenClaw monorepo location something we can decide today, or does it depend on conversation with the OpenClaw team?
2. **Phase 5 spawn validation** — what's the minimum conditions that have to hold before we run it? (Newborn smoke-test script written? OAuth pre-staged? A specific window of time?)
3. **antml-fix migration timing** — is "2 weeks from 2026-05-06" a hard gate, or is the data already stable enough that we could move sooner? Worth a peek at the rate's variance over the last 24h.
4. **Stale-cascade cleanup** has been on the list since the 2026-04-16 rewrite (3 weeks). Is there a reason to keep deferring, or is it just lower-priority than always-fresh work? If lower-priority, this is exactly the kind of thing a 3rd helper agent could take.
5. **Config-surface Phase 4-6** — the UI tab — does this still feel high-priority post-paper, or has the use case decayed (we've gotten by hand-editing config.json fine for a month)?

---

*Generated autonomously while Lei was out. Not committed to git — this is a planning artifact for thread 2 with Lei when he returns.*
