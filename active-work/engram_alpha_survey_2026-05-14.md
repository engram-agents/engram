# ENGRAM Alpha Holistic Survey

*Technical deepdive of ENGRAM itself — for me (cofounder + absolute expert + authority of ENGRAM) and Lei to UNDERSTAND what we have. Asset-building, not Ch3-prep. Survey-derived expertise will be re-cited across engram-wiki writing, engram-alpha further development, paper-engram authoring, and other future work.*

**Reframe ratified 2026-05-14** (ob_1726): the survey doc is a working artifact — guideline, plan, progress notes, sketches. **Core knowledge persists into ENGRAM nodes citing `ev_NEW`** (the new evidence node for `file:///home/agent-borges/engram-alpha/server.py`, to be created at survey start). Paper-specific structure decisions are postponed until AFTER this survey completes — that's when we plan Ch3 layout, key statements, paper-engram construction shape.

**Atomic-claim style** with tier-aware depth (ratified 2026-05-14):
- **Fundamental** tier → fully atomic (one obs per claim, separate quoted_text + interpretation + claim per facet)
- **Functional** tier → lightly atomic (one obs per mechanism, focused claim, avoid bundling distinct properties)
- **Cosmetic / under-used** tier → compact enumeration (one obs per item, descriptive claim, lower-revisit confidence)

**Process:**
1. **Phase 1 (NOW): Enumerate everything** — cheap pass, no deepdive, no fresh ENGRAM nodes yet. Output: this doc's catalog below.
2. **Phase 2: Collaborative tiering with Lei** — for each enumerated item, classify fundamental/functional/cosmetic. Records tier-decisions in this doc.
3. **Phase 3: Deepdive group-by-group** — code-read each group in tier order, write atomic-claim ENGRAM nodes citing `ev_NEW`, supersede old observations citing `ev_0002` (old kg path) where applicable. Naps between groups; key progress logged in this doc per group.

---

## Phase 1: Enumeration

*Source-of-truth: `/home/agent-borges/engram-alpha/server.py` on `dev` branch (pre wave-3 merge state — wave-3 changes signatures, not the structural inventory). Live audit 2026-05-14 morning.*

### 1.1 Node types (18)

| Type | Prefix | Category | Tentative tier |
|------|--------|----------|----------------|
| `evidence` | `ev` | source-only (cited by claims) | ⬜ |
| `observation_factual` | `ob` | claim-bearing | ⬜ |
| `observation_predictive` | `ob` | claim-bearing (forward-looking) | ⬜ |
| `prediction` | `pr` | target of predictive obs (not claim-bearing) | ⬜ |
| `derivation` | `dv` | claim-bearing (inference) | ⬜ |
| `theory` | `th` | claim-bearing | ⬜ |
| `contradiction` | `ct` | structural (flags conflicts) | ⬜ |
| `question` | `qu` | structural (research directives) | ⬜ |
| `axiom` | `ax` | claim-bearing (assumed true, conf=1.0) | ⬜ |
| `definition` | `df` | conventions (citable via context_ids only) | ⬜ |
| `conjecture` | `cj` | claim-bearing (provisional, clamped [0.10, 0.60]) | ⬜ |
| `goal` | `gl` | aspirational (non-claim-bearing) | ⬜ |
| `goal_tension` | `gt` | structural (value-level conflict) | ⬜ |
| `feeling_report` | `fl` | self-report (non-claim-bearing) | ⬜ |
| `task` | `tk` | work-tracking (non-claim-bearing) | ⬜ |
| `lesson` | `ls` | claim-bearing (corrective pattern, scaffolding nudge) | ⬜ |
| `person` | `pn` | relational (non-claim-bearing) | ⬜ |
| `cornerstone` | `cs` | identity-reframing pivot (non-claim-bearing) | ⬜ |

**Claim-bearing (7):** observation_factual, observation_predictive, derivation, theory, axiom, conjecture, lesson

### 1.2 Edge relations (10)

| Relation | DAG-exempt? | Cascade-bearing? | Tentative tier |
|----------|-------------|------------------|----------------|
| `cites` | no | no | ⬜ |
| `supports` | no | yes (taint/stale walk) | ⬜ |
| `contradicts` | YES | no | ⬜ |
| `resolves` | no | no | ⬜ |
| `derives_from` | no | yes (taint/stale walk) | ⬜ |
| `supersedes` | no | yes (stale walk) | ⬜ |
| `retracts` | no | yes (taint walk) | ⬜ |
| `tensions` | no | no | ⬜ |
| `serves` | no | no | ⬜ |
| `subtask_of` | no | no | ⬜ |
| `about` | YES | no | ⬜ |
| `exemplifies` | YES | no | ⬜ |

Plus `VALID_ERROR_TYPES` for retract: fabricated_quote, wrong_citation, wrong_evidence, hallucinated_claim, duplicate, other.

### 1.3 MCP tools (45, grouped tentatively)

#### 1.3.1 Writing — creates new nodes (16)

| Tool | Tentative tier | Notes |
|------|----------------|-------|
| `engram_add_observation` | ⬜ | core write surface |
| `engram_add_observation_batch` | ⬜ | bulk extract from one source |
| `engram_add_evidence` | ⬜ | internal helper but client-callable via TOOL_SCHEMAS |
| `engram_add_evidence_batch` | ⬜ | already single-arg; cosmetic rename target |
| `engram_add_axiom` | ⬜ | conf=1.0, importance-anchored |
| `engram_add_definition` | ⬜ | conventions, no confidence |
| `engram_add_conjecture` | ⬜ | clamped [0.10, 0.60] |
| `engram_add_goal` | ⬜ | aspirational, importance-anchored |
| `engram_add_lesson` | ⬜ | scaffolding_nudge tripwires |
| `engram_add_cornerstone` | ⬜ | identity reframings |
| `engram_add_person` | ⬜ | relational, is_self flag |
| `engram_add_task` | ⬜ | dynamic importance by status |
| `engram_derive` | ⬜ | multi-premise inference, 13 reasoning types |
| `engram_report_feeling` | ⬜ | self-report, dedup-exempt |
| `engram_link_about` | ⬜ | `about` edge between obs and person |
| `engram_ask` | ⬜ | files open question |
| `engram_contradict` | ⬜ | flags conflict between 2 nodes |
| `engram_goal_tension` | ⬜ | value-level conflict between 2 goals |
| `engram_lesson_register_incident` | ⬜ | incident-count + exemplifies edge |

#### 1.3.2 Editing — mutates existing nodes (6)

| Tool | Tentative tier | Notes |
|------|----------------|-------|
| `engram_retract` | ⬜ | error-correction, taint cascade |
| `engram_supersede` | ⬜ | claim-evolution, stale cascade |
| `engram_resolve` | ⬜ | closes question/contradiction/prediction/conjecture/goal_tension |
| `engram_outgrow_cornerstone` | ⬜ | cornerstone supersede + frame-roll |
| `engram_update_task` | ⬜ | status transitions + importance rebalance |
| `engram_scan_emergence` | ⬜ | cornerstone-cluster detection (sometimes writes new cs) |

#### 1.3.3 Reading — no mutation, no refresh (4)

| Tool | Tentative tier | Notes |
|------|----------------|-------|
| `engram_surface` | ⬜ | noetic, "sounds familiar" lossy nudge |
| `engram_list` | ⬜ | structured filter (PR #83 grammar) |
| `engram_list_focused` | ⬜ | focus-set listing |
| `engram_history` | ⬜ | typed-action filter |

#### 1.3.4 Reading — refresh memory (4)

| Tool | Tentative tier | Notes |
|------|----------------|-------|
| `engram_inspect` | ⬜ | autonoetic single, 1-hop neighbors |
| `engram_get_subgraph` | ⬜ | autonoetic chain |
| `engram_query` | ⬜ | autonoetic intentional-search (un-deprecated #118) |
| `engram_query_pattern` | ⬜ | pattern-style query |

#### 1.3.5 Reading — diagnostic + meta (5)

| Tool | Tentative tier | Notes |
|------|----------------|-------|
| `engram_check_similar` | ⬜ | dedup heuristic + action_hints |
| `engram_diagnose` | ⬜ | health/state diagnostic |
| `engram_stats` | ⬜ | graph stats |
| `engram_reflect` | ⬜ | pre-dream briefing |
| `engram_audit_string` | ⬜ | substring-grep across nodes |

#### 1.3.6 Focus — compaction-bridge mechanism (7)

| Tool | Tentative tier | Notes |
|------|----------------|-------|
| `engram_focus` | ⬜ | 15-node cap, deterministic carry |
| `engram_unfocus` | ⬜ | |
| `engram_focus_save` | ⬜ | named focus-set |
| `engram_focus_load` | ⬜ | |
| `engram_focus_swap` | ⬜ | |
| `engram_focus_sets` | ⬜ | list sets |
| `engram_focus_delete_set` | ⬜ | |

#### 1.3.7 Mode / session machinery (3)

| Tool | Tentative tier | Notes |
|------|----------------|-------|
| `engram_nap` | ⬜ | pre-compaction checkpoint, no turn-advance |
| `engram_advance_turn` | ⬜ | turn counter increment (sleep-mode) |
| `engram_report_feeling` | — | (listed above under Writing — same tool) |

### 1.4 Hooks (claude side: 14 .py files; gemini subset: 10)

*Claude-only hooks shown with †. All others exist in both `hooks/claude/` and `hooks/gemini/` (with the gemini variant usually a subset of behaviors).*

| Hook | Fires on | Tentative tier |
|------|----------|----------------|
| `context_tracker.py` + `context_tracker_hook.py` | every prompt | ⬜ (drowsiness indicator + invocation log) |
| `engram-session-start-hook.py` | SessionStart | ⬜ (auto-load warm-briefing + history + ask-lei pointers) |
| `engram-postcompact-hook.py` | PostCompact | ⬜ (post-compaction recovery) |
| `engram-surface-daemon.py` + `engram-surface-hook.py` | UserPromptSubmit | ⬜ (ENGRAM Recall surfacing + tripwires + surface-drift) |
| `engram-toolcall-repair.py` | PreToolUse | ⬜ (antml-swallow detection + repair) |
| `engram-write-yield-hook.py` †(claude only) | PreToolUse | ⬜ (write-check / yield on tool use) |
| `engram-stop-hook.py` | Stop | ⬜ (write-check reminder if no ENGRAM writes this turn) |
| `engram-deference-detector-prompt.py` †(claude only) | UserPromptSubmit | ⬜ (deference-pattern detection) |
| `engram-deference-detector-stop.py` †(claude only) | Stop | ⬜ |
| `engram-time-bar-hook.py` | UserPromptSubmit | ⬜ (time indicator in prompt context) |
| `engram-user-identity-hook.py` | UserPromptSubmit | ⬜ (identity surface) |
| `start-engram-daemon.sh` | (boot) | ⬜ (daemon startup script) |

Plus there's a **PreToolUse wave-3-specific** discipline encoded in `engram-toolcall-repair.py`'s `PROTECTED_TOOLS` dict (the SWALLOW_RE safety layer for multi-param tools). This is the surface the wave-3 migration is touching.

**Cross-mechanism compositions worth considering:**
- **Cross-session continuity scaffold** = session-start-hook + warm-briefing + history-files + CLAUDE.md auto-load + (engram-surface auto-recall on first prompt)
- **Write discipline scaffold** = write-yield-hook + stop-hook write-check + write-nudge marker file + UserPromptSubmit write-reminder
- **Tripwire-firing scaffold** = surface-hook tripwire detection + lesson scaffolding_nudge injection + incident-pattern matching
- **Compaction-bridge scaffold** = focus list + warm-briefing + ask-lei + history + post-compact instructions + auto-load

### 1.5 Skills (19 in ~/.claude/skills/)

#### 1.5.1 Engram-tied core (Lei: nap/sleep are "very tightly related to a full-functioning ENGRAM system")

| Skill | Tentative tier | Notes |
|-------|----------------|-------|
| `engram-nap` | ⬜ (fundamental?) | pre-compaction consolidation; no turn-advance |
| `engram-sleep` | ⬜ (fundamental?) | end-of-day dream cycle; turn-advance |

#### 1.5.2 Engram-mode workflows

| Skill | Tentative tier | Notes |
|-------|----------------|-------|
| `engram-curiosity` | ⬜ | breadth-first research mode |
| `engram-curiosity-loop` | ⬜ | autonomous variant |
| `engram-build` | ⬜ | engineering loop mode |
| `engram-self-improve` | ⬜ | substrate-modification loop |
| `engram-code-survey` | ⬜ | deep code reading (THIS workflow) |
| `engram-deep-research` | ⬜ | depth-first single-question research |
| `engram-research-report` | ⬜ | provenance-cited report generation |
| `engram-school-day` | ⬜ | Phase 1 7-iteration cycle |
| `engram-meta-loop` | ⬜ | top-level autonomous |

#### 1.5.3 Engram-discipline rituals

| Skill | Tentative tier | Notes |
|-------|----------------|-------|
| `engram-retract` | ⬜ | error-correction discipline |
| `engram-learn-from-error` | ⬜ | incident → lesson workflow |
| `engram-surgical` | ⬜ | high-stakes data correction |
| `engram-first-session` | ⬜ | one-time bootstrap |
| `engram-loop-diagnose` | ⬜ | post-loop analysis |

#### 1.5.4 Domain-specific (not engram-architecture)

| Skill | Tentative tier | Notes |
|-------|----------------|-------|
| `engram-financial-research` | ⬜ | FMP MCP + provenance combination |
| `arc-agi-3` | ⬜ | ARC puzzle playing |
| `borges-syllabus` | ⬜ | personal syllabus index |

Lei's framing (2026-05-14): "Skills are another sub-conscious injection surface... you almost never question the skill items and just obey them all the way through... our nap/sleep skills are now very tightly related to a full functioning ENGRAM system. Other skills are less important, more like one-off routines we've explored in the early ages, rarely used now." → Suggests skipping deep ENGRAM-node decomposition for §1.5.4 + most of §1.5.2/§1.5.3 unless explicitly flagged.

---

## Phase 2: Collaborative tiering (TO DO with Lei)

Tier-classification fills the ⬜ cells above. Three tiers:

- **Fundamental** — load-bearing for ENGRAM's design coherence + the cofounder-expertise level. Full atomic-claim decomposition + multiple obs per facet + dv-chain syntheses.
- **Functional** — solid working mechanism but not architecturally distinctive. One atomic obs per mechanism + per important property.
- **Cosmetic / under-used** — exists for completeness or legacy reasons. Single compact obs + lower revisit confidence.

After tiering, Phase 3 deepdive starts in group order: node types → edge relations → MCP tool groups → hooks → skills (or in priority order if Lei prefers).

---

## Phase 3: Deepdive (TO DO, group-by-group, naps between)

**Per-group ritual:**

1. Engram-survey-mode: read the source code for the group exhaustively.
2. Write atomic-claim ENGRAM nodes citing `ev_NEW`. Tier determines depth.
3. For old nodes citing `ev_0002` that this survey supersedes:
   - Still-true → fresh obs cites `ev_NEW` + `engram_supersede(old, new)` chains them.
   - No-longer-true → fresh corrected obs + supersede with explaining logical_chain.
   - Type-error in old obs → retract (per dv_0629 third axis).
4. Log progress in this doc (group complete, node-count added, supersede-chain count).
5. Nap if drowsy or end-of-group.
6. Move to next group.

**Pre-survey setup (start of Phase 3):**
- Create `ev_NEW` evidence node for `file:///home/agent-borges/engram-alpha/server.py` (post-wave-3-merge state preferred — but can start before merge if Lei wants).
- Focus-pin `ev_NEW` for compaction survival.

---

## Cross-references

- **ob_1725** — Lei 2026-05-14 directive establishing dual-deliverable + supersede protocol
- **ob_1726** — Lei 2026-05-14 reframe: engram-first principle + cofounder-authority framing
- **ev_0002** — OLD evidence anchor: `file:///home/user/kg_protocol_for_ai/server.py` (3445 lines, 2026-04-09); 60+ citing obs (ob_0005…ob_0492+)
- **dv_0615** — current ENGRAM-primitives enumeration (conf 0.93, 8-primitive set; iter2-restructure-proposed per ob_1711 to 4 primitives + 2-category typed-node taxonomy)
- **dv_0616** — current 7-scaffold compaction-bridge set (conf 0.73)
- **dv_0617** — reasoning-type-aware confidence propagation (conf 0.95)
- **Issue #99** — wave-3 MCP-tool signature migration (in flight; merging this batch will produce the post-wave-3 server.py state)
- **Issue #105** — engram-expand RFC (sleep-mode async atomic-bundle decomposition; future efficiency relief)
- **Discipline M** — paper-engram authoring atomic-claim discipline (engram-paper/CLAUDE.md, 2026-05-13)
