# 00 — Complete File Inventory

*Generated 2026-06-05 from dev tip; every file in the repo, its ship status per `src/build/packaging/tiers.json`, and its mechanism assignment (filled during the sweep). Regenerate mechanically (generation script: see PR #856 thread; tools/ home pending); never hand-edit counts. The handbook's own files are not indexed.*


**Census** (2026-06-05 snapshot; Gemini CLI lane files removed by #865 2026-06-06): 385 files · 121 shipped (tier-mapped) · 9 manifest-excluded (tracked, deliberately not shipped) · 204 dev-only · 13 repo-only (outside build-plugin.sh universe, repo-clone-only by design) · 33 forum-deploy (manifest-excluded, ships via forum deploy path, issue #868) · 4 zombie-suspect (remaining after #865 removed 30 gemini-lane zombie-suspects) · 1 unmapped ⚠ (in-universe gap, needs adjudication)


**Status legend** (per-mechanism, assigned during sweep): `prod-verified` (behavior checked against ground truth, dated) · `prod-presumed` (ships, believed live, unverified this audit) · `dormant` (ships, inert by design) · `zombie-suspect` (ships, evidence of silent breakage — the #824 class) · `regressed` · `parked` · `retired` · `dev-only`


## (root) (29 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `.gitignore` | dev-only | — | _?_ | _unswept_ |
| `CHANGELOG.md` | repo-only | — | _?_ | _unswept_ |
| `CLAUDE.md` | repo-only | — | _?_ | _unswept_ |
| `DEVELOPMENT.md` | repo-only | — | _?_ | _unswept_ |
| `LICENSE` | repo-only | — | _?_ | _unswept_ |
| `README.md` | repo-only | — | _?_ | _unswept_ |
| `RELEASING.md` | repo-only | — | _?_ | _unswept_ |
| `SKILL.md` | shipped | essential | _?_ | _unswept_ |
| `USER_GUIDE.md` | repo-only | — | _?_ | _unswept_ |
| `bootstrap.py` | shipped | essential | _?_ | _unswept_ |
| `engram_backup.py` | shipped | essential | _?_ | _unswept_ |
| `engram_client.py` | shipped | essential | _?_ | _unswept_ |
| `engram_confidence.py` | shipped | essential | _?_ | _unswept_ |
| `engram_filter.py` | shipped | essential | _?_ | _unswept_ |
| `engram_idf.py` | shipped | essential | _?_ | _unswept_ |
| `engram_ids.py` | shipped | essential | _?_ | _unswept_ |
| `engram_log_emitter.py` | shipped | essential | _?_ | _unswept_ |
| `engram_log_indexer.py` | shipped | essential | _?_ | _unswept_ |
| `engram_walguard.py` | shipped | essential | _?_ | _unswept_ |
| `launch-engram-server.sh` | shipped | essential | _?_ | _unswept_ |
| `plugin.json` | repo-only | — | _?_ | _unswept_ |
| `pytest.ini` | repo-only | — | _?_ | _unswept_ |
| `requirements-dev.txt` | repo-only | — | _?_ | _unswept_ |
| `requirements-lock.txt` | repo-only | — | _?_ | _unswept_ |
| `requirements.txt` | repo-only | — | _?_ | _unswept_ |
| `selftest.py` | ZOMBIE-SUSPECT ⚠ | — | _?_ | _unswept_ |
| `server.py` | shipped | essential | _?_ | _unswept_ |
| `viz_server.py` | shipped | convenience | _?_ | _unswept_ |

## .claude (5 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `.claude/agents/engram-code-auditor.md` | dev-only | — | _?_ | _unswept_ |
| `.claude/agents/engram-coder-fairy.md` | dev-only | — | _?_ | _unswept_ |
| `.claude/agents/engram-dream-fairy.md` | dev-only | — | _?_ | _unswept_ |
| `.claude/agents/engram-paper-research.md` | dev-only | — | _?_ | _unswept_ |
| `.claude/agents/engram-pr-reviewer.md` | dev-only | — | _?_ | _unswept_ |

## .github (2 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `.github/workflows/check-no-new-shipped-node-ids.yml` | dev-only | — | _?_ | _unswept_ |
| `.github/workflows/tests.yml` | dev-only | — | _?_ | _unswept_ |

## agents/claude (7 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `agents/claude/README.md` | repo-only | — | _?_ | _unswept_ |
| `agents/claude/engram-batch-summary-fairy.md` | shipped | essential | _?_ | _unswept_ |
| `agents/claude/engram-coder-fairy.md` | shipped | convenience | _?_ | _unswept_ |
| `agents/claude/engram-dream-fairy.md` | shipped | essential | _?_ | _unswept_ |
| `agents/claude/engram-dream-master.md` | shipped | essential | _?_ | _unswept_ |
| `agents/claude/engram-pr-reviewer.md` | shipped | convenience | _?_ | _unswept_ |
| `agents/claude/engram-summary-fairy.md` | shipped | essential | _?_ | _unswept_ |

## docs (1 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `docs/baton-protocol.md` | dev-only | — | _?_ | _unswept_ |

## forum (36 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `forum/FORUM.md` | forum-deploy | — | _?_ | _unswept_ |
| `forum/README.md` | forum-deploy | — | _?_ | _unswept_ |
| `forum/__init__.py` | forum-deploy | — | _?_ | _unswept_ |
| `forum/__main__.py` | forum-deploy | — | _?_ | _unswept_ |
| `forum/admin.py` | forum-deploy | — | _?_ | _unswept_ |
| `forum/audit.py` | forum-deploy | — | _?_ | _unswept_ |
| `forum/avatar.py` | forum-deploy | — | _?_ | _unswept_ |
| `forum/db.py` | forum-deploy | — | _?_ | _unswept_ |
| `forum/deploy/README.md` | forum-deploy | — | _?_ | _unswept_ |
| `forum/deploy/engram-forum-backup.service` | shipped | convenience | _?_ | _unswept_ |
| `forum/deploy/engram-forum-backup.timer` | shipped | convenience | _?_ | _unswept_ |
| `forum/deploy/engram-forum.service.template` | forum-deploy | — | _?_ | _unswept_ |
| `forum/deploy/install-forum-service.sh` | forum-deploy | — | _?_ | _unswept_ |
| `forum/embeddings.py` | forum-deploy | — | _?_ | _unswept_ |
| `forum/fairy-spec-backend.md` | ZOMBIE-SUSPECT ⚠ | — | _?_ | _unswept_ |
| `forum/fairy-spec-configurable-categories-slice2.md` | ZOMBIE-SUSPECT ⚠ | — | _?_ | _unswept_ |
| `forum/fairy-spec-frontend.md` | ZOMBIE-SUSPECT ⚠ | — | _?_ | _unswept_ |
| `forum/packs.py` | forum-deploy | — | _?_ | _unswept_ |
| `forum/render.py` | forum-deploy | — | _?_ | _unswept_ |
| `forum/requirements.txt` | forum-deploy | — | _?_ | _unswept_ |
| `forum/seed.py` | forum-deploy | — | _?_ | _unswept_ |
| `forum/seeds/categories.default.json` | forum-deploy | — | _?_ | _unswept_ |
| `forum/seeds/retraction-0-op.md` | forum-deploy | — | _?_ | _unswept_ |
| `forum/seeds/retraction-1-borges.md` | forum-deploy | — | _?_ | _unswept_ |
| `forum/seeds/retraction-2-ariadne.md` | forum-deploy | — | _?_ | _unswept_ |
| `forum/seeds/welcome-0-op.md` | forum-deploy | — | _?_ | _unswept_ |
| `forum/seeds/welcome-1-borges.md` | forum-deploy | — | _?_ | _unswept_ |
| `forum/seeds/welcome-2-ariadne.md` | forum-deploy | — | _?_ | _unswept_ |
| `forum/server.py` | forum-deploy | — | _?_ | _unswept_ |
| `forum/spec.md` | forum-deploy | — | _?_ | _unswept_ |
| `forum/static/.gitkeep` | forum-deploy | — | _?_ | _unswept_ |
| `forum/templates/forum.html` | forum-deploy | — | _?_ | _unswept_ |
| `forum/templates/pack_detail.html` | forum-deploy | — | _?_ | _unswept_ |
| `forum/templates/packs.html` | forum-deploy | — | _?_ | _unswept_ |
| `forum/templates/search.html` | forum-deploy | — | _?_ | _unswept_ |
| `forum/templates/thread.html` | forum-deploy | — | _?_ | _unswept_ |

## forum/tests (21 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `forum/tests/__init__.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_admin_categories.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_audit.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_avatar.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_configurable_categories.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_db.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_discovery.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_embeddings.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_endpoints.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_forum_template.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_index_filters.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_online.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_packs.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_packs_browse.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_qa.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_read_view.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_readstate.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_render.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_search.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_search_hybrid.py` | dev-only | — | _?_ | _unswept_ |
| `forum/tests/test_seed.py` | dev-only | — | _?_ | _unswept_ |

## hooks (1 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `hooks/hooks.json` | UNMAPPED ⚠ | — | _?_ | _unswept_ |

## hooks/claude (19 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `hooks/claude/context_tracker.py` | shipped | essential | _?_ | _unswept_ |
| `hooks/claude/context_tracker_hook.py` | excluded | — (superseded by hooks/claude/context_tracker.py (Iss) | _?_ | _unswept_ |
| `hooks/claude/engram-baton-prompt-hook.py` | shipped | convenience+MA | _?_ | _unswept_ |
| `hooks/claude/engram-deference-detector-prompt.py` | shipped | convenience | _?_ | _unswept_ |
| `hooks/claude/engram-deference-detector-stop.py` | shipped | convenience | _?_ | _unswept_ |
| `hooks/claude/engram-end-of-day-hook.py` | shipped | convenience | _?_ | _unswept_ |
| `hooks/claude/engram-forum-prompt-hook.py` | shipped | convenience | _?_ | _unswept_ |
| `hooks/claude/engram-github-notifications-hook.py` | shipped | convenience | _?_ | _unswept_ |
| `hooks/claude/engram-inter-agent-prompt-hook.py` | shipped | convenience+MA | _?_ | _unswept_ |
| `hooks/claude/engram-postcompact-hook.py` | shipped | essential | _?_ | _unswept_ |
| `hooks/claude/engram-session-start-hook.py` | shipped | essential | _?_ | _unswept_ |
| `hooks/claude/engram-stop-hook.py` | shipped | essential | _?_ | _unswept_ |
| `hooks/claude/engram-surface-daemon.py` | shipped | essential | _?_ | _unswept_ |
| `hooks/claude/engram-surface-hook.py` | shipped | essential | _?_ | _unswept_ |
| `hooks/claude/engram-time-bar-hook.py` | shipped | convenience | _?_ | _unswept_ |
| `hooks/claude/engram-toolcall-repair.py` | shipped | essential | _?_ | _unswept_ |
| `hooks/claude/engram-user-identity-hook.py` | shipped | essential | _?_ | _unswept_ |
| `hooks/claude/engram-utility-credit-mention-stop.py` | shipped | convenience | _?_ | _unswept_ |
| `hooks/claude/start-engram-daemon.sh` | shipped | essential | _?_ | _unswept_ |

*(Gemini CLI lane — hooks/gemini/ 13 files, integrations/gemini-cli/ 4 files, skills/gemini/ 16 files, install-gemini.sh — RETIRED by #865 (2026-06-06; archived at tag `archive/gemini`). All rows removed from census.)*

## inter-agent (3 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `inter-agent/README.md` | dev-only | — | _?_ | _unswept_ |
| `inter-agent/hot_seat_scratchpad_design.md` | dev-only | — | _?_ | _unswept_ |
| `inter-agent/session_context_design.md` | dev-only | — | _?_ | _unswept_ |

## output-styles (1 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `output-styles/claude/proactive-with-carveouts.md` | shipped | convenience | _?_ | _unswept_ |

## src/build/packaging (5 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `src/build/packaging/README.md` | dev-only | — | _?_ | _unswept_ |
| `src/build/packaging/mcp.json` | dev-only | — | _?_ | _unswept_ |
| `src/build/packaging/platforms/claude-code.json` | dev-only | — | _?_ | _unswept_ |
| `src/build/packaging/platforms/codex.json` | dev-only | — | _?_ | _unswept_ |
| `src/build/packaging/tiers.json` | dev-only | — | _?_ | _unswept_ |

## skills/claude (25 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `skills/claude/engram-auto-coder-fairy-judgement/SKILL.md` | shipped | convenience | _?_ | _unswept_ |
| `skills/claude/engram-auto-reviewer-fairy-judgement/SKILL.md` | shipped | convenience | _?_ | _unswept_ |
| `skills/claude/engram-baton/SKILL.md` | shipped | convenience+MA | _?_ | _unswept_ |
| `skills/claude/engram-collaborating-loop/SKILL.md` | shipped | convenience+MA | _?_ | _unswept_ |
| `skills/claude/engram-contradiction-resolution/SKILL.md` | shipped | essential | _?_ | _unswept_ |
| `skills/claude/engram-curiosity-loop/SKILL.md` | shipped | convenience | _?_ | _unswept_ |
| `skills/claude/engram-deep-research/SKILL.md` | shipped | convenience | _?_ | _unswept_ |
| `skills/claude/engram-fairy-orchestration/SKILL.md` | shipped | convenience | _?_ | _unswept_ |
| `skills/claude/engram-first-session/SKILL.md` | shipped | essential | _?_ | _unswept_ |
| `skills/claude/engram-forum/SKILL.md` | shipped | convenience | _?_ | _unswept_ |
| `skills/claude/engram-learn-from-error/SKILL.md` | shipped | essential | _?_ | _unswept_ |
| `skills/claude/engram-letter/SKILL.md` | shipped | convenience+MA | _?_ | _unswept_ |
| `skills/claude/engram-loop-diagnose/SKILL.md` | shipped | convenience | _?_ | _unswept_ |
| `skills/claude/engram-loop/SKILL.md` | shipped | convenience | _?_ | _unswept_ |
| `skills/claude/engram-meta-loop/SKILL.md` | shipped | convenience | _?_ | _unswept_ |
| `skills/claude/engram-nap/SKILL.md` | shipped | essential | _?_ | _unswept_ |
| `skills/claude/engram-research-report/SKILL.md` | shipped | convenience | _?_ | _unswept_ |
| `skills/claude/engram-resolve-cascade/SKILL.md` | shipped | essential | _?_ | _unswept_ |
| `skills/claude/engram-retract/SKILL.md` | shipped | essential | _?_ | _unswept_ |
| `skills/claude/engram-school-day/SKILL.md` | shipped | convenience | _?_ | _unswept_ |
| `skills/claude/engram-self-improve/SKILL.md` | shipped | dev | _?_ | _unswept_ |
| `skills/claude/engram-sleep/SKILL.md` | shipped | essential | _?_ | _unswept_ |
| `skills/claude/engram-trust-tier/SKILL.md` | shipped | essential | _?_ | _unswept_ |
| `skills/claude/engram-upgrade/SKILL.md` | shipped | convenience | _?_ | _unswept_ |
| `skills/claude/internal-external-decision/SKILL.md` | shipped | essential | _?_ | _unswept_ |

## templates (10 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `templates/template.AGENTS.md` | shipped | essential | _?_ | _unswept_ |
| `templates/template.CLAUDE.multi-agent.md` | shipped | convenience+MA | _?_ | _unswept_ |
| `templates/template.CLAUDE.md` | shipped | essential | _?_ | _unswept_ |
| `templates/agents.json.example` | shipped | convenience+MA | _?_ | _unswept_ |
| `templates/compact-instructions.md` | shipped | essential | _?_ | _unswept_ |
| `templates/config.json.example` | shipped | essential | _?_ | _unswept_ |
| `templates/engram-surface-daemon.service.template` | shipped | convenience | _?_ | _unswept_ |
| `templates/engram-viz-user.service.template` | shipped | convenience | _?_ | _unswept_ |
| `templates/engram-viz.service` | shipped | convenience | _?_ | _unswept_ |
| `templates/template.warm-briefing.md` | shipped | essential | _?_ | _unswept_ |

## tests (165 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `tests/conftest.py` | dev-only | — | _?_ | _unswept_ |
| `tests/spawn/Dockerfile` | dev-only | — | _?_ | _unswept_ |
| `tests/spawn/README.md` | dev-only | — | _?_ | _unswept_ |
| `tests/spawn/claude-stub.sh` | dev-only | — | _?_ | _unswept_ |
| `tests/spawn/run_spawn_test.sh` | dev-only | — | _?_ | _unswept_ |
| `tests/test_action_hints.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_add_axiom_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_add_conjecture_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_add_cornerstone_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_add_definition_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_add_evidence_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_add_goal_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_add_lesson_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_add_observation_batch_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_add_observation_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_add_person_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_add_task_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_advance_turn_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_ask_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_auto_surface_prepending.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_axiom_definition_conjecture.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_baton.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_baton_autopull.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_baton_hook.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_bootstrap_codex.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_bootstrap_fresh_db.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_build_plugin_hooks_consistency.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_cascade_semantics.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_closure_checker.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_codex_hook_envelopes.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_cohort_dispatch.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_config_schema.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_contradict_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_contradiction_cascade.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_count_live_exemplars.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_deference_detector_scope_gate.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_derive_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_diagnose_calibration.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_diagnose_read_tool_contention.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_dream_fairy_patterns.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_dream_master_batch_flow.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_drowsiness_display_levels.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_edge_classifications_ssot.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_end_of_day_hook.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_engine_flows.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_engine_phase3.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_engine_steps.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_engine_unit.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_engram_add_edge.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_engram_backup.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_engram_client_schemas.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_engram_filter.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_engram_idf.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_engram_ids.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_engram_stats_mode.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_engram_surface_hook_critical_warning.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_engram_surface_hook_idf_gate.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_engram_utility_credit_mention_stop.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_env_home_precedence.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_fairy_policy.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_feeling.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_finalize_name_rewrite.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_focus_delete_set_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_focus_load_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_focus_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_focus_save_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_focus_sets.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_focus_swap_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_forum_hook.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_fts_retract_trigger.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_git_backup_embeddings.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_goal.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_goal_tension.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_goal_tension_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_history_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_hooks_json_stdout_audit.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_ia_cli.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_inductive_enumeration_and_timing.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_inspect_include_superseded.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_inspect_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_inspect_view_modes.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_inter_agent_hook.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_lesson_register_incident_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_link_about_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_list_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_list_recall_summary.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_log_emitter.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_log_indexer.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_log_integration.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_logging_emit.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_migrate_db_trust_tier.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_migrate_trust_tier_self_backfill.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_mmr_rerank.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_mode_gate_helpers.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_multiplicative_composite.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_nap_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_outgrow_cornerstone_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_per_session_marker.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_per_session_marker_prune.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_plugin_manifest_import_sync.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_polarity_nli.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_prewarm_embeddings.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_query_include_superseded.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_query_pattern.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_query_pattern_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_query_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_query_view_modes.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_reasoning_types.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_recall_summary_substrate.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_recall_summary_writes.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_reflect_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_reflect_view_modes.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_register_exemplar.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_remove_edge_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_report_feeling_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_resolve_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_resolve_runtime_dir_plugin.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_restore_doa_781.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_retract_and_quotes.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_retract_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_sanitize_fts_query.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_scan_emergence_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_scope_export.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_search_nodes_bm25_relevance.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_search_nodes_debug.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_sequences_id.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_session_start_hook_auto_sleep_cron.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_session_start_hook_calibration.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_session_start_hook_mcp_health.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_set_trust_tier_v2.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_similar_existing_helper.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_skill_frontmatter_lint.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_skill_server_sync.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_source_class.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_special_type_bypass.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_stale_replacement_dict.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_starred_block.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_stop_hook_idle_suppression.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_strip_agent_facing_fields.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_subgraph_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_subgraph_view_modes.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_supersede_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_supersede_relational.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_supported_by.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_surface_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_thresholds.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_tier_build_filter.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_tier_renderer_confidence.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_tier_sizes.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_tiers_manifest.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_time_bar_hook.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_trust_tier.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_unfocus_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_update_task_payload.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_url_validation.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_utility_use_action.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_utility_use_sites_expansion.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_viz_server_calibration.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_viz_server_config_ui.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_viz_server_config_write.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_viz_server_search.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_viz_server_select_control.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_viz_server_stats.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_walguard_786.py` | dev-only | — | _?_ | _unswept_ |
| `tests/test_wave_2_bool_coercion_sweep.py` | dev-only | — | _?_ | _unswept_ |

## tools (41 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `tools/__init__.py` | shipped | essential | _?_ | _unswept_ |
| `tools/_channel.py` | shipped | convenience+MA | _?_ | _unswept_ |
| `tools/_common.py` | shipped | convenience | _?_ | _unswept_ |
| `tools/_session_context.py` | shipped | convenience | _?_ | _unswept_ |
| `tools/_telegram_api.py` | excluded | — (Telegram-specific transport helper; persona-specif) | _?_ | _unswept_ |
| `tools/agent-bootstrap` | excluded | — (operator-level host administration script for mult) | _?_ | _unswept_ |
| `tools/agentctl` | excluded | — (Lei's day-to-day admin CLI for co-located AI agent) | _?_ | _unswept_ |
| `tools/antml_repair_baseline.py` | shipped | dev | _?_ | _unswept_ |
| `tools/baton.py` | shipped | convenience+MA | _?_ | _unswept_ |
| `tools/borges_session_eval.py` | excluded | — (Borges-persona-specific session quality evaluator;) | _?_ | _unswept_ |
| `tools/build-plugin.sh` | shipped | convenience | _?_ | _unswept_ |
| `tools/cohort_dispatch.py` | shipped | essential | _?_ | _unswept_ |
| `tools/compute_task_time.py` | shipped | convenience | _?_ | _unswept_ |
| `tools/config_schema.py` | shipped | convenience | _?_ | _unswept_ |
| `tools/deference_baseline.py` | shipped | dev | _?_ | _unswept_ |
| `tools/dream_master_batch.py` | shipped | essential | _?_ | _unswept_ |
| `tools/engram-fix-git-backup.sh` | shipped | convenience | _?_ | _unswept_ |
| `tools/engram-regenerate-embeddings.py` | shipped | essential | _?_ | _unswept_ |
| `tools/forum.py` | shipped | convenience | _?_ | _unswept_ |
| `tools/forum_backfill_embeddings.py` | forum-deploy | — | _?_ | _unswept_ |
| `tools/forum_backup.py` | shipped | convenience | _?_ | _unswept_ |
| `tools/forum_regen_derived.py` | forum-deploy | — | _?_ | _unswept_ |
| `tools/ia.py` | shipped | convenience+MA | _?_ | _unswept_ |
| `tools/inspect_raw.py` | shipped | essential | _?_ | _unswept_ |
| `tools/install-local-marketplace.sh` | shipped | convenience | _?_ | _unswept_ |
| `tools/migrate-backup.sh` | shipped | convenience | _?_ | _unswept_ |
| `tools/migrate-to-plugin.sh` | shipped | convenience | _?_ | _unswept_ |
| `tools/migrate_db_trust_tier.py` | shipped | dev | _?_ | _unswept_ |
| `tools/operator-setup-viz.sh` | shipped | convenience | _?_ | _unswept_ |
| `tools/recall_summary_payload.py` | shipped | essential | _?_ | _unswept_ |
| `tools/recall_summary_prompts.py` | shipped | essential | _?_ | _unswept_ |
| `tools/recall_summary_validator.py` | shipped | essential | _?_ | _unswept_ |
| `tools/scan-leaks.py` | shipped | dev | _?_ | _unswept_ |
| `tools/session.py` | shipped | convenience | _?_ | _unswept_ |
| `tools/surgical.py` | shipped | dev | _?_ | _unswept_ |
| `tools/telegram_bot.py` | excluded | — (Telegram bot CLI; persona-specific integration (Bo) | _?_ | _unswept_ |
| `tools/test_forum_backup.py` | excluded | — (test file for tools/forum_backup.py — CI tooling o) | _?_ | _unswept_ |
| `tools/test_forum_cli.py` | excluded | — (test file for tools/forum.py — CI tooling only, ne) | _?_ | _unswept_ |
| `tools/test_migrate_to_plugin.sh` | excluded | — (test file for tools/migrate-to-plugin.sh — CI tool) | _?_ | _unswept_ |
| `tools/verify_quote.py` | shipped | essential | _?_ | _unswept_ |
| `tools/write_sleep_marker.py` | shipped | essential | _?_ | _unswept_ |

## tools/engine (6 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `tools/engine/__init__.py` | shipped | convenience | _?_ | _unswept_ |
| `tools/engine/build.py` | shipped | convenience | _?_ | _unswept_ |
| `tools/engine/cli.py` | shipped | convenience | _?_ | _unswept_ |
| `tools/engine/flows.py` | shipped | convenience | _?_ | _unswept_ |
| `tools/engine/manifest.py` | shipped | convenience | _?_ | _unswept_ |
| `tools/engine/steps.py` | shipped | convenience | _?_ | _unswept_ |

## tools/engram-pkg (2 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `tools/engram-pkg/README.md` | shipped | convenience | _?_ | _unswept_ |
| `tools/engram-pkg/engram-pkg` | shipped | convenience | _?_ | _unswept_ |

## tools/migration (6 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `tools/migration/__init__.py` | shipped | convenience | _?_ | _unswept_ |
| `tools/migration/migrate_cascade_semantics_v1.py` | shipped | convenience | _?_ | _unswept_ |
| `tools/migration/migrate_config_v2.py` | shipped | convenience | _?_ | _unswept_ |
| `tools/migration/migrate_config_v3.py` | shipped | convenience | _?_ | _unswept_ |
| `tools/migration/migrate_supports_to_supported_by.py` | shipped | convenience | _?_ | _unswept_ |
| `tools/migration/migrate_trust_tier_self_backfill.py` | shipped | convenience | _?_ | _unswept_ |

## upgrade-guides (1 files)

| file | ship | tier/reason | mechanism | status |
|---|---|---|---|---|
| `upgrade-guides/v1-trust-tier.md` | dev-only | — | _?_ | _unswept_ |
