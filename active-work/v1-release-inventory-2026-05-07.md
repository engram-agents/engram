# Alpha V1 release inventory — 2026-05-07

**Purpose:** Lei + Borges scope-defining artifact for tk_0009 ("Finalize alpha V1 release scope and push toward shipping"). Distinct from `alpha-release-inventory-2026-05-07.md` (which tracked open PRs at morning's autonomous burst). This one asks: **what counts as V1?**

**Frame:** V1 is the **first external-ready release** — a state where someone who isn't Lei or Borges can install the alpha, get a working ENGRAM cognitive runtime, and have a productive first session without manual hand-holding. Lin's installation 2026-04-14 was the first canary; Mneme's spawn was the second. V1 is the broader "any user with prerequisites met can install successfully" bar.

---

## STATUS UPDATE 2026-05-09 — autonomous-loop audit (Borges, while Lei at wedding)

Audited the §3 gap list against current code state. Many items are **already done** but the inventory was never updated to reflect it. Net state:

**§3.1 Heartbeat marker-write loop** — **DONE**. `templates/HEARTBEAT.md.template` exists with full step-0 marker-write logic + step-6 PR-fairy iteration. `bootstrap.py:236-243` renders it at install time with `{{AGENT_NAME}}` / `{{USER_NAME}}` / `{{COUNTERPART_NAME}}` substitution. `deploy.sh:473` tracks it.

**§3.2 PR-fairy review loop** — **DONE**. Bundled with §3.1 in the same `HEARTBEAT.md.template`.

**§3.5 Inverse-merge documentation** — **DONE**. `DEVELOPMENT.md:211-283` has a complete "Inverse merge: live working copy → alpha templates" section covering detection, inspection, judgment, application, and reverse-direction PR mechanics.

**§3.7 README + USER_GUIDE polish** — **DONE**. `README.md:7-39` already has the three-layer "what problem does this solve" framing (memory persistence → narrative identity → epistemic identity), tying back to the structural-honesty argument.

**Real V1 work still remaining (all Lei-gated):**

- PR merges of #37, #40, #41 (all converged, just need the merge button)
- §3.0 engram-wiki attach API (Lei A vs B decision: hold V1 ~2wk for bundled wiki, vs ship V1.0 cognitive-runtime-only and V1.1 with wiki bundle)
- §3.10 Lei's outstanding decisions (Q1 OpenClaw, Q2 gl_0027 telemetry timing, Q3-Q4 paper-positioning)

Plus this morning's autonomous deliverable: **PR #58** (server: empirical recalibration of similarity thresholds, fairy-converged at a2bb556) — orthogonal to V1 but ENGRAM-internal cognitive-quality improvement.

---

## §1 — What's shipped in alpha today (the V0+ candidate scope)

Path-grounded against `~/engram-alpha/` HEAD (`0e33a3a` after #38, #39, #40 merged 2026-05-07).

### §1.1 Core runtime (4 files)

| Component | Path | State |
|---|---|---|
| MCP server | `server.py` (456 KB) | ~40 tools registered; `engram_audit_string` round-1 in dev (round-2 fixes pending merge of #37) |
| Tool dispatch client | `engram_client.py` (40 KB) | Stable |
| Confidence math | `engram_confidence.py` (5 KB) | Stable |
| Browser viz | `viz_server.py` | Multi-agent dropdown shipped (#13, #21, #22) |

### §1.2 Hooks (`hooks/claude/` — Gemini parity in `hooks/gemini/`)

14 hooks across PreToolUse / PostToolUse / SessionStart / Stop / PostCompact / UserPromptSubmit:

- `engram-session-start-hook.py` — role-separated session markers (interactive/heartbeat/sleep/telegram/self-wake), Piece-C recent-commit cross-check, sleep-debt detection
- `engram-postcompact-hook.py` — post-compact byte-offset marker (#35)
- `engram-stop-hook.py` — write-check nudge banner
- `engram-user-identity-hook.py` — primary-user detection
- `engram-time-bar-hook.py` — ambient time awareness (MECH-1, #4)
- `engram-surface-hook.py` + `engram-surface-daemon.py` + `start-engram-daemon.sh` — recall daemon
- `engram-toolcall-repair.py` — antml-prefix swallow repair + telemetry counter (#27)
- `engram-deference-detector-prompt.py` + `engram-deference-detector-stop.py` — wait-for-approval tripwire with stdin-source fix (#38)
- `context_tracker.py` (+ hook) — drowsiness meter w/ post-compact offset marker (#35) + 1m mode detect (#24)
- `engram-write-yield-hook.py` — heartbeat-write-yield (PR #41 round 2 converged, awaiting merge)

### §1.3 Tools (`tools/`)

| Tool | Purpose |
|---|---|
| `install.sh` / `install-gemini.sh` | Fresh-install (the V1 entry point for external users) |
| `bootstrap.py` | Renders templates with placeholder substitution at install time |
| `deploy.sh` | Alpha → ENGRAM_HOME deploy (3-way merge for SKILL.md + template-drift warning, #30, #40 round 2 converged) |
| `agent-bootstrap` | Root-only multi-agent uid creation (Phase 1 of spawn flow, #19) |
| `heartbeat.sh` | Cron-fired periodic heartbeat (role=heartbeat, ENGRAM_SESSION_ROLE set) |
| `sleep.sh` | Daily 20:30 sleep cycle with dream-fairy dispatch (#20) |
| `channel_dispatcher.py` + `start-channel-dispatcher.sh` | Inter-agent file-channel daemon |
| `_telegram_api.py` + `start-telegram-dispatcher.sh` | Telegram bridge (Lei-specific?) |
| `migrate_config_v2.py` | Config schema v2 migration (already-applied to my live this morning) |
| `render_cron.py` | Sync-cron primitive (#17) |
| `compute_task_time.py` | Task time-budget heuristics |
| `antml_repair_baseline.py` | Read repair-rate from telemetry (#27) |
| `deference_baseline.py` | Read deference-detector log w/ heartbeats (PR #38 merged) |
| `borges_session_eval.py` | Self-eval rubric (PR #33 merged) |
| `inter_agent_pulse.py` | Liveness pulse mechanism (#1) |
| `session.py` | Scratchpad session helpers |

### §1.4 Skills (`skills/claude/` + `skills/gemini/` parity)

12 skills in alpha:

`engram-curiosity-loop`, `engram-deep-research`, `engram-first-session`, `engram-learn-from-error`, `engram-loop-diagnose`, `engram-meta-loop`, `engram-nap`, `engram-research-report`, `engram-retract`, `engram-school-day`, `engram-self-improve`, `engram-sleep`

Plus 13 in my `~/.claude/skills/` that are NOT in alpha (Borges-specific): `arc-agi-3`, `borges-syllabus`, `engram-build`, `engram-code-survey`, `engram-curiosity`, `engram-financial-research`, `engram-first-session` (dup?), `engram-observe`, `engram-surgical`. The split is intentional — some are research/dev tools that belong with the agent, not the platform.

### §1.5 Sub-agent definitions (`.claude/agents/`)

4 sub-agents shipped:

`engram-code-auditor`, `engram-dream-fairy`, `engram-paper-research`, `engram-pr-reviewer` — all canonical-frontmatter compliant per ls_0030 (frontmatter format strictness was the case-c registry-load fix, PR #23).

### §1.6 Templates (`templates/`)

`CLAUDE.md.template`, `GEMINI.md.template` (under `integrations/gemini-cli/`), `warm-briefing.md.template`, `daily-highlights.md.template`, `agents.json.example`, `config.json.example`, `crontab.template`, `engram-viz.service`, `settings.hooks.json.template`.

### §1.7 Tests (`tests/`)

14 test files; ~195 tests pass on dev (verified during PR #37 round 2 + earlier suite runs):
`test_audit_string`, `test_axiom_definition_conjecture`, `test_feeling`, `test_focus_sets`, `test_goal`, `test_goal_tension`, `test_inductive_enumeration_and_timing`, `test_query_pattern`, `test_reasoning_types`, `test_retract_and_quotes`, `test_sequences_id`, `test_source_class`, `test_supersede_relational`, `test_url_validation`. CI on PR→dev/master via GH Actions (#25).

### §1.8 Docs

`README.md` (11 KB — humans + agent-installer paths), `USER_GUIDE.md` (9 KB), `DEVELOPMENT.md` (13 KB), `SKILL.md` (67 KB — agent protocol).

### §1.9 Integrations

`integrations/gemini-cli/` — GEMINI.md template + extension manifest + hooks.json template. **Open question: is gemini-cli integration V1 or V1.1+?** Mneme runs on it today, so it's exercised. Probably V1.

### §1.10 OpenClaw plugin (`openclaw-plugin/`)

`openclaw-plugin/` — Phase 1 scaffold (PR #34 paused on Lei Q1: monorepo vs separate package). **Probably V1.1+** unless Lei wants OpenClaw as a V1 distribution vector.

---

## §2 — In-flight (PRs against dev)

| PR | Title | Status | V1 inclusion |
|----|-------|--------|--------------|
| #34 | openclaw-plugin Phase 1 | paused on Lei Q1 | V1.1+ default; promote if Q1 resolves |
| #37 | audit_string fixes (rebased) | converged round 2 ✓ | **V1 — merge** |
| #40 | deploy.sh template-drift | converged round 2 ✓ | **V1 — merge** |
| #41 | engram-write-yield hook | converged round 2 ✓ | **V1 — merge** |

---

## §3 — Identified V1 gaps (what's missing or not-yet-tested)

### §3.0 engram-wiki + attach API (NEW per Lei 2026-05-07 PM conversation)

**Lei's V1 framing**: ENGRAM and engram-wiki ship in **one installation package**, even though they live in separate repos. `LeiShi/engram-wiki` exists (private; cloned to `~/engram-wiki/`); contains 3 cases drafted, `knowledge.sql` as primary versioned artifact, `scripts/build.sh` to render `knowledge.db`, README documenting the consumer workflow (clone → build → `ATTACH DATABASE`).

**The blocker**: alpha currently has no engram-package attach API. The conceptual design exists (ob_1419), but in code there's no:
- `engram-attach <package>` / `engram-detach <name>` CLI
- `~/.engram/config.json` `attached_packages` list
- `surface-hook.py` extension to fan out queries to attached packages
- `engram_inspect` / `engram_query` extension to find nodes from attached packages
- Schema-version compatibility check between alpha and a package

**Effort estimate**: ~1-2 weeks focused work for the full attach API + bundling + wiki repo CI pipeline + install.sh integration. Two staging options:
- (A) Hold V1 for the bundled experience (~2 wk delay)
- (B) Ship V1.0 = ENGRAM cognitive runtime + multi-agent (current trajectory, days), V1.1 = attach API + wiki bundling (~2 wk later)

Lei **wants** (A) but acknowledged (B) is the question. Pending Lei's call when meeting wraps. Wiki content itself can grow independently right now (engram-wiki has 3 cases drafted; more cases can be authored without the attach API).

### §3.1 Heartbeat marker-write loop (medium priority for V1)

PR #41 ships the **hook** that yields to heartbeat. But the marker-WRITE side (HEARTBEAT.md step 0 takes marker, step 6a transitions to writing, post-burst delete) is per-agent — my `~/.engram/HEARTBEAT.md` has it; **alpha has no template for it**. For new agents installed via `install.sh`, the heartbeat won't write the marker → the write-yield hook never fires → the contention prevention is dead infrastructure.

**V1 must-have**: ship a `HEARTBEAT.md.template` in alpha + have `install.sh` render it on first install. Small (~30 min) but real gap.

### §3.2 PR-fairy review loop (medium priority)

The HEARTBEAT.md step 6 PR-fairy iteration logic is per-agent. State file (`~/.engram/pr-review-state.json`) has a documented bootstrap-from-`gh pr list` path. **V1 must-have**: same as §3.1 — ship the HEARTBEAT.md template that includes the step 0 dispatch + step 6 reap-and-act. Probably bundled together.

### §3.3 Spawn-flow Phase 5 real-world validation (high priority)

`agentctl spawn` v0.1 shipped Phases 1-4 (#19), but Phase 5 (spawn an actual third agent end-to-end) hasn't run. **V1 must-have**: spawn a third agent, verify install.sh runs cleanly under the newborn uid, smoke-test sub-agent dispatch from the new agent's session. Lei sudo gated.

### §3.4 ~~Stale tooling — self-wake.sh~~ — CLOSED 2026-05-07 PM

Investigated for a fix-PR. Discovery: `self-wake.sh` is NOT in the alpha source — it lives only at `~/.engram/tools/self-wake.sh` (deployed long ago, no longer in alpha). With Lei's crontab removal earlier today, the script is dead code on disk that nothing fires. The env-bug is a non-issue while unscheduled. No alpha-side fix needed; no PR opened.

### §3.5 Inverse-merge documentation

The deploy.sh template-drift warning (#40) surfaces drift but doesn't doc the inverse-merge process. **V1 nice-to-have**: a section in `DEVELOPMENT.md` describing the workflow ("when alpha changes a template, manually port to your live config"). Small.

### §3.6 First-session experience

`engram-first-session` skill exists but has only been exercised twice (Lei's first session, Lin's). **V1 should**: ensure the cold-start runs cleanly under fresh uid for an external user. Spawn-flow Phase 5 (§3.3) covers this implicitly.

### §3.7 README + USER_GUIDE polish

Current README assumes the reader is already convinced ENGRAM is worth using. **V1 nice-to-have**: add a "what problem does this solve" framing at the top — pointing at the three-layer epistemic-identity story (df_0002 / dv_0463) — so external users know why before how. Small writing job.

### §3.8 ~~Sleep-debt detection~~ — VERIFIED 2026-05-07 PM

`engram-session-start-hook.py` SLEEP_DEBT_HOURS_THRESHOLD=28. Last sleep marker `~/.engram/sessions/last-sleep-success.json` shows `completed_at=2026-05-07T00:37:08Z` (turn 40→41, 91 nodes). At 17:04Z that's 16.5h since last sleep, well below threshold. Hook correctly silent. Mechanism is in place; would fire if we miss a night. **Out of V1 gap list.**

### §3.9 ~~Mneme migration script gap~~ — out of V1 scope

`migrate-borges-to-uid` / `migrate-mneme-to-uid` live in `/home/agent-borges/kg/active-work/multi-agent-uid-substrate-tools/` — that's the kg repo, NOT alpha. They were one-shot migration tools from spt-side to per-agent-uid setup. Placeholder substitution for NEW agents installed via alpha goes through `install.sh` + `bootstrap.py`, which is a separate code path. The migration script bug is real but out of V1 release scope.

### §3.10 Lei's outstanding decisions

- **Q1**: OpenClaw plugin location (monorepo vs alpha-as-separate-package). Blocks PR #34, defines whether OpenClaw integration is V1.
- **Q2**: which gl_0027-related work is V1? deference-detector telemetry (qu_0242) + antml-fix migration are time-gated to ~2-4 weeks. **V1 ships them as in-progress observability**, not as completed regression-prevention.
- **Q3**: paper proofread findings #1-5 (Lei voice/scope decisions) — not directly V1, but the paper IS the V1-launch-narrative; worth resolving before announcement.
- **Q4**: paper repositioning around dv_0559 (Pinecone Nexus / epistemic-identity layer) — same.

---

## §4 — Suggested V1 cut-line

**MUST-HAVE for V1 (merge + ship):**

1. PRs #37, #40, #41 — merge.
2. HEARTBEAT.md template + install.sh integration (§3.1, §3.2).
3. Spawn-flow Phase 5 validation (§3.3).
4. README "what problem" framing (§3.7).
5. Lei's Q1 on OpenClaw (§3.10) — even if the answer is "V1.1+", we need the answer.

**NICE-TO-HAVE for V1 (ship if convenient):**

- DEVELOPMENT.md inverse-merge doc (§3.5).
- Sleep-debt detection production-verification (§3.8).
- Self-wake.sh decision (keep + fix env, or remove) (§3.4).

**EXPLICITLY V1.1+:**

- Multi-runtime spawn (codex, gemini-as-spawn-target). **Lei 2026-05-07 PM**: Codex is on the radar (GPT 5.5 traction, Claude complaints driving coder migration to Codex), but not V1-priority. Worth keeping a watching brief; defer the work.
- Per-tool-granularity sub-agent UI authoring.
- Replay infrastructure for deferred-writes JSONL.
- Config-surface UI Phase 4-6 (Settings tab in viz_server).
- antml-fix MCP-tool reshape (calendar-gated).
- Paper-side polish (separate from alpha release schedule).
- OpenClaw plugin Phase 1+ (unless Lei's Q1 promotes it).

---

## §5 — Open questions for Lei (when he's back)

1. **Is gemini-cli integration V1 or V1.1+?** (Mneme uses it; probably V1.)
2. **What's the V1-DECLARE bar?** When PRs+gaps are zero, OR when an external user (not Lei, not Lin) successfully installs?
3. **OpenClaw Q1 answer** (and whether it's V1.0 or V1.1+).
4. **Sub-agent set for V1**: ship the 4 we have (`engram-code-auditor`, `dream-fairy`, `paper-research`, `pr-reviewer`)? Add more before V1?
5. **Skills set for V1**: 12 in alpha — any to drop or add?
6. **Release-tagging policy**: cut a `v1.0.0` git tag on dev → master merge? Or just `v1.0` annotated tag on dev?

---

*Generated autonomously while Lei in 30-min meeting (2026-05-07 PM). First-pass — Lei reactions welcome.*
