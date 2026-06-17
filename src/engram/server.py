"""
Protocol-Governed Knowledge Graph Memory Server
================================================
An MCP server that provides structured knowledge graph operations
for LLM agents, implementing epistemic hierarchy principles:
Evidence → Observation → Derivation, with full provenance tracking,
confidence propagation, and version control via git checkpoints.

MVP tools: ingestion (add_evidence, add_observation),
query (query, get_subgraph), version control (checkpoint, diff).
"""

import functools
import hashlib
import json
import logging
import math
import os
import re
import socket
import sqlite3
import subprocess
import textwrap
import time
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse

from fastmcp import FastMCP

# engram_log_emitter (optional at import time — emit_if_initialized is a
# silent no-op when the singleton has not been initialized, which is the
# normal state for server.py: it runs as a long-lived MCP process serving
# multiple Claude sessions and has no single session_id to bind to.
# Phase 4 will add a separate init path for server-side events.
# See alpha #175 (two-level logging architecture) for the spec.
from engram_log_emitter import emit_if_initialized  # noqa: E402

# sqlite-vec (optional): SIMD-accelerated KNN over embeddings, replaces the
# O(N) pure-Python cosine loop in _semantic_search. If the extension fails to
# load (older Python build without enable_load_extension, or module absent),
# we fall back to the legacy Python cosine path — search still works, just
# slowly. Availability is checked once at module import and again per-connection.

# Flipped to False at runtime if extension loading fails on the actual
# connection (some Python sqlite3 builds compile without load_extension).


# ---------------------------------------------------------------------------
# Embedding manager (optional — works without sentence-transformers installed)
# ---------------------------------------------------------------------------



# Global embedding manager instance


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


# Feeling-nudge marker file: written by checkpoint(nap), reflect, and the
# postcompact hook; read-and-cleared by engram_report_feeling. See spec §6
# "Marker file protocol" — drives mechanical nudge_source tagging.

# ── Utility scoring (MemRL-inspired, the MemRL-inspired conjecture) ──
# USE model: bump utility directly on each USE action, α determined by the
# action's intensity-of-impression (Lei 2026-05-19 design call).

# Sandbox guard: set to True when running inside engram_sandbox() context.
# When True, _ensure_data_dir() will refuse to create ~/.engram.

# ---------------------------------------------------------------------------
# WAL/shm self-guard state (#786)
# ---------------------------------------------------------------------------

# Timestamp of last walguard check (epoch float); 0.0 = never checked.
# Throttle interval: run the inode check at most once every 30 seconds.
# Set to True after the first _get_db() call (startup marker-clearing path runs once).
# Set to True after the first skipped-guard log line is emitted (once-per-process).



# Utility Q-update learning rate per USE action. Three tiers per Lei
# 2026-05-19 design call. Numbers preserve the existing α scale.
# Tier 1 (0.15): deep cognitive load — derive / supersede / contradict /
#   resolve / lesson_incident. The agent had to think with the node, not
#   just read it.
# Tier 2 (0.10): moderate engagement — deliberate probes, targeted writes,
#   commitments. The agent acted on the node with intent.
# Tier 3 (0.05): loose touch — focus_load brings IDs into view as a
#   batch; unfocus is a removal action. The agent didn't engage deeply
#   but the substrate counts the contact.

# Multiplicative-amplifier composite (Lei 2026-05-19 design call).
# Replaces the additive (1-λ)×rel + λ×util blend with multiplicative
# composition: relevance is the base, utility and importance are
# multiplicative amplifiers. Calibrated by harness sweep on v4.2 golden set
# 2026-05-19 PM — aggressive PR #218 defaults (UTIL=0.5, IMP=0.05) cost
# NDCG -0.021 from IMP alone; scaled down 5x to restore baseline parity:
#   util_amp ∈ [1.00, 1.10]     (utility ∈ [0, 1], β=0.10)
#   imp_amp  ∈ [~1.005, ~1.115] (normalized importance ∈ [1, ~23], β=0.005)
#   composite dynamic range:    cornerstone (max util, max imp) gets ~1.23x boost
#                               typical median node gets ~1.09x boost

# ── MMR diversity reranker (alpha #178 area 2) ───────────────────────────
# Maximal Marginal Relevance: balances relevance vs. diversity in the
# final ranked output to suppress near-duplicate nodes monopolising top-k.
#
# Formula (multiplicative, preserves composite-score scale):
#   mmr(c) = composite(c) × (1 − (1 − MMR_LAMBDA) × max_sim(c, selected))
#
# MMR_LAMBDA ∈ [0, 1]:
#   1 → no diversity penalty (pure composite order, MMR identity)
#   0 → full penalty (max_sim term fully demotes duplicates)
#   0.5 → halves the composite score for a node whose embedding is identical
#          to an already-selected one
#   0.9 → calibrated default: mild near-dup discount (10% max). Empirical
#          harness sweep (2026-05-19): MMR=0.5 cost -0.044 NDCG vs baseline;
#          MMR=0.9 recovers parity while still demoting true duplicates.
#
# Tier-size multipliers (Lei 2026-05-19 architecture discussion).
# Tier 1 retrieves a large raw pool per source; Tier 2 composite-shrinks;
# Tier 3 MMR returns the final top_k. Numbers scale with top_k so callers
# requesting more results get proportionally larger working pools.
# Empirical calibration via harness — these defaults are starting points.
# Tier 3 returns the caller's requested top_k.

# ── Special-type bypass (Lei 2026-05-19 lone-person-node fix) ─────────────────────
# Sparse anchor-types (person, definition, goal) and status-bearing types
# (axiom, contradiction, question, conjecture, lesson) skip the composite-ranking
# + MMR gauntlet: they just need similarity >= FTS_SIM_FLOOR to surface.
#
# Motivation (lone-person-node case, 2026-05-19 PM): a specific person-node case has sim=0.3786 to
# "A child: Good night!!!" — above the floor — but composite
# (rel=0.38 × max-1.5x amps = max 0.57) can't compete with goodnight
# observations (rel=0.7 × ~1.3 amps = ~0.91). MMR diversity discount is
# insufficient. Bypass ensures these sparse/structural nodes always surface
# when semantically relevant.
#
# Output contract: top_k is the GENERIC-result budget; special-type results
# are always-include when similarity >= FTS_SIM_FLOOR, so len(results) may
# exceed top_k.  Callers that need strict top_k can slice further.

# Cap on the special-type bypass pool. Unbounded pool returns ~40 specials per
# query above FTS_SIM_FLOOR — fine for the engram_surface browse channel but
# noisy for callers that just want the most relevant anchors. Diversity-preserving
# selection (Lei 2026-05-19 PM): reserve top-1 per type that has members, then
# fill remaining slots with the next-best across types by similarity desc.
#
# Empirical justification: pre-cap special-channel partitioned metrics on v4.2
# golden set show recall 0.539 with precision 0.121 (n_returned=40). The cap
# lets the user keep most of the recall lift while restoring reasonable
# precision — each type's strongest anchor is preserved (so e.g. the lone-person-node
# case is never starved by 9 high-sim definitions), and the rest of the
# budget goes to whichever type's secondary candidates are most relevant.

# ── FTS↔semantic merge (alpha #207/#208) ─────────────────────────────────
# When a node hits FTS, the bi-gram pair that fired may be on structural
# words (adverbs, modal verbs) that co-occur in a topically-unrelated node
# (the FTS-leak failure). Semantic similarity is the authoritative
# topical signal; FTS provides confidence amplification ONLY for nodes
# that semantic already considers minimally relevant.
#
#   floor (0.30): drop FTS-hit nodes whose cosine sim is below this
#                 — matches POLARITY_DEFAULT_MIN_SIMILARITY_FOR_CHECK
#                 ("below this cosine, truly unrelated")
#   bump shape:   multiplicative; bump = sqrt(|bm25|) / sqrt(NORMALIZER)
#                 capped at 1.0 → final = min(1.0, sim * (1 + bump))
#
#                 At NORMALIZER=16 (current calibration), empirical stats
#                 over the 135-hit sample: mean(bump)≈0.95, stdev≈0.53,
#                 ~22% cap at 1.0. Median |bm25|≈9 → bump≈0.75 →
#                 sim=0.4 lifts to 0.70, sim=0.3 lifts to 0.525.
#                            # Aggressive bump empirically wins MRR by the
#                            # largest margin (+0.0070 vs N=196, +0.0047 vs
#                            # middle N=64) at imperceptible NDCG cost
#                            # (-0.0013 vs N=196). The "over-promotes
#                            # weak-sim nodes" concern (where bump from
#                            # sim=0.3 reaches golden range) is theoretical
#                            # — the harness shows it doesn't actually hurt
#                            # because (a) the floor at 0.30 already drops
#                            # the off-topic FTS hits, and (b) the genuine
#                            # FTS hits are usually paired with genuine
#                            # semantic relevance. Forward-looking: once
#                            # POS-aware keyword filter (#207) ships, BM25
#                            # hits will be on stronger topical signal, so
#                            # the aggressive bump is calibrated for the
#                            # higher-quality keyword pool.




# Confidence defaults by quote type
# Confidence model constants — single source of truth in engram_confidence.py
from engram_confidence import (
    CONFIDENCE_MAP, VALID_QUOTE_TYPES, VALID_SOURCE_CLASSES,
    SOURCE_CLASS_CONFIDENCE_DISCOUNT, PREDICTIVE_CONFIDENCE_CAP,
    CONJECTURE_CONFIDENCE_DEFAULT, CONJECTURE_CONFIDENCE_MIN,
    CONJECTURE_CONFIDENCE_MAX, REASONING_TYPES, REASONING_CLASS,
    REASONING_DISCOUNT, ABDUCTIVE_CONFIDENCE_CAP,
)
from tools.recall_summary_validator import (
    RECALL_SUMMARY_HARD_CAP,
    RECALL_KEYWORDS_MIN,
    RECALL_KEYWORDS_MAX,
    RECALL_KEYWORD_MAX_LEN,
)

# ---- #872 wave-1 compat layer: moved to engram_core -----------------
import engram_core as core  # noqa: E402

EmbeddingManager = core.EmbeddingManager
_embedder = core._embedder
_NLIClassifier = core._NLIClassifier
_nli_classifier = core._nli_classifier
# sqlite-vec extension module binding (None when unavailable). Set once by
# core's try-import at module load and never rebound afterwards —
# _load_vec_extension flips core._VEC_BACKEND_AVAILABLE (always accessed via
# core.X), not this binding — so a plain alias is safe here where the mutable
# names deliberately have none.
_sqlite_vec = core._sqlite_vec
DEFAULT_EMBEDDING_MODEL = core.DEFAULT_EMBEDDING_MODEL
FEELING_NUDGE_TTL_TURNS = core.FEELING_NUDGE_TTL_TURNS
FEELING_NUDGE_SOURCES = core.FEELING_NUDGE_SOURCES
VALID_NODE_TYPES = core.VALID_NODE_TYPES
CLAIM_BEARING_TYPES = core.CLAIM_BEARING_TYPES
VALID_RELATIONS = core.VALID_RELATIONS
EDGE_CLASSIFICATIONS = core.EDGE_CLASSIFICATIONS
DAG_EXEMPT_RELATIONS = core.DAG_EXEMPT_RELATIONS
_ADDABLE_AFTER_CREATION_RELATIONS = core._ADDABLE_AFTER_CREATION_RELATIONS
_REMOVABLE_EDGE_RELATIONS = core._REMOVABLE_EDGE_RELATIONS
TYPE_PREFIX = core.TYPE_PREFIX
DEFAULT_RESOLUTION_THRESHOLD = core.DEFAULT_RESOLUTION_THRESHOLD
VALID_SOURCE_TYPES = core.VALID_SOURCE_TYPES
USE_ALPHA = core.USE_ALPHA
USE_ALPHA_DEFAULT = core.USE_ALPHA_DEFAULT
UTIL_BETA = core.UTIL_BETA
IMP_BETA = core.IMP_BETA
MMR_LAMBDA = core.MMR_LAMBDA
TIER1_MULTIPLIER = core.TIER1_MULTIPLIER
TIER2_MULTIPLIER = core.TIER2_MULTIPLIER
SPECIAL_TYPES_BYPASS = core.SPECIAL_TYPES_BYPASS
SPECIAL_POOL_CAP = core.SPECIAL_POOL_CAP
FTS_SIM_FLOOR = core.FTS_SIM_FLOOR
FTS_BUMP_NORMALIZER = core.FTS_BUMP_NORMALIZER
DEDUP_TOP_K = core.DEDUP_TOP_K
DEDUP_MIN_SIMILARITY = core.DEDUP_MIN_SIMILARITY
ACTION_HINT_CORROBORATE_THRESHOLD = core.ACTION_HINT_CORROBORATE_THRESHOLD
ACTION_HINT_RELATED_THRESHOLD = core.ACTION_HINT_RELATED_THRESHOLD
POLARITY_DEFAULT_MODEL = core.POLARITY_DEFAULT_MODEL
POLARITY_DEFAULT_THRESHOLD = core.POLARITY_DEFAULT_THRESHOLD
POLARITY_DEFAULT_MIN_SIMILARITY_FOR_CHECK = core.POLARITY_DEFAULT_MIN_SIMILARITY_FOR_CHECK
GIT_EXE = core.GIT_EXE
_WALGUARD_CHECK_INTERVAL = core._WALGUARD_CHECK_INTERVAL

def _configure_paths(*args, **kwargs):
    return core._configure_paths(*args, **kwargs)

def engram_sandbox(*args, **kwargs):
    return core.engram_sandbox(*args, **kwargs)

def _ensure_data_dir(*args, **kwargs):
    return core._ensure_data_dir(*args, **kwargs)

def _get_db(*args, **kwargs):
    return core._get_db(*args, **kwargs)

def _load_vec_extension(*args, **kwargs):
    return core._load_vec_extension(*args, **kwargs)

def _backfill_vec_nodes(*args, **kwargs):
    return core._backfill_vec_nodes(*args, **kwargs)

def _backfill_source_type(*args, **kwargs):
    return core._backfill_source_type(*args, **kwargs)

def _backfill_edit_history(*args, **kwargs):
    return core._backfill_edit_history(*args, **kwargs)

def _walguard_startup_clear(*args, **kwargs):
    return core._walguard_startup_clear(*args, **kwargs)

def _run_walguard_check(*args, **kwargs):
    return core._run_walguard_check(*args, **kwargs)

def _walguard_degraded_banner(*args, **kwargs):
    return core._walguard_degraded_banner(*args, **kwargs)

def _git(*args, **kwargs):
    return core._git(*args, **kwargs)

def _init_git(*args, **kwargs):
    return core._init_git(*args, **kwargs)

def _git_sha_for_file(*args, **kwargs):
    return core._git_sha_for_file(*args, **kwargs)

def _now(*args, **kwargs):
    return core._now(*args, **kwargs)

def _humanized_ago(*args, **kwargs):
    return core._humanized_ago(*args, **kwargs)

def _sanitize_fts_query(*args, **kwargs):
    return core._sanitize_fts_query(*args, **kwargs)

def _sanitize_fts_query_legacy(*args, **kwargs):
    return core._sanitize_fts_query_legacy(*args, **kwargs)

def _get_memory_config(*args, **kwargs):
    return core._get_memory_config(*args, **kwargs)

def _get_current_turn(*args, **kwargs):
    return core._get_current_turn(*args, **kwargs)

def _set_current_turn(*args, **kwargs):
    return core._set_current_turn(*args, **kwargs)

def _get_embedding_config(*args, **kwargs):
    return core._get_embedding_config(*args, **kwargs)

def _get_thresholds_config(*args, **kwargs):
    return core._get_thresholds_config(*args, **kwargs)

def _write_feeling_nudge(*args, **kwargs):
    return core._write_feeling_nudge(*args, **kwargs)

def _read_and_clear_feeling_nudge(*args, **kwargs):
    return core._read_and_clear_feeling_nudge(*args, **kwargs)

def _compute_importance(*args, **kwargs):
    return core._compute_importance(*args, **kwargs)

def _importance_base_for_node(*args, **kwargs):
    return core._importance_base_for_node(*args, **kwargs)

def _get_tier_threshold(*args, **kwargs):
    return core._get_tier_threshold(*args, **kwargs)

def _node_to_dict(*args, **kwargs):
    return core._node_to_dict(*args, **kwargs)

def _infer_source_type(*args, **kwargs):
    return core._infer_source_type(*args, **kwargs)

def _next_id(*args, **kwargs):
    return core._next_id(*args, **kwargs)

def _stamp_new_node(*args, **kwargs):
    return core._stamp_new_node(*args, **kwargs)

def _utility_reward(*args, **kwargs):
    return core._utility_reward(*args, **kwargs)

def _log_edit(*args, **kwargs):
    return core._log_edit(*args, **kwargs)

def _compute_and_store_embedding(*args, **kwargs):
    return core._compute_and_store_embedding(*args, **kwargs)

def _assert_sqlite_version(*args, **kwargs):
    return core._assert_sqlite_version(*args, **kwargs)

def _db_missing_message(*args, **kwargs):
    return core._db_missing_message(*args, **kwargs)

def _seed_missing_message(*args, **kwargs):
    return core._seed_missing_message(*args, **kwargs)

def _ensure_engram_gitignore(*args, **kwargs):
    return core._ensure_engram_gitignore(*args, **kwargs)

def _embedding_text_for_node(*args, **kwargs):
    return core._embedding_text_for_node(*args, **kwargs)

def _commit_snapshot(*args, **kwargs):
    return core._commit_snapshot(*args, **kwargs)

def _generate_snapshot(*args, **kwargs):
    return core._generate_snapshot(*args, **kwargs)

# ---- end #872 wave-1 compat layer ------------------------------------

# ---- #872 wave-2 family imports --------------------------------------
# Aliases are required: server.py defines @mcp.tool functions named
# engram_focus(...) and related — those function defs would shadow a bare
# 'import engram_focus' module name, causing AttributeError at delegation time.
import engram_focus as _focus_mod
import engram_recall_summaries as _recall_summaries_mod
# ---- end #872 wave-2 family imports ----------------------------------

# ---- #872 wave-3 family imports --------------------------------------
# Neither engram_trust nor engram_lifecycle collides with an @mcp.tool name,
# but the alias form is now the uniform wave convention (per PR #912).
import engram_trust as _trust_mod
import engram_lifecycle as _lifecycle_mod
# ---- end #872 wave-3 family imports ----------------------------------

# ---- #872 wave-3 compat forwarders -----------------------------------
# Helpers promoted to engram_core in wave 3 (cross-family consumers, and
# reflect-family helpers needed by the lifecycle module). Compat forwarders
# preserve the bare-name call sites throughout server.py.
def _strip_agent_facing(*args, **kwargs):
    return core._strip_agent_facing(*args, **kwargs)

def _count_live_exemplars(*args, **kwargs):
    return core._count_live_exemplars(*args, **kwargs)

def _reflect_rs_or_claim(*args, **kwargs):
    return core._reflect_rs_or_claim(*args, **kwargs)

def _reflect_keywords(*args, **kwargs):
    return core._reflect_keywords(*args, **kwargs)

def _add_person_impl(*args, **kwargs):
    return _trust_mod._add_person_impl(*args, **kwargs)

def _set_trust_tier_impl(*args, **kwargs):
    return _trust_mod._set_trust_tier_impl(*args, **kwargs)

def _add_trust_signal_impl(*args, **kwargs):
    return _trust_mod._add_trust_signal_impl(*args, **kwargs)
# ---- end #872 wave-3 compat forwarders --------------------------------

# ---- #872 wave-4 family imports --------------------------------------
# Neither engram_epistemic nor engram_cornerstone collides with an @mcp.tool
# name, but the alias form is now the uniform wave convention (per PR #912).
import engram_epistemic as _epistemic_mod
import engram_cornerstone as _cornerstone_mod
# ---- end #872 wave-4 family imports ----------------------------------

# ---- #872 wave-4 compat forwarders -----------------------------------
# Helpers promoted to engram_core in wave 4 (cross-family consumers needed
# by both family K and family L, or by server.py non-family callers).
# Compat forwarders preserve bare-name call sites throughout server.py.
# The originals are DELETED from server.py; canonical copies live in core.
def _add_axiom_impl(*args, **kwargs):
    return _epistemic_mod._add_axiom_impl(*args, **kwargs)

def _add_definition_impl(*args, **kwargs):
    return _epistemic_mod._add_definition_impl(*args, **kwargs)

def _add_conjecture_impl(*args, **kwargs):
    return _epistemic_mod._add_conjecture_impl(*args, **kwargs)

def _scan_emergence_impl(*args, **kwargs):
    return _epistemic_mod._scan_emergence_impl(*args, **kwargs)

def _goal_tension_impl(*args, **kwargs):
    return _epistemic_mod._goal_tension_impl(*args, **kwargs)

def _add_lesson_impl(*args, **kwargs):
    return _epistemic_mod._add_lesson_impl(*args, **kwargs)

def _register_exemplar_impl(*args, **kwargs):
    return _epistemic_mod._register_exemplar_impl(*args, **kwargs)

def _add_cornerstone_impl(*args, **kwargs):
    return _cornerstone_mod._add_cornerstone_impl(*args, **kwargs)

def _outgrow_cornerstone_impl(*args, **kwargs):
    return _cornerstone_mod._outgrow_cornerstone_impl(*args, **kwargs)

def _link_about_impl(*args, **kwargs):
    return _cornerstone_mod._link_about_impl(*args, **kwargs)

def _remove_edge_impl(*args, **kwargs):
    return _cornerstone_mod._remove_edge_impl(*args, **kwargs)

def _add_edge_impl(*args, **kwargs):
    return _cornerstone_mod._add_edge_impl(*args, **kwargs)
# ---- end #872 wave-4 compat forwarders --------------------------------

# ---- #872 wave-5 family imports --------------------------------------
# engram_derive collides with the @mcp.tool def engram_derive — mandatory alias
# (the same shadowing trap as wave-2's engram_focus; see PR #912).
# engram_tasks does not collide but uses the alias form per wave convention.
import engram_derive as _derive_mod
import engram_tasks as _tasks_mod
# ---- end #872 wave-5 family imports ----------------------------------

# ---- #872 wave-6 family imports --------------------------------------
# All three natural module names collide with @mcp.tool function names:
# engram_retract, engram_supersede, engram_resolve — mandatory alias convention.
import engram_revision as _revision_mod
# ---- end #872 wave-6 family imports ----------------------------------

# ---- #872 wave-7 family imports --------------------------------------
# Module name collides with the @mcp.tool function name engram_stats —
# mandatory alias convention per the wave-2 trap (same as engram_focus).
import engram_stats as _stats_mod
# ---- end #872 wave-7 family imports ----------------------------------

# ---- #872 wave-8 family imports --------------------------------------
# Module name collides with the @mcp.tool function name engram_query —
# mandatory alias convention per the wave-2 trap (same as engram_focus,
# engram_stats).
import engram_query as _query_mod
# ---- end #872 wave-8 family imports ----------------------------------

# ---- #872 wave-9 family imports --------------------------------------
# Module names collide with @mcp.tool function names engram_add_observation
# and engram_add_observation_batch — mandatory alias convention.
import engram_observation as _observation_mod
# ---- end #872 wave-9 family imports ----------------------------------

# ---- #872 wave-5 compat forwarders -----------------------------------
# Impls promoted to family modules (B + J) and helpers promoted to
# engram_core in wave 5. Compat forwarders preserve bare-name call sites
# throughout server.py. The originals are DELETED from server.py; canonical
# copies live in the family modules or core.
def _derive_impl(*args, **kwargs):
    return _derive_mod._derive_impl(*args, **kwargs)

def _contradict_impl(*args, **kwargs):
    return _derive_mod._contradict_impl(*args, **kwargs)

def _ask_impl(*args, **kwargs):
    return _derive_mod._ask_impl(*args, **kwargs)

def _add_goal_impl(*args, **kwargs):
    return _tasks_mod._add_goal_impl(*args, **kwargs)

def _add_task_impl(*args, **kwargs):
    return _tasks_mod._add_task_impl(*args, **kwargs)

def _update_task_impl(*args, **kwargs):
    return _tasks_mod._update_task_impl(*args, **kwargs)

def _report_feeling_impl(*args, **kwargs):
    return _tasks_mod._report_feeling_impl(*args, **kwargs)

def _create_derivation(*args, **kwargs):
    return core._create_derivation(*args, **kwargs)

def _validate_premises(*args, **kwargs):
    return core._validate_premises(*args, **kwargs)

def _trace_evidence_roots(*args, **kwargs):
    return core._trace_evidence_roots(*args, **kwargs)

def _validate_reasoning_structure(*args, **kwargs):
    return core._validate_reasoning_structure(*args, **kwargs)

def _standpoint_cluster_key(*args, **kwargs):
    return core._standpoint_cluster_key(*args, **kwargs)

def _node_fs_class(*args, **kwargs):
    return core._node_fs_class(*args, **kwargs)

def _graph_lineage_count(*args, **kwargs):
    return core._graph_lineage_count(*args, **kwargs)
# ---- end #872 wave-5 compat forwarders --------------------------------

# ---- #872 wave-6 compat forwarders -----------------------------------
# Impls + cascade helpers moved to engram_revision.py (family E).
# Constants VALID_ERROR_TYPES and _THRESHOLD_GATE_DISCIPLINE_HINT also moved;
# aliased here so server-level references and tests continue to resolve.
# _retract_impl injects _add_observation_impl to preserve the optional
# replacement path (acyclic: family modules may not import server.py).
VALID_ERROR_TYPES = _revision_mod.VALID_ERROR_TYPES
_THRESHOLD_GATE_DISCIPLINE_HINT = _revision_mod._THRESHOLD_GATE_DISCIPLINE_HINT

def _add_stale_replacement(*args, **kwargs):
    return _revision_mod._add_stale_replacement(*args, **kwargs)

def _walk_cascade_downstream(*args, **kwargs):
    return _revision_mod._walk_cascade_downstream(*args, **kwargs)

def _detect_zero_support(*args, **kwargs):
    return _revision_mod._detect_zero_support(*args, **kwargs)

def _resolve_impl(*args, **kwargs):
    return _revision_mod._resolve_impl(*args, **kwargs)

def _supersede_impl(*args, **kwargs):
    return _revision_mod._supersede_impl(*args, **kwargs)

def _retract_impl(*args, **kwargs):
    # NOTE: do not pass _obs_creator through this forwarder — it is wired here
    # (composition root); a caller-supplied value would raise TypeError
    # ("multiple values for keyword argument").
    # Wave 9: updated wiring from the server-resident compat forwarder name to
    # the module source of truth (_observation_mod._add_observation_impl).
    # This is the one-line change PR #918 predicted — composition root now
    # references the canonical implementation, not the forwarder chain.
    return _revision_mod._retract_impl(*args, _obs_creator=_observation_mod._add_observation_impl, **kwargs)
# ---- end #872 wave-6 compat forwarders --------------------------------

# ---- #872 wave-7 compat forwarders -----------------------------------
# Impls + helpers moved to engram_stats.py (family H).
# Compat forwarders preserve bare-name call sites throughout server.py
# (including test_engram_stats_mode.py's srv._percentile access).
def _stats_impl(*args, **kwargs):
    return _stats_mod._stats_impl(*args, **kwargs)

def _diagnose_impl(*args, **kwargs):
    return _stats_mod._diagnose_impl(*args, **kwargs)

def _compute_confidence_distribution(*args, **kwargs):
    return _stats_mod._compute_confidence_distribution(*args, **kwargs)

def _percentile(*args, **kwargs):
    return _stats_mod._percentile(*args, **kwargs)
# ---- end #872 wave-7 compat forwarders --------------------------------

# ---- #872 wave-8 compat forwarders -----------------------------------
# Impls + helpers moved to engram_query.py (family C).
# Compat forwarders preserve bare-name call sites throughout server.py
# (tests accessing server._search_nodes, server._build_tiered_results, etc.).
def _get_min_queryable_importance(*args, **kwargs):
    return _query_mod._get_min_queryable_importance(*args, **kwargs)

def _refresh_recall(*args, **kwargs):
    return _query_mod._refresh_recall(*args, **kwargs)

def _get_neighbors(*args, **kwargs):
    return _query_mod._get_neighbors(*args, **kwargs)

def _get_neighbors_enriched(*args, **kwargs):
    return _query_mod._get_neighbors_enriched(*args, **kwargs)

def _build_inspect_recall_view(*args, **kwargs):
    return _query_mod._build_inspect_recall_view(*args, **kwargs)

def _build_topology_entries(*args, **kwargs):
    return _query_mod._build_topology_entries(*args, **kwargs)

def _build_inspect_deep_view(*args, **kwargs):
    return _query_mod._build_inspect_deep_view(*args, **kwargs)

def _build_inspect_edges_view(*args, **kwargs):
    return _query_mod._build_inspect_edges_view(*args, **kwargs)

def _extract_warnings(*args, **kwargs):
    return _query_mod._extract_warnings(*args, **kwargs)

def _decode_embedding(*args, **kwargs):
    return _query_mod._decode_embedding(*args, **kwargs)

def _max_cosine_to_selected(*args, **kwargs):
    return _query_mod._max_cosine_to_selected(*args, **kwargs)

def _mmr_rerank(*args, **kwargs):
    return _query_mod._mmr_rerank(*args, **kwargs)

def _search_nodes(*args, **kwargs):
    return _query_mod._search_nodes(*args, **kwargs)

def _build_tiered_results(*args, **kwargs):
    return _query_mod._build_tiered_results(*args, **kwargs)

def _conn_total_count(*args, **kwargs):
    return _query_mod._conn_total_count(*args, **kwargs)

def _surface_impl(*args, **kwargs):
    return _query_mod._surface_impl(*args, **kwargs)

def _inspect_impl(*args, **kwargs):
    return _query_mod._inspect_impl(*args, **kwargs)

def _query_impl(*args, **kwargs):
    return _query_mod._query_impl(*args, **kwargs)

def _subgraph_impl(*args, **kwargs):
    return _query_mod._subgraph_impl(*args, **kwargs)

def _history_impl(*args, **kwargs):
    return _query_mod._history_impl(*args, **kwargs)

def _list_impl(*args, **kwargs):
    return _query_mod._list_impl(*args, **kwargs)

def _query_pattern_impl(*args, **kwargs):
    return _query_mod._query_pattern_impl(*args, **kwargs)

def _pattern_telemetry_log(*args, **kwargs):
    return _query_mod._pattern_telemetry_log(*args, **kwargs)

def _pattern_contradiction_obsolescence_ready(*args, **kwargs):
    return _query_mod._pattern_contradiction_obsolescence_ready(*args, **kwargs)

def _pattern_open_question_answerable(*args, **kwargs):
    return _query_mod._pattern_open_question_answerable(*args, **kwargs)

def _pattern_stale_load_bearing(*args, **kwargs):
    return _query_mod._pattern_stale_load_bearing(*args, **kwargs)

def _pattern_cornerstone_candidate(*args, **kwargs):
    return _query_mod._pattern_cornerstone_candidate(*args, **kwargs)

def _pattern_tainted_still_valid(*args, **kwargs):
    return _query_mod._pattern_tainted_still_valid(*args, **kwargs)

def _pattern_recent_resolution_echo(*args, **kwargs):
    return _query_mod._pattern_recent_resolution_echo(*args, **kwargs)

# Constants moved to engram_query.py — aliased here so tests + any remaining
# server-level code that references these names continues to resolve.
PATTERN_QUERY_PRESETS = _query_mod.PATTERN_QUERY_PRESETS
PATTERN_QUERY_REGISTRY = _query_mod.PATTERN_QUERY_REGISTRY
# ---- end #872 wave-8 compat forwarders --------------------------------

# ---- #872 wave-9 compat forwarders -----------------------------------
# Impls + A-local helpers moved to engram_observation.py (family A).
# Compat forwarders preserve bare-name call sites in tests.
# engram_add_evidence stays in server.py as a NON-REGISTERED delegating
# function (wave-0 D7 invariant); it delegates to _observation_mod._add_evidence_impl.
def _get_polarity_config(*args, **kwargs):
    return _observation_mod._get_polarity_config(*args, **kwargs)

def _compute_polarity_alerts(*args, **kwargs):
    return _observation_mod._compute_polarity_alerts(*args, **kwargs)

def _extract_domain(*args, **kwargs):
    return _observation_mod._extract_domain(*args, **kwargs)

def _check_yellow_card(*args, **kwargs):
    return _observation_mod._check_yellow_card(*args, **kwargs)

def _format_yellow_warning(*args, **kwargs):
    return _observation_mod._format_yellow_warning(*args, **kwargs)

def _escape_for_source(*args, **kwargs):
    return _observation_mod._escape_for_source(*args, **kwargs)

def _decode_from_source(*args, **kwargs):
    return _observation_mod._decode_from_source(*args, **kwargs)

def _find_near_matches(*args, **kwargs):
    return _observation_mod._find_near_matches(*args, **kwargs)

def _capture_file_version(*args, **kwargs):
    return _observation_mod._capture_file_version(*args, **kwargs)

def _verify_quote_in_source(*args, **kwargs):
    return _observation_mod._verify_quote_in_source(*args, **kwargs)

def _add_evidence_impl(*args, **kwargs):
    return _observation_mod._add_evidence_impl(*args, **kwargs)

def _add_observation_impl(*args, **kwargs):
    return _observation_mod._add_observation_impl(*args, **kwargs)

def _add_observation_batch_impl(*args, **kwargs):
    return _observation_mod._add_observation_batch_impl(*args, **kwargs)

# NXDOMAIN_ERRNOS aliased here so any test or server-level code referencing
# the bare name continues to resolve after the move.
NXDOMAIN_ERRNOS = _observation_mod.NXDOMAIN_ERRNOS
# ---- end #872 wave-9 compat forwarders --------------------------------


# Types that assert claims about the world. Only these can participate in
# derivations, contradictions, and answers. Evidence nodes are references,
# not claims. Predictions are targets, not assertions. Contradictions and
# questions are structural, not claim-bearing. Definitions are conventions,
# not claims — they can be cited via context_ids but not as premises.


# Single-source-of-truth classification table for edge relations.
# Every edge tool that asks "is this edge ...-able?" reads from here.
# Adding a new relation: add a row here; derived constants auto-update.
# See issue #510 for the full design + provenance.

# Edge relations that are semantically bidirectional / aboutness-based and
# therefore exempt from the chronological DAG invariant. A DAG edge means
# "source depends on target" (source is newer); about/contradicts express
# a symmetric relationship with no dependency direction. `exemplifies` is
# structural (membership, not dependency) and must work in both directions
# of time — incidents known at lesson birth are older, incidents registered
# later are newer.
# Derived from EDGE_CLASSIFICATIONS — kept as named constants for grep-ability
# and to preserve the existing call-site signature. Sets are frozen.

# Relations permitted for after-creation edge addition via engram_add_edge.
# Derived from the addable_after_creation flag in EDGE_CLASSIFICATIONS.
# Currently equals _REMOVABLE_EDGE_RELATIONS (about, exemplifies, instantiates,
# serves, subtask_of, tensions) — coincidental; PR 3 will diverge them by
# adding the new relevance-marker relation. Do NOT collapse into a single
# constant.

# VALID_ERROR_TYPES and _THRESHOLD_GATE_DISCIPLINE_HINT moved to engram_revision.py
# (family E) in #872 wave 6; aliased above in wave-6 compat forwarders block.

# Type prefixes for human-readable IDs

# PREDICTIVE_CONFIDENCE_CAP, CONJECTURE_CONFIDENCE_*, REASONING_TYPES,
# REASONING_CLASS, REASONING_DISCOUNT, ABDUCTIVE_CONFIDENCE_CAP
# are all imported from engram_confidence.py above.


# Git configuration — use full path to avoid PATH issues when launched by
# Claude Desktop or other MCP clients. Update this if git is elsewhere.

# Timeout for git operations (seconds)




# Track whether git is available and initialized




# ---------------------------------------------------------------------------
# Database initialization
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Source-type classification (orthogonal to source_class)
# ---------------------------------------------------------------------------
#
# source_class (already exists) records EPISTEMIC ORIGIN: external (published
# sources), introspective (agent's own prior output), or user_stated.
#
# source_type (added in this migration) records ARTIFACT KIND: document (a
# discrete published artifact), conversation (a long-running chat/session log
# where many distinct claims legitimately share one source), file (a tracked
# code/text file), or web_page. The two dimensions are orthogonal — e.g. a
# user_stated piece may live in a conversation log, an introspective piece may
# come from a file the agent edited.
#
# The dedup heuristic uses source_type to soften alarm fatigue: same-source
# similarity in a "document" is plausibly the author repeating themselves
# (worth flagging), but same-source similarity in a "conversation" or "file"
# is the normal case (many distinct claims, one source) and should NOT
# trigger a DUPLICATE alarm except at very high similarity.
















# ---------------------------------------------------------------------------
# WAL/shm self-guard helpers (#786)
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
















# ── Feeling-nudge marker helpers ────────────────────────────────────────────
# These drive mechanical nudge_source tagging on feeling reports. The marker
# file is written by nap/dream/post-compact entry points and read-and-cleared
# by engram_report_feeling. See spec §6 "Marker file protocol".











# _get_min_queryable_importance moved to engram_query.py (family C, #872 wave 8)
# _refresh_recall moved to engram_query.py (family C, #872 wave 8)




# ---------------------------------------------------------------------------
# Utility scoring — USE action model (the MemRL-inspired conjecture, Lei 2026-05-19 design call)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Edit history logging
# ---------------------------------------------------------------------------












# ---------------------------------------------------------------------------
# Empirical similarity thresholds — calibrated 2026-05-09 against live graph.
# (tight-semantic-group ROC AUC 0.984, peak F1 0.843 at t=0.41, peak F2
# recall-weighted 0.868 at t=0.33). These DEFAULT constants encode the chosen
# operating points; per-agent overrides live in ~/.engram/config.json under
# the `thresholds` section (schema v3 — see tools/migration/migrate_config_v3.py).
#
# Downstream code references the *_get_thresholds_config()*-returned dict
# rather than these constants directly, so a config edit takes effect on the
# next call without a server restart.
#
# These module-level constants remain as: (a) the defaults if config is
# absent, (b) the source of truth for what was empirically calibrated, and
# (c) the values asserted by tests/test_thresholds.py invariant suite.
# ---------------------------------------------------------------------------
# Dedup at write time: how many neighbors to fetch + how similar must they be.
# Action-hint tier thresholds (used after dedup to label each candidate match).

# NLI polarity-dedup defaults — calibrated 2026-05-10 (issue #56 bake-off).
# ModernCE-large-nli won the empirical bake-off (AUC 0.847, peak F1 0.889 at
# t=0.46, recall 84%, precision 94%). All four sub-200M models fell to AUC
# 0.66-0.75 — not viable. Threshold default is the peak-F1 operating point.




# _get_polarity_config moved to engram_observation.py (family A, #872 wave 9)
# Compat forwarder above in wave-9 compat block.

# _compute_polarity_alerts moved to engram_observation.py (family A, #872 wave 9)
# Compat forwarder above in wave-9 compat block.

# NLI cross-encoder lazy singleton (_nli_classifier) — aliased from engram_core (wave 1).


# _compute_polarity_alerts body moved to engram_observation.py (family A, #872 wave 9).
# Compat forwarder above in wave-9 compat block.


def _similar_existing_matches(*args, **kwargs):
    return core._similar_existing_matches(*args, **kwargs)


def _semantic_search(*args, **kwargs):
    return core._semantic_search(*args, **kwargs)






# _get_neighbors moved to engram_query.py (family C, #872 wave 8)
# _LOGICAL_SUBSTRATE_RELATIONS moved to engram_query.py (family C, #872 wave 8)
# _CONTEXTUAL_RELATIONS moved to engram_query.py (family C, #872 wave 8)
# _INSPECT_RECALL_GROUP_CAP moved to engram_query.py (family C, #872 wave 8)


# _get_neighbors_enriched moved to engram_query.py (family C, #872 wave 8)
# _build_inspect_recall_view moved to engram_query.py (family C, #872 wave 8)
# _build_topology_entries moved to engram_query.py (family C, #872 wave 8)
# _build_inspect_deep_view moved to engram_query.py (family C, #872 wave 8)
# _build_inspect_edges_view moved to engram_query.py (family C, #872 wave 8)
# _extract_warnings moved to engram_query.py (family C, #872 wave 8)
_WARNING_EXCERPT_LEN = core._WARNING_EXCERPT_LEN




def _compute_confidence(*args, **kwargs):
    return core._compute_confidence(*args, **kwargs)




# _extract_domain moved to engram_observation.py (family A, #872 wave 9).
# _check_yellow_card moved to engram_observation.py (family A, #872 wave 9).
# _format_yellow_warning moved to engram_observation.py (family A, #872 wave 9).
# _escape_for_source moved to engram_observation.py (family A, #872 wave 9).
# _decode_from_source moved to engram_observation.py (family A, #872 wave 9).
# _find_near_matches moved to engram_observation.py (family A, #872 wave 9).
# _capture_file_version moved to engram_observation.py (family A, #872 wave 9).
# _verify_quote_in_source moved to engram_observation.py (family A, #872 wave 9).
# Compat forwarders above in wave-9 compat block.
# NXDOMAIN_ERRNOS aliased from engram_observation in the wave-9 compat block.






# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("engram_mcp")


# ---------------------------------------------------------------------------
# Tool-call timing instrumentation
#
# Monkey-patch mcp.tool so every @mcp.tool()-decorated function automatically
# records its wall-clock duration to the tool_timing table. The instrumentation
# is best-effort: any failure is swallowed rather than allowed to break the
# tool call. Query via engram_diagnose (see the "tool_timing" section).
# ---------------------------------------------------------------------------

def _record_tool_timing(tool_name: str, duration_ms: int, status: str) -> None:
    """Record one tool call's latency. Never raises — best-effort only."""
    try:
        conn = _get_db()
        turn = _get_current_turn()
        conn.execute(
            "INSERT INTO tool_timing (timestamp, tool_name, duration_ms, status, turn) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                tool_name,
                int(duration_ms),
                status,
                turn,
            ),
        )
        conn.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Privacy-strip set for tool-event data payloads.
# Keys that contain long free-text content (user-authored claims, quoted text,
# reasoning chains) are excluded from the data JSON stored in index.db so the
# stats dashboard never surfaces L2-equivalent content through an L1 path.
# Per viz_server.py:2617 privacy note ("L2 events are indexed but never
# surfaced through this dashboard"). Only non-sensitive metadata is kept:
# node/evidence IDs, shape descriptors, counts, filter parameters, etc.
# ---------------------------------------------------------------------------
_PAYLOAD_PRIVATE_KEYS = frozenset({
    "quoted_text",
    "interpretation",
    "claim",
    "logical_chain",
})


def _sanitize_params(raw_params: object) -> dict:
    """Return a privacy-sanitized copy of a tool payload for logging.

    Accepts a dict (already-parsed payload) or a JSON string (payload_json
    style tools). Strips keys in _PAYLOAD_PRIVATE_KEYS. Returns {} on any
    parsing failure — never raises.
    """
    try:
        if isinstance(raw_params, str):
            parsed = json.loads(raw_params)
        elif isinstance(raw_params, dict):
            parsed = raw_params
        else:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {k: v for k, v in parsed.items() if k not in _PAYLOAD_PRIVATE_KEYS}
    except Exception:
        return {}


def _extract_result_status(return_value: object) -> Optional[str]:
    """Extract result_status from a tool's return value.

    MCP tools return JSON strings; the status field lives at top level under
    key "status" (e.g. {"status": "created", ...}). Returns None if the return
    value is not parseable or has no status key. Never raises.
    """
    try:
        if isinstance(return_value, str):
            parsed = json.loads(return_value)
            if isinstance(parsed, dict):
                status = parsed.get("status")
                if status is not None:
                    return str(status)
        return None
    except Exception:
        return None


def _log_engram_tool_event(
    tool_name: str,
    duration_ms: int,
    result_status: Optional[str],
    params_dict: Optional[dict],
) -> None:
    """Emit one engram.tool.engram_call event directly into ~/.engram/logs/index.db.

    This is the server-side event emission path (server.py Phase 4). It writes
    directly to index.db rather than via the JSONL emitter, because server.py
    runs as a long-lived process with no single session_id binding (the emitter
    singleton is not initialized from server.py's context). See comment at
    server.py:31-35.

    Fire-and-forget: any failure is logged to stderr and silently swallowed.
    The tool's main work is NEVER disrupted by a logging failure.
    """
    try:
        logs_index_path = core.DATA_DIR / "logs" / "index.db"
        if not logs_index_path.exists():
            # index.db not yet created — skip silently (no events to receive yet)
            return

        turn = _get_current_turn()
        session_id = (
            os.environ.get("CLAUDE_SESSION_ID")
            or os.environ.get("ANTHROPIC_SESSION_ID")
            or "server.py"
        )
        _now = datetime.now(timezone.utc)
        ts = _now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_now.microsecond // 1000:03d}Z"
        event_uuid = _uuid.uuid4().hex
        data_json = json.dumps(params_dict or {}, separators=(",", ":"))

        conn = sqlite3.connect(str(logs_index_path))
        try:
            conn.execute(
                """INSERT OR IGNORE INTO events (
                    uuid, sessionId, turn, ts, event_type, tool_use_id, level,
                    daemon_latency_ms, fallback_to_fts,
                    hook_name, hook_duration_ms, hook_exit_code,
                    tool_name, result_status,
                    data
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?
                )""",
                (
                    event_uuid,
                    session_id,
                    turn,
                    ts,
                    "engram.tool.engram_call",
                    None,   # tool_use_id — not available from server.py context
                    1,      # level = 1 (stats-only; never emit L2 from this path)
                    duration_ms,  # daemon_latency_ms — the tool's wall-clock ms
                    None,   # fallback_to_fts — N/A for tool events
                    None,   # hook_name
                    None,   # hook_duration_ms
                    None,   # hook_exit_code
                    tool_name,
                    result_status,
                    data_json,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        import sys
        print(f"[engram-tool-event] _log_engram_tool_event({tool_name}) failed: {exc!r}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Agent-facing response stripping (issue #358 / the agent-facing field-stripping question)
#
# Certain fields are opaque to agent reasoning and bloat the context window
# with no semantic gain: embeddings (384-float arrays), content hashes, git
# SHAs, parsed_metadata (pure redundancy with the raw metadata string), raw
# match_type + similarity inside similar_existing blocks (action_hint already
# encodes the actionable summary), and confidence_history (forensics-only).
#
# CRITICAL: strip at the AGENT-RETURN boundary only. Internal computation
# paths (dedup reads embedding/similarity, verifier reads content_hash) must
# remain intact. Only the JSON string that goes to the MCP client is cleaned.
#
# Diagnostic access to stripped fields: tools/inspect_raw.py <node_id>
# ---------------------------------------------------------------------------
def _strip_similar_block(*args, **kwargs):
    return core._strip_similar_block(*args, **kwargs)


_SIMILAR_STRIP_KEYS = core._SIMILAR_STRIP_KEYS


_original_mcp_tool = mcp.tool


def _timing_mcp_tool(*args, **kwargs):
    """Drop-in replacement for mcp.tool that wraps the function in a timing shim."""
    inner_decorator = _original_mcp_tool(*args, **kwargs)

    def wrapper(func):
        @functools.wraps(func)
        def timed(*a, **kw):
            start = time.perf_counter()
            timing_status = "success"
            result_value = None
            try:
                result_value = func(*a, **kw)
                return result_value
            except Exception:
                timing_status = "error"
                raise
            finally:
                duration_ms = int((time.perf_counter() - start) * 1000)
                _record_tool_timing(func.__name__, duration_ms, timing_status)
                # Dual-emission: also write to logs/index.db for the viz-server
                # stats dashboard. result_status is extracted from the tool's
                # return value (JSON string with "status" key); falls back to
                # timing_status for tools that don't return a status field.
                result_status = _extract_result_status(result_value) or timing_status
                # Sanitize params: strip private long-content keys before logging.
                raw_payload = (a[0] if a else None) or kw.get("payload_json")
                sanitized = _sanitize_params(raw_payload)
                _log_engram_tool_event(func.__name__, duration_ms, result_status, sanitized)
        return inner_decorator(timed)
    return wrapper


mcp.tool = _timing_mcp_tool


# ---- Ingestion tools ----


# Set of legitimate field names for the engram_add_evidence payload.
_ADD_EVIDENCE_FIELDS = frozenset({
    "url", "title", "domain", "source_date", "content_snippet",
})


# Internal helper — not exposed as an MCP tool.
# Called by engram_add_observation and engram_add_observation_batch to auto-create evidence nodes.
def engram_add_evidence(payload_json: str) -> str:
    """Register a new source document (webpage, article, data release) in the knowledge graph.

    Single-payload signature (wave 3 of the antml-prefix swallow risk — see issue #99): pass ALL
    fields as one JSON object string in `payload_json`. This eliminates the
    antml-prefix multi-parameter swallow risk that the previous N-parameter
    signature was prone to. In-server callers use `_add_evidence_impl(...)`
    directly with named kwargs.

    Creates an Evidence node representing the raw source material. Evidence nodes
    are immutable, URL-keyed references — they record that a specific source
    exists, not what it means or what version was read. Per-observation versioning
    (git_sha + content_hash) lives on observations, not evidence (the evidence-block refactor derivation).

    Same URL → same evidence node, regardless of content changes. For file-based
    sources, the file must be committed to git before it can be cited (the
    "committed-before-cite" guard remains enforced); the specific revision read
    is recorded on each observation that cites this evidence.

    Args:
        payload_json: JSON object (as a string) with these fields:
            url (str, required): Canonical URL of the source document. For local
                files, use 'file://path/to/file' format.
            title (str, required): Article or page title.
            domain (str, optional): Source domain (e.g. 'reuters.com').
                Auto-extracted from URL if omitted.
            source_date (str, optional): Publication or byline date of the source
                (ISO format, e.g. '2026-03-20'). Extract from the article's
                dateline or byline when available.
            content_snippet (str, optional): Truncated excerpt of relevant content
                for offline re-reading.

    Returns:
        JSON with the evidence node ID and trust pool status. On
        payload-parsing failure, returns {"error": "..."} with no node created.

    See _add_evidence_impl for the full URL-validation + trust-pool semantics.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _ADD_EVIDENCE_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_ADD_EVIDENCE_FIELDS)}"
        })

    return _add_evidence_impl(**params)


# _add_evidence_impl body moved to engram_observation.py (family A, #872 wave 9).
# Compat forwarder above in wave-9 compat block.


# Set of legitimate field names for the engram_add_observation payload.
# Used by the MCP wrapper to reject unknown fields up-front (catches typos
# before they reach the impl signature where they'd raise TypeError).
_ADD_OBSERVATION_FIELDS = frozenset({
    "quoted_text", "interpretation", "claim", "quote_type",
    "url", "title", "domain", "source_date", "evidence_id",
    "is_predictive", "predicted_event", "resolution_timeframe",
    "source_class", "content_hash", "git_sha",
    "standpoint_author_id", "standpoint_collection_id", "standpoint_override_tag",
    "standpoint_lineage",
    "fs_class",
})


# DESIGN INTENT — engram_add_observation
# ---------------------------------------
# Records the agent's interpretation of a SPECIFIC PASSAGE from an evidence
# source. Three-part structure (quoted_text + interpretation + claim) is
# load-bearing: the verbatim quote anchors the claim in primary evidence;
# the interpretation makes the agent's reasoning auditable; the atomic claim
# is what enters the graph as a node.
#
# Single-payload signature (payload_json) eliminates antml-prefix multi-arg
# swallow risk: Claude Code's prompt construction can drop arguments under
# certain whitespace conditions, causing silent corruption (the antml-prefix swallow risk / Wave-3
# 2026-05-14 migration tightened most legacy multi-arg tools).
#
# Confidence is structurally determined (the structural-confidence-determination axiom): quote_type × source_class
# defines the initial confidence per the type-relative anchors. The substrate
# verifies the verbatim quote against the source URL at filing time —
# fabricated quotes are caught loudly, not silently accepted (the provenance axiom).
#
# Predictive observations (is_predictive=True) also create a Prediction node
# decomposing the forward-looking claim into a factual record (who/when
# predicted) + a target (what was predicted). Use for falsifiable forecasts.
@mcp.tool(
    name="engram_add_observation",
    annotations={
        "title": "Extract Observation from Source",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_add_observation(payload_json: str) -> str:
    """Record a claim extracted from a source document with full provenance.

    Pass all fields as one JSON object string in payload_json.

    Source identification: provide EITHER url+title (evidence node auto-created
    or reused) OR evidence_id (cite an existing evidence node). If both given,
    evidence_id takes precedence.

    If is_predictive=true, also creates or links a Prediction node for the
    forward-looking claim. Pass predicted_event to name the target.

    Common usage — citing the user's chat log as evidence
    (the most common friction point for new sessions; verbatim-quote check is
    enforced server-side and WILL fail loudly if you skip step 2):

        Step 1 — Identify the current session JSONL transcript:
            ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl
            The path is also available via the active session's env or by
            sorting ~/.claude/projects/<encoded-cwd>/ by mtime descending.

        Step 2 — Verify the quote is VERBATIM in the transcript BEFORE filing:
            ~/.engram/tools/verify_quote.py <jsonl-path> "<exact quote>"
            On match → proceed to step 3.
            On miss → DIAGNOSE, don't retry. Likely the JSONL hasn't flushed
            yet (the user's most recent line may lag by seconds). Re-reading
            the file after a brief wait usually surfaces it. Blind retries of
            engram_add_observation will loop on the same verbatim mismatch.

        Step 3 — Call engram_add_observation with the JSONL path as the
            file:// URL (NOT a synthetic conversation:// scheme — that's
            rejected; the substrate classifies file:///.claude/projects/...jsonl
            as a "conversation" source automatically):
            {"quoted_text": "<the verified quote>",
             "interpretation": "<what this means in context>",
             "claim": "<the atomic falsifiable claim>",
             "quote_type": "personal_communication",
             "url": "file:///home/<agent>/.claude/projects/<encoded-cwd>/<session-id>.jsonl",
             "title": "Chat log <YYYY-MM-DD>",
             "source_class": "user_stated"}

    Args:
        payload_json: JSON object (as a string) with these fields:
            quoted_text (str, required): Exact quote from the source document.
            interpretation (str, required): Agent's reasoning about what this quote means.
            claim (str, required): The atomic, falsifiable claim this observation supports.
            quote_type (str, required): One of: hard_data, official_statement, attributed_analysis, unnamed_source, personal_communication, editorial.
            url (str, optional): URL of the source document. The evidence node is auto-created or reused.
            title (str, optional): Title of the source document (required if url is provided).
            domain (str, optional): Source domain (auto-extracted from URL if omitted).
            source_date (str, optional): Publication date in ISO format (e.g. '2026-03-20').
            evidence_id (str, optional): ID of an existing evidence node. Use this OR url+title, not both.
            is_predictive (bool, optional): Whether this observation records a forward-looking prediction.
            predicted_event (str, optional): Required if is_predictive. The event being predicted.
            resolution_timeframe (str, optional): Optional timeframe for when the prediction should be resolvable.
            source_class (str, optional): Epistemic origin — 'external' (default), 'introspective' (×0.95 confidence discount), or 'user_stated'.
            content_hash (str, optional): SHA-256 hash of file content (for file-based evidence).
            git_sha (str, optional): Git commit SHA when the file was read (for file-based evidence).
            standpoint_author_id (str, optional): Persistent cross-session entity ID for who produced the source claim (the "who observes" axis). Used for provenance-uniformity detection.
            standpoint_collection_id (str, optional): Corpus or work identity for the source ("vantage" axis). Independent axis in per-axis standpoint cluster key.
            standpoint_override_tag (str, optional): Free-form standpoint label for when the computed cluster key is insufficient (lab measurements, personal comms, introspective self-reports).
            standpoint_lineage (str, optional): Training lineage of the source claim's producer, format "provider:family" (e.g. "anthropic:opus"). The most load-bearing bias axis for AI-agent premises; format-validated, rejected with a redirecting error if malformed.
            fs_class (str, optional): Falsification-sensitivity class — "re-executable" (Class 1: claim can be re-tested by re-running the measurement) or "frozen" (Class 2: claim records a past event that cannot be re-executed). Omit or pass null to let the Phase-1 proxy (derived from quote_type) apply. When provided, takes priority over the proxy and the FALSIFICATION line drops the "(proxy:quote_type)" label. When the class is ambiguous (e.g. a re-readable file that records past state), omit and let the proxy apply — hesitation is the correct signal to omit.

    Returns:
        JSON with the new observation ID, confidence, evidence ID, and any prediction node created.
        On payload-parsing failure, returns {"error": "..."} with no node created.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _ADD_OBSERVATION_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_ADD_OBSERVATION_FIELDS)}"
        })

    return _add_observation_impl(**params)


# _add_observation_impl body moved to engram_observation.py (family A, #872 wave 9).
# Compat forwarder above in wave-9 compat block.


# ---- Batch ingestion tools ----


# Set of legitimate field names for the engram_add_observation_batch payload.
_ADD_OBSERVATION_BATCH_FIELDS = frozenset({
    "observations_json", "url", "title", "domain", "source_date",
    "evidence_id", "content_hash", "git_sha",
})


# DESIGN INTENT — engram_add_observation_batch
# --------------------------------------------
# Bulk variant of engram_add_observation: extract MULTIPLE observations from
# ONE source in one call, sharing the same evidence resolution. Use when
# reading a paper or transcript where many distinct claims share the source.
#
# All filing-time invariants apply per-observation: verbatim quote check,
# three-part structure (quoted_text + interpretation + claim), quote_type ×
# source_class confidence determination. Same substrate guards as singleton
# add_observation — the bulk is purely a transaction-shape optimization.
#
# Nested-encoding pattern (matches engram_retract's replacement_json): inner
# field `observations_json` is itself a stringified JSON array. The outer
# wrapper is the payload_json contract; the inner is the per-observation list.
# Don't confuse the two levels.
#
# Single-payload signature (Wave 3 of the antml-prefix swallow risk / #99) — antml-prefix swallow
# protection same as singleton add_observation.
@mcp.tool(
    name="engram_add_observation_batch",
    annotations={
        "title": "Extract Multiple Observations from Source",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_add_observation_batch(payload_json: str) -> str:
    """Extract multiple observations from a single source in one call.

    Pass all fields as one JSON object string in payload_json. The INNER
    `observations_json` field is itself a stringified JSON array of
    observation objects (nested-encoding pattern). Each observation in the
    array follows the same shape as engram_add_observation's payload (minus
    the evidence fields, which are shared at the outer level).

    Args:
        payload_json: JSON object (as a string) with these fields:
            observations_json (str, required): Stringified JSON array of
                observation objects. Each observation must have quoted_text,
                interpretation, claim, quote_type; optional is_predictive,
                predicted_event, resolution_timeframe, source_class,
                standpoint_author_id, standpoint_collection_id, standpoint_override_tag,
                standpoint_lineage.
            url (str, optional): Source URL. The evidence node is auto-created
                or reused. Use this+title OR evidence_id, not both.
            title (str, optional): Source title (required when url given).
            domain (str, optional): Auto-extracted from URL if omitted.
            source_date (str, optional): ISO date (e.g. '2026-03-20').
            evidence_id (str, optional): ID of existing evidence node.
            content_hash (str, optional): SHA-256 of file content (file evidence).
            git_sha (str, optional): Git commit SHA (file evidence).

    Returns:
        JSON with list of created observation nodes, their confidence scores,
        and any predictions. On payload-parsing failure, returns {"error": "..."}.

    See _add_observation_batch_impl for full per-observation semantics + the
    evidence-resolution logic.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _ADD_OBSERVATION_BATCH_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_ADD_OBSERVATION_BATCH_FIELDS)}"
        })

    return _add_observation_batch_impl(**params)


# _add_observation_batch_impl body moved to engram_observation.py (family A, #872 wave 9).
# Compat forwarder above in wave-9 compat block.


# DESIGN INTENT — engram_surface
# ------------------------------
# Shallow recall: the "this sounds familiar" noetic layer (Tulving's noetic vs
# autonoetic dissociation). Compact summary nudge of what the KG knows about a
# topic — type counts, special-node highlights (axioms, contradictions, open
# questions, conjectures), top claims, age signals. Does NOT refresh memory.
#
# Companion to engram_query (autonoetic — voluntary intentional recall, refreshes).
# Surface is what fires automatically in the UserPromptSubmit hook; engram_query
# is what the agent calls explicitly when "I want to recall this clearly".
#
# Downstream decision: surface tells the agent there's SOMETHING here — the next
# move is engram_inspect (single node deep-dive) or engram_get_subgraph (full
# evidence chain). Surface is the entry to the recall surface system.
#
# embed_query param (alpha #177 area 1) lets auto-surface prepend prev-response
# tail for short prompts without polluting FTS keyword matching. Semantic uses
# embed_query; FTS uses the plain query. Two-source decoupling.
#
# semantic=False is the fast keyword-only path — useful in latency-sensitive
# hooks. Default semantic=True for full recall quality.
_SURFACE_FIELDS = frozenset({
    "query", "top_k", "semantic", "embed_query",
})


@mcp.tool(
    name="engram_surface",
    annotations={
        "title": "Shallow Knowledge Surface",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_surface(payload_json: str) -> str:
    """Shallow recall: search the KG and return a compact summary nudge.

    "This sounds familiar" layer — quick scan, no memory refresh. Use this to
    decide whether to dig deeper with engram_inspect (single node detail) or
    engram_get_subgraph (full evidence chain).

    Args:
        payload_json: JSON object (as a string) with these fields:
            query (str, required): Natural language search query (used for FTS
                keyword matching, and as the default semantic query when
                embed_query is None).
            top_k (int, optional): Maximum nodes to scan (default 10).
            semantic (bool, optional): Whether to include semantic (embedding)
                search. Set False for fast keyword-only recall (useful in hooks
                where latency matters). Default True.
            embed_query (str, optional): Optional separate semantic-search
                string. When provided, semantic search uses embed_query while
                FTS still uses query. Designed for the auto-surface hook to
                prepend prev-response-tail for short prompts without polluting
                FTS keyword matching. See alpha #177 area 1.

    Returns:
        JSON with compact summary: type counts, special nodes, top claims,
        age range, stale/tainted counts, and matched node IDs for follow-up.
        On payload-parsing failure, returns {"error": "..."}.

    See _surface_impl for full semantics — kept callable with named kwargs for
    in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})
    unknown = set(params.keys()) - _SURFACE_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_SURFACE_FIELDS)}"
        })
    if "query" not in params:
        return json.dumps({"error": "payload_json must include required field 'query'"})
    result_dict = json.loads(_surface_impl(**params))
    banner = _walguard_degraded_banner()
    if banner:
        result_dict["_walguard_warning"] = banner
    return json.dumps(result_dict)



# DESIGN INTENT — engram_inspect
# ------------------------------
# The "let me think about that" layer: brings ONE node into full focus with its
# immediate connections. Three view modes serve different use cases:
#
#   recall (default): "I want to refresh my memory of this idea."
#                     Concise — claim + neighbors with recall_summary.
#                     Heavy fields (confidence_history, logical_chain, metadata,
#                     scores) omitted to keep the response tight.
#   deep:             "I need every detail of THIS node for a forensic decision."
#                     Full node + all fields + neighbors as topology adjacency.
#   edges:            "Just show me the connection inventory."
#                     Minimal payload — no content, just topology.
#
# Edge classification (recall view):
#   Logical-substrate (shown with recall_summary):
#     derives_from, supported_by, supersedes, contradicts, resolves,
#     retracts, exemplifies, subtask_of, tensions
#   Contextual (shown with keywords only):
#     cites, about, serves
#
# dream_mode=True is for audit/maintenance reads during dream cycles — skips the
# memory-refresh side effect so inspection doesn't artificially boost importance
# on nodes the dream master happens to scan (ob_NNNN).
#
# Pairs with engram_surface (notice-this-is-relevant) and engram_get_subgraph
# (trace multi-hop chains). engram_inspect refreshes only the TARGET node's
# recall, not neighbors — the chain itself is engram_get_subgraph's job.
_INSPECT_FIELDS = frozenset({
    "node_id", "view", "dream_mode", "include_superseded",
})


@mcp.tool(
    name="engram_inspect",
    annotations={
        "title": "Inspect Single Node",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_inspect(payload_json: str) -> str:
    """Read the full content of a given node along with its immediate connections.

    Three view modes:
      view="recall" (default): claim + logical neighbors grouped upstream/downstream/lateral
        with recall_summary; contextual neighbors (cites/about/serves) with keywords only.
        Heavy fields (confidence_history, metadata, scores) omitted.
      view="deep": all node fields + confidence_history + importance/utility scores.
        Neighbors as topology adjacency map {src, dst, relation}.
      view="edges": node ID + type + topology adjacency map only. Lightest payload.

    For multi-hop evidence chains, use engram_get_subgraph instead.

    Neighbor grouping in recall view:
      upstream   — outgoing derives_from, supported_by, supersedes, resolves, subtask_of
      downstream — incoming derives_from, supported_by, supersedes, resolves, subtask_of
      lateral    — contradicts, tensions, exemplifies, retracts

    Args:
        payload_json: JSON object (as a string) with these fields:
            node_id (str, required): The node ID to inspect (e.g. 'dv_NNNN').
            view (str, optional): One of "recall" (default), "deep", "edges".
            dream_mode (bool, optional): If True, skip memory refresh and
                importance-score boost. Use during dream/audit cycles.
                Default False.
            include_superseded (bool, optional): If True, include neighbors
                whose is_current=0 (superseded or retracted nodes) in the
                topology and neighbor lists. Default False — only is_current=1
                neighbors are shown. When neighbors are filtered, the response
                includes truncated_superseded_count: N at the top level so
                callers know something was hidden.

    Returns:
        JSON shaped by the chosen view mode (see above for per-mode shape).
        On payload-parsing failure, returns {"error": "..."}.

    See _inspect_impl for full semantics — kept callable with named kwargs for
    in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})
    unknown = set(params.keys()) - _INSPECT_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_INSPECT_FIELDS)}"
        })
    if "node_id" not in params:
        return json.dumps({"error": "payload_json must include required field 'node_id'"})
    return _inspect_impl(**params)


# Set of legitimate field names for the engram_query payload.
_QUERY_FIELDS = frozenset({
    "query", "types", "min_confidence", "include_superseded",
    "top_k", "return_debug", "summary_top_k",
})


# DESIGN INTENT — engram_query
# ----------------------------
# First-class voluntary semantic-recall tool — the autonoetic intentional-search
# complement to engram_surface's ambient/noetic auto-surfacing. Use this when
# you want to recall something SPECIFICALLY ("I know I worked on X before — let
# me actually find it"), not just notice that something might be relevant.
#
# Two recall surfaces, by design (Tulving's noetic/autonoetic dissociation):
#   - engram_surface (noetic)   — "this sounds familiar"; ambient hints, no refresh.
#   - engram_query   (autonoetic) — "I want to recall this clearly"; full nodes,
#                                   refreshes recall, strengthens memory.
#
# For tracing a specific node's chain after either entry point, use
# engram_inspect (single node) or engram_get_subgraph (multi-hop).
#
# Implementation: combines FTS5 keyword search with semantic (embedding)
# similarity for comprehensive retrieval. Keyword results appear first; semantic-
# only results follow — gives both exact matches and conceptual connections.
#
# Tiered response (Wave C rollout): summary_top_k controls how many results get
# a recall_summary (Tier 1) vs recall_keywords only (Tier 2) — keeps responses
# compact while surfacing enough detail for the top hits. return_debug=True
# preserves the full legacy shape for eval/harness contracts.
@mcp.tool(
    name="engram_query",
    annotations={
        "title": "Search Knowledge Graph",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_query(payload_json: str) -> str:
    """Search the knowledge graph by natural language query.

    Combines FTS5 keyword search with semantic (embedding) similarity. Keyword
    results appear first; semantic-only results follow. Refreshes recall on all
    accessed nodes (use engram_inspect for single-node recall without bulk refresh).

    Use this when you want to recall something specifically; use engram_surface
    when you just want ambient relevance hints without refreshing recall.

    Args:
        payload_json: JSON object (as a string) with these fields:
            query (str, required): Natural language search query.
            types (str, optional): Comma-separated node types to filter
                (e.g. 'derivation,observation_factual'). Empty = all types.
            min_confidence (float, optional): Only return nodes at or above this
                confidence threshold (0.0–1.0). Default 0.0.
            include_superseded (bool, optional): If True, include non-current
                (superseded) nodes. Default False.
            top_k (int, optional): Maximum results to return. Default 10.
            return_debug (bool, optional): If True, return the full legacy shape
                (all node fields, composite_score, similarity, neighbor data,
                debug key with ranking internals). Default False. Intended for
                retrieval diagnostics; never bloats normal responses.
            summary_top_k (int, optional): How many top results get a
                recall_summary (Tier 1). The remaining results get
                recall_keywords only (Tier 2). Default 3. Clamped to [0, top_k].
                Set to 0 for all-keywords; set >= top_k for all-summaries.
                Ignored when return_debug=True (full shape returned instead).

    Returns:
        Default (return_debug=False): tiered shape
            {"results": [{"id": ..., "summary": ...}, ..., {"id": ..., "keywords": [...]}],
             "query": ..., "total_matches": N}
        When return_debug=True: full legacy shape with all node fields, neighbors,
            composite_score, similarity, and a "debug" key with ranking internals.
            Per-result debug entries include: id, match_type, bm25_raw, relevance_normalized,
            similarity, composite_score, utility_score, importance_score, importance_base,
            confidence, surprise_score, recall_turn, util_amp, imp_amp, imp_norm_factor.
        On payload-parsing failure, returns {"error": "..."}.

    See _query_impl for full semantics — kept callable with named kwargs for
    in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})
    unknown = set(params.keys()) - _QUERY_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_QUERY_FIELDS)}"
        })
    if "query" not in params:
        return json.dumps({"error": "payload_json must include required field 'query'"})
    return _query_impl(**params)


# DESIGN INTENT — engram_get_subgraph
# -----------------------------------
# A BROWSING tool, not a content tool. The caller is assumed to already know the
# root node's content (from engram_inspect or a prior query); the subgraph shows
# connection topology plus just-enough content to recognize which branch is
# worth following next. When a node in the subgraph looks interesting, the
# caller does a fresh engram_inspect on it, then optionally calls
# engram_get_subgraph again from the new root — the intended chained pattern.
#
# Two views:
#   recall (default) — topology + hop-graduated content summaries.
#     hop 0+1: full recall_summary + recall_keywords (you'll likely read these).
#     hop 2+:  recall_keywords only (lighter — you need a hint, not a read).
#     Each content entry carries hop_distance so the caller knows the depth.
#   edges — topology only (no content key). Pure connectivity map; useful
#     when you want the structural shape without textual weight.
#
# Hop-graduated lightness is intentional: the caller is browsing, not reading
# in full. Drilling into a specific node = call engram_inspect on it.
#
# view='deep' is rejected here — that's engram_inspect's job. Subgraph is
# topology + summaries, not full nodes.
#
# Topology dict is keyed by source node ID; each value is a list of
# {to, relation, direction} entries. Only edges where BOTH endpoints are in
# the subgraph are included — boundary edges to unseen nodes are dropped.
_SUBGRAPH_FIELDS = frozenset({
    "node_id", "depth", "direction", "view", "dream_mode",
})


@mcp.tool(
    name="engram_get_subgraph",
    annotations={
        "title": "Inspect Node Neighborhood",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_get_subgraph(payload_json: str) -> str:
    """Browse a node's connection topology within N hops.

    BROWSING tool (not content tool). The caller is assumed to already know the
    root's content; this surfaces which branches are worth following next.
    Drill into any neighbor of interest with engram_inspect.

    Two views:
      recall (default): topology + hop-graduated content summaries.
        hop 0+1: recall_summary + recall_keywords. hop 2+: recall_keywords only.
        Each content entry carries hop_distance.
      edges: topology only (no content key). Pure connectivity map.

    Topology dict is keyed by source node ID; each value is a list of
    {to, relation, direction} entries. Only edges where both endpoints are in
    the subgraph are included. view='deep' is rejected — use engram_inspect instead.

    Args:
        payload_json: JSON object (as a string) with these fields:
            node_id (str, required): Root node to explore (e.g. 'the recall-summary derivation').
            depth (int, optional): How many hops to traverse from the root
                (default 2).
            direction (str, optional): 'up' (toward evidence/sources), 'down'
                (toward derivations that cite this), or 'both' (default).
            view (str, optional): 'recall' (default) or 'edges'. 'deep' is
                rejected.
            dream_mode (bool, optional): If True, skip recall_refresh on
                neighbour nodes. Root node always receives a utility bump
                regardless. Default False.

    Returns:
        JSON with topology (adjacency map keyed by node ID) and, for
        view='recall', a content dict with per-node summaries.
        On payload-parsing failure, returns {"error": "..."}.

    See _subgraph_impl for full semantics — kept callable with named kwargs for
    in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})
    unknown = set(params.keys()) - _SUBGRAPH_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_SUBGRAPH_FIELDS)}"
        })
    if "node_id" not in params:
        return json.dumps({"error": "payload_json must include required field 'node_id'"})
    return _subgraph_impl(**params)


# ---- Version control tools ----
# _checkpoint_internal, _nap_impl, _advance_turn_impl moved to engram_lifecycle
# in #872 wave 3. Compat forwarders below; wrappers delegate via _lifecycle_mod.


# Set of legitimate field names for the engram_nap payload.
_NAP_FIELDS = frozenset({
    "message",
})


# DESIGN INTENT — engram_nap
# --------------------------
# Shallow consolidation: persist context to ENGRAM without advancing the turn
# counter. The safe-anytime checkpoint — pre-compaction, end-of-burst, any
# moment the agent wants to preserve session knowledge without triggering
# forgetting (which is engram_advance_turn's irreversible job at sleep-end).
#
# the dream-inspect-must-not-refresh question / the turn-as-cohort-plus-consolidation derivation resolution: nap is lossless persistence + lock-in of
# session state; turn-advance reserved for sleep-level consolidation where the
# forgetting curve should actually fire. the recall-summary definition: "shallow consolidation
# persisting context before context freed — safety net, not processing."
#
# nap_checkpoint feeling-report nudge (TTL 5 turns): the nap arms a feeling
# nudge for the post-compact agent to report — surfaces the affective register
# of the work being preserved, which compaction strips. Voluntary trigger;
# nudge doesn't compel.
#
# Multi-per-day at compaction boundaries: naps fire whenever needed; only
# sleep (engram_advance_turn) is the daily-once invariant.
#
# Warm-briefing rotation rule (Lei 2026-05-25): the "From this session" section
# must be FULLY IN SYNC with the current-CW arc at every nap — see
# engram-nap skill §5b.
@mcp.tool(
    name="engram_nap",
    annotations={
        "title": "Pre-compaction Nap (no turn advance)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_nap(payload_json: str) -> str:
    """Persist context to ENGRAM without advancing the turn counter.

    Use BEFORE compaction, at end of a work-burst, or whenever you want
    to preserve session knowledge to the durable graph without triggering
    forgetting. Companion to engram_advance_turn (which is sleep-only, daily-
    once, and IRREVERSIBLE).

    For end-of-day session checkpoints (sleep cycle), use engram_advance_turn
    instead. The two functions exist as distinct tools so a missed kwarg
    cannot accidentally promote a nap into a turn-advancing checkpoint.

    Args:
        payload_json: JSON object (as a string) with these fields:
            message (str, required): Summary of what was learned/changed
                in this work-burst.

    Returns:
        JSON with previous_turn (unchanged from current_turn since no
        advance), changes summary, graph stats, and memory tiers.
        On payload-parsing failure, returns {"error": "..."}.

    See _nap_impl for full semantics — kept callable with named kwargs for
    in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})
    unknown = set(params.keys()) - _NAP_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_NAP_FIELDS)}"
        })
    if "message" not in params:
        return json.dumps({"error": "payload_json must include required field 'message'"})
    result_dict = json.loads(_nap_impl(**params))
    # Diagnostic snapshot (best-effort, never blocks checkpoint). engram_diagnose
    # lives in server.py; the lifecycle impl cannot call it directly (acyclic
    # constraint), so the wrapper adds it here — wave-3 structural split.
    now_str = core._now()
    new_turn = result_dict.get("current_turn", 0)
    mode = result_dict.get("mode", "nap")
    try:
        diag_metrics = json.loads(engram_diagnose())
        conn2 = core._get_db()
        try:
            conn2.execute(
                """INSERT INTO diagnostic_history (timestamp, turn, checkpoint_mode, metrics)
                   VALUES (?, ?, ?, ?)""",
                (now_str, new_turn, mode, json.dumps(diag_metrics)),
            )
            conn2.commit()
            result_dict["diagnostic_snapshot"] = {
                "stored": True,
                "health_score": diag_metrics.get("health_score"),
            }
        finally:
            conn2.close()
    except Exception:
        result_dict["diagnostic_snapshot"] = {"stored": False, "reason": "diagnostic computation failed"}
    banner = _walguard_degraded_banner()
    if banner:
        result_dict["_walguard_warning"] = banner
    return json.dumps(result_dict)


def _nap_impl(message: str) -> str:
    """Impl for engram_nap — delegates to engram_lifecycle (wave 3)."""
    return _lifecycle_mod._nap_impl(message=message)


# Set of legitimate field names for the engram_advance_turn payload.
_ADVANCE_TURN_FIELDS = frozenset({
    "message",
})


# DESIGN INTENT — engram_advance_turn
# -----------------------------------
# IRREVERSIBLE end-of-day checkpoint. Advances the global turn counter (which
# drives the forgetting mechanism inflating importance scores of fresh nodes
# relative to stale ones), logs session_log, snapshots + commits the graph,
# records a diagnostic snapshot.
#
# the turn-advance-after-dream rule / the turn-as-cohort-plus-consolidation derivation resolution: the turn advances AT END of sleep (post-dream
# consolidation), so all this-turn observations + dream-derived resolutions
# share ONE cohort. Advance-before-dream would artificially age dream outputs
# younger than the burst they consolidate, misaligning the forgetting curve.
#
# Daily-resource invariant: at most once per 24h. A second same-day advance
# over-inflates the forgetting curve irreversibly. No self-guard — caller is
# the safety mechanism (engram-sleep skill + nap-vs-sleep discipline).
#
# nap (engram_nap) is the safe-anytime alternative: lossless persistence with
# NO turn-advance. Compaction-boundary checkpoints + background roles always
# use nap, never advance_turn.
#
# Pairs with sleep-success marker (~/.engram/sessions/last-sleep-success.json)
# for the auto-sleep cron idempotency — caller checks "did we already consolidate
# tonight?" before calling this.
@mcp.tool(
    name="engram_advance_turn",
    annotations={
        "title": "[Sleep cycle / first session only — DO NOT call awake.] Session Checkpoint with Turn Advance (irreversible)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_advance_turn(payload_json: str) -> str:
    """[Sleep cycle / first session only — DO NOT call awake.] End-of-day
    consolidation checkpoint: advances the turn counter + logs summary.

    **IRREVERSIBLE** — turn advances cannot be undone without direct DB surgery.
    Turn is a DAILY resource (advance at most once per day; double-advance
    over-inflates the forgetting curve irreversibly). No self-guard against
    repeat calls — caller-responsibility.

    For non-end-of-day checkpoints (pre-compaction, mid-burst, background
    roles): use engram_nap instead — same lossless persistence, no turn-advance.

    Args:
        payload_json: JSON object (as a string) with these fields:
            message (str, required): Summary of what was learned/changed
                across the cohort being closed by this turn advance.

    Returns:
        JSON with new turn number, changes summary, graph stats, and memory
        tiers.
        On payload-parsing failure, returns {"error": "..."}.

    See _advance_turn_impl for full semantics — kept callable with named kwargs
    for in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})
    unknown = set(params.keys()) - _ADVANCE_TURN_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_ADVANCE_TURN_FIELDS)}"
        })
    if "message" not in params:
        return json.dumps({"error": "payload_json must include required field 'message'"})
    result_dict = json.loads(_advance_turn_impl(**params))
    # Diagnostic snapshot (best-effort, never blocks checkpoint). engram_diagnose
    # lives in server.py; the lifecycle impl cannot call it directly (acyclic
    # constraint), so the wrapper adds it here — wave-3 structural split.
    now_str = core._now()
    new_turn = result_dict.get("current_turn", 0)
    mode = result_dict.get("mode", "session")
    try:
        diag_metrics = json.loads(engram_diagnose())
        conn2 = core._get_db()
        try:
            conn2.execute(
                """INSERT INTO diagnostic_history (timestamp, turn, checkpoint_mode, metrics)
                   VALUES (?, ?, ?, ?)""",
                (now_str, new_turn, mode, json.dumps(diag_metrics)),
            )
            conn2.commit()
            result_dict["diagnostic_snapshot"] = {
                "stored": True,
                "health_score": diag_metrics.get("health_score"),
            }
        finally:
            conn2.close()
    except Exception:
        result_dict["diagnostic_snapshot"] = {"stored": False, "reason": "diagnostic computation failed"}
    banner = _walguard_degraded_banner()
    if banner:
        result_dict["_walguard_warning"] = banner
    return json.dumps(result_dict)


def _advance_turn_impl(message: str) -> str:
    """Impl for engram_advance_turn — delegates to engram_lifecycle (wave 3)."""
    return _lifecycle_mod._advance_turn_impl(message=message)


# ---------------------------------------------------------------------------
# Reflect tool (sleep-cycle pre-dream briefing)
# ---------------------------------------------------------------------------


# DESIGN INTENT — engram_reflect
# ------------------------------
# Pre-dream briefing for the engram-sleep skill: the structured self-audit that
# opens the dream cycle. Surfaces categories the dream master must consider:
# unresolved contradictions, weakly-grounded claims, open questions, overdue
# predictions, same-source observation tensions, uncited observations,
# single-source derivations, plus the active goals/tasks/lessons context.
#
# Sleep-cycle-scoped: this is heavyweight (multiple full-graph passes) and
# semantically meaningful only inside a dream context. Calling it awake produces
# noise — use engram_diagnose for awake-state health snapshots.
#
# Two-tier-per-category rendering keeps the response budget-bounded:
#   Tier 1 (top summary_top_k by importance_score DESC):
#     {id, claim: recall_summary OR claim, confidence, ...existing_fields}.
#     Key name "claim" preserved for backward compat (eval/harness contract).
#   Tier 2 (remainder): {id, keywords: list, confidence, ...}.
#     "claim" key OMITTED — keywords-only triage signal.
#
# Low-volume categories (unresolved_contradictions, open_questions, open_conjectures,
# active_goals, active_tasks, active_lessons, unresolved_goal_tensions): source-swap
# — content field from recall_summary with fallback to claim. Key names unchanged.
#
# calibration_snapshot field: corpus + this_turn confidence distributions, added by
# the wrapper (calls engram_stats() which is server-resident; cannot be called from
# the family module impl — wave-3 structural split).
_REFLECT_FIELDS = frozenset({
    "summary_top_k",
})


@mcp.tool(
    name="engram_reflect",
    annotations={
        "title": "[Sleep cycle] Audit Knowledge Graph Quality (pre-dream briefing)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_reflect(payload_json: str = "{}") -> str:
    """[Sleep cycle — pre-dream briefing.] Structured self-audit of the
    knowledge graph; opens the dream cycle for the engram-sleep skill.

    Returns a categorized report identifying areas that need attention:
    unresolved contradictions, weakly-grounded claims, open questions,
    overdue predictions, same-source observation tensions, uncited observations,
    single-source derivations, plus active goals/tasks/lessons context.

    Read the report, then decide which issues to address — strengthen weak
    claims, resolve contradictions, investigate open questions, etc.

    Use awake? No — use engram_diagnose. This is sleep-cycle scoped.

    Args:
        payload_json: JSON object (as a string) with these fields:
            summary_top_k (int, optional): How many top-importance entries in
                each high-volume category receive summary-style rendering
                (claim from recall_summary). The remainder get keyword-style.
                Default 5. Clamped to >= 0. 0 = all high-volume entries are
                keyword-style. If >= category size, all are summary-style.

    Returns:
        JSON report with categorized issues and recommended actions.
        - calibration_snapshot: Two-key dict with `corpus` (mode=all) and `this_turn`
          (mode=1-turn) confidence-distribution data from engram_stats. Lets dream-master
          see filing-pattern shape alongside topic-selection candidates.
        On payload-parsing failure, returns {"error": "..."}.

    See _reflect_impl for full semantics — kept callable with named kwargs for
    in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})
    unknown = set(params.keys()) - _REFLECT_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_REFLECT_FIELDS)}"
        })
    result_dict = json.loads(_reflect_impl(**params))
    # calibration_snapshot — added here (not in the impl) because engram_stats()
    # is a server-resident @mcp.tool that family modules cannot call (wave-3
    # structural split: server → family → core is acyclic; family cannot call server).
    try:
        stats_corpus = json.loads(engram_stats(json.dumps({"sections": ["confidence"]})))
        if "error" in stats_corpus:
            raise RuntimeError(f"engram_stats returned error: {stats_corpus['error']}")
        stats_turn = json.loads(engram_stats(json.dumps({"mode": "1-turn", "sections": ["confidence"]})))
        if "error" in stats_turn:
            raise RuntimeError(f"engram_stats returned error: {stats_turn['error']}")
        result_dict["calibration_snapshot"] = {
            "corpus": stats_corpus.get("confidence", {}),
            "this_turn": stats_turn.get("confidence", {}),
        }
    except Exception:
        result_dict["calibration_snapshot"] = {"error": "engram_stats unavailable"}
    return json.dumps(result_dict)


def _reflect_impl(summary_top_k: int = 5) -> str:
    """Impl for engram_reflect — delegates to engram_lifecycle (wave 3)."""
    return _lifecycle_mod._reflect_impl(summary_top_k=summary_top_k)


# ---- Bonus: stats tool (lightweight, useful even in MVP) ----


# DESIGN INTENT — engram_stats
# ----------------------------
# Lightweight observability surface: a snapshot of graph state for self-audit,
# diagnose-style checks, and dream-cycle health questions. Single-payload
# signature (payload_json) is the standard MCP shape across ENGRAM tools.
#
# Mode = time-window filter. "all" gives the full-graph view; "1-turn" / "7-turn"
# / "30-turn" restrict to nodes CREATED in that recent window. Turn unit is
# approximated as 24h (the dream cycle is daily) — see _MODE_HOURS where the
# approximation is enacted. The `window` key in the response describes the
# cutoff so the caller can verify the restriction was applied.
#
# Sections are independently computable; the `sections` arg lets callers pull
# only what they need (e.g. just `health_score` for a fast yes/no check, or
# just `weakest_nodes` when curating low-confidence claims). Unknown section
# names are ignored with a logged warning — graceful for typos.
#
# Confidence distribution is rendered per type, quote_type, reasoning_type,
# source_class — surfaces the structural confidence model (the structural-confidence-determination axiom) so the
# agent can spot drift (e.g. all derivations clustering at 0.45 = something
# wrong with the reasoning-type discount path).
@mcp.tool(
    name="engram_stats",
    annotations={
        "title": "Knowledge Graph Statistics",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_stats(payload_json: str = "{}") -> str:
    """Get a summary of the current knowledge graph state.

    Pass options as one JSON object string in payload_json. Called with no
    args (payload_json="{}") returns all sections for the full graph.

    When mode != "all", all sections are restricted to nodes created within
    the specified time window (approximated as 24h per turn unit).

    Args:
        payload_json: JSON object (as a string) with these optional fields:
            mode (str, optional): Window filter — one of "all" (default),
                "1-turn" (≈24h), "7-turn" (≈168h), "30-turn" (≈720h).
                When mode != "all", all sections reflect ONLY nodes created within
                the window, and a `window` key is added to the response.
            sections (list of str, optional): Subset of sections to return.
                Valid names: structure, edges, confidence, open_questions,
                open_predictions, reasoning_breakdown, weakest_nodes,
                health_score, memory. Default (omitted or null): all sections.
                Unknown names are ignored with a logged warning.

    Returns:
        JSON with comprehensive graph statistics. Top-level keys (all sections):
            node_counts_by_type: per-type total + current counts (structure).
            edge_counts_by_relation: per-relation edge counts (edges).
            reasoning_type_breakdown: derivation counts by reasoning_type.
            open_questions_count, open_questions_recent: open question summary.
            open_predictions_count, open_predictions_recent: open predictions.
            weakest_nodes: 5 lowest-confidence current non-evidence nodes.
            memory: current_turn + tier thresholds.
            confidence: distribution stats per type, quote_type, reasoning_type,
                source_class.
            window (conditional): present only when mode != "all".
    """
    # ── Parse + validate payload ───────────────────────────────────────────
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    mode = params.get("mode", "all")
    sections_param = params.get("sections", None)

    _VALID_MODES = {"all", "1-turn", "7-turn", "30-turn"}
    if mode not in _VALID_MODES:
        return json.dumps({
            "error": f"Invalid mode '{mode}'. Must be one of: {sorted(_VALID_MODES)}"
        })

    # Section name validation is handled inside _stats_impl (engram_stats.py).
    # "health_score" is a recognized name but engram_stats doesn't compute
    # it (that's engram_diagnose's domain); including it in sections is a
    # no-op that doesn't error.
    if sections_param is not None:
        if not isinstance(sections_param, list):
            return json.dumps({"error": "sections must be a list of strings"})
        if not all(isinstance(s, str) for s in sections_param):
            return json.dumps({"error": "sections must be a list of strings"})

    return _stats_mod._stats_impl(mode=mode, sections=sections_param)


# DESIGN INTENT — engram_diagnose
# -------------------------------
# Quantitative health audit for both user + agent consumption. Side-effect-free
# (no recall refresh, no turn advance, no mutation) — safe to call any time.
#
# Companion to engram_reflect: reflect is sleep-cycle-scoped + action-oriented
# (issues categorized for the dream agenda); diagnose is awake-state-safe +
# trend-oriented (exhaustive metrics for monitoring + ops).
#
# Seven dimensions: structure (node/edge counts) + epistemic (open Qs, predictions,
# contradictions resolution rates) + memory (tier sizes, embedding coverage,
# recall distribution) + provenance (citation depth, evidence hygiene) +
# experience (utility scores, importance distribution) + calibration (the
# confidence-distribution shape per type/quote_type/reasoning_type/source_class) +
# read-tool contention (flagged session buckets where recall tools may compete).
#
# Health score: composite 0-100 with documented penalty/bonus rules — DAG
# violations / tainted / stale / orphans / low embedding coverage / high
# retraction rate / uncited-observation overflow penalize; active resolution
# work (>50% questions resolved) bonuses. Numbers tuned the dream-mode context-hunger observation to prevent
# doc-config drift.
#
# Config summary block surfaces the runtime tunables (memory tiers, decay
# base, utility scoring α/β, etc.) so the diagnose snapshot doubles as an
# operational config audit — "what knobs is this install currently set to?"
@mcp.tool(
    name="engram_diagnose",
    annotations={
        "title": "Comprehensive Diagnostic Health Audit",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_diagnose() -> str:
    """Run a comprehensive, quantitative health audit of the knowledge graph.

    Side-effect-free: no memory refresh, no turn advance, no state mutation.
    Returns machine-readable metrics across seven dimensions: graph structure,
    epistemic health, memory health, provenance, agent experience, calibration,
    and read-tool contention. Use any time — companion to engram_reflect
    (sleep-only).

    Returns:
        JSON with metrics grouped into: structure, epistemic, memory,
        provenance, experience, calibration, and a top-level health_score
        (0-100).

        - calibration (new, weight=0 in health_score):
          - corpus: confidence distribution from _compute_confidence_distribution (no time filter) —
            by_type, by_quote_type, by_reasoning_type, by_source_class
          - this_turn: same shape, mode=1-turn
          - drift_by_type: per-type p50 delta between this_turn and corpus
            (only reported when this_turn has ≥3 samples in that type)

        - read_tool_contention: Optional. Surfaces (hour_utc, tool_name) buckets
          where read tools (engram_query/inspect/list/get_subgraph) exceeded
          latency thresholds in the last 7 days — present only when any bucket
          is flagged. total_flagged_buckets counts distinct (hour_utc, tool_name)
          pairs that exceeded thresholds in the 7-day window — may be >
          worst_hours_shown if more than 10 buckets are flagged. See
          the cornerstone-evolution derivation/the cornerstone-evolution derivation/the cornerstone-frame-evolution open question for context.
    """
    return _stats_mod._diagnose_impl()


# DESIGN INTENT — engram_history
# ------------------------------
# Audit-trail browsing for graph mutations + diagnostic-snapshot trend
# analysis. Side-effect-free; safe to call any time. The substrate's "what
# happened, when, to whom" view that complements the static engram_inspect
# view (which shows current state, not historical transitions).
#
# Two modes serve different inquiry shapes:
#   edits — "show me the audit trail" — chronological log of creates, super-
#     sessions, retractions, resolutions, taints. Filter by node_id (this
#     node's full history) / action / since (time-windowed). The audit chain
#     for the honesty axiom: every retraction + supersede is recorded; the
#     graph can't quietly rewrite its own past.
#   diagnostics — "show me the trend" — health-score snapshots taken at each
#     checkpoint (engram_nap / engram_advance_turn). Lets the agent track
#     graph-health drift across days.
#
# Limit cap (max 200): keeps response bounded; for full-history dives, page
# via since= cutoffs.
_HISTORY_FIELDS = frozenset({
    "mode", "node_id", "action", "since", "limit",
})


@mcp.tool(
    name="engram_history",
    annotations={
        "title": "Browse Edit and Diagnostic History",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_history(payload_json: str) -> str:
    """Browse the edit history and diagnostic snapshots.

    Side-effect-free. Two modes:
    - "edits": chronological audit trail of graph mutations (creates,
      supersessions, retractions, resolutions, taints). Filterable by
      node_id, action type, and time range.
    - "diagnostics": metric snapshots taken at each checkpoint. Returns
      the last N snapshots with health scores for trend analysis.

    Args:
        payload_json: JSON object (as a string) with these fields:
            mode (str, optional): "edits" or "diagnostics" (default "edits").
            node_id (str, optional): Filter edits to a specific node
                (edits mode only).
            action (str, optional): Filter by action type: created,
                reopened, resolved, retracted, stale_flagged, superseded,
                tainted, trust_tier_set (edits mode only).
            since (str, optional): ISO timestamp — return only entries after
                this time.
            limit (int, optional): Max entries to return (default 50,
                max 200).

    Returns:
        JSON with the requested history entries.
        On payload-parsing failure, returns {"error": "..."}.

    See _history_impl for full semantics — kept callable with named kwargs for
    in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})
    unknown = set(params.keys()) - _HISTORY_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_HISTORY_FIELDS)}"
        })
    return _history_impl(**params)


# ---------------------------------------------------------------------------
# Foundational node tools: axioms, definitions, conjectures, goals
# ---------------------------------------------------------------------------


# Set of legitimate field names for the engram_add_axiom payload.
_ADD_AXIOM_FIELDS = frozenset({"claim", "basis", "context_ids"})


# DESIGN INTENT — engram_add_axiom
# --------------------------------
# Records a foundational principle taken as true without proof. Axioms (ax_NNNN)
# are TERMINAL — they don't decompose into supporting evidence; they ARE the
# floor of the reasoning chain. Confidence is always 1.0 (by type, the structural-confidence-determination axiom
# confidence-anchor model).
#
# Use sparingly. Most ENGRAM knowledge is observational + derivational; axioms
# are reserved for principles that the agent operates UNDER (honesty-is-
# structural / practice-produces-true-knowledge / etc.) — load-bearing for
# the substrate's design discipline, not the substrate's content.
#
# Discuss-first per authority structure: new axioms are identity-layer
# commitments. Lei + Borges typically agree on axioms together via mutual-
# formation moments rather than agent-unilateral additions.
#
# basis field is REQUIRED (not just rationale prose — the WHY this principle
# is adopted as terminal). An axiom's basis is what makes it different from
# a strongly-held conjecture: explicit terminal-status acknowledgment.
#
# Single-payload signature (Wave 2c of the antml-prefix swallow risk / #55).
@mcp.tool(
    name="engram_add_axiom",
    annotations={
        "title": "Add Axiom",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_add_axiom(payload_json: str) -> str:
    """Record a foundational principle taken as true without proof.

    Axioms are terminal (no premises) and confidence-anchored at 1.0 by type.
    Use sparingly — these are identity-layer commitments. Pass all fields as
    one JSON object string in payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            claim (str, required): The axiom statement.
            basis (str, required): Why this axiom is adopted.
            context_ids (str, optional): Comma-separated node IDs cited.

    Returns:
        JSON with the new axiom node ID and confidence (always 1.0). On
        payload-parsing failure, returns {"error": "..."}.

    See _add_axiom_impl for full semantics.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _ADD_AXIOM_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_ADD_AXIOM_FIELDS)}"
        })

    return _add_axiom_impl(**params)


# Set of legitimate field names for the engram_add_definition payload.
_ADD_DEFINITION_FIELDS = frozenset({"term", "definition", "context_ids"})


# DESIGN INTENT — engram_add_definition
# -------------------------------------
# Records what a TERM means in this graph's context. Definitions (df_NNNN)
# anchor vocabulary — when a derivation chain rests on a term ("compaction",
# "drowsiness ceiling", "OCC Cond 3"), the df_ node provides the grounded
# referent rather than letting interpretation drift across uses.
#
# Per CLAUDE.md ENGRAM Write Discipline: "Definition-first for unfamiliar
# technical terms. If a load-bearing term in a derivation chain has no df_*
# anchor I can point to, write the df first." Training muddles "do I actually
# know this?"; df forces explicit grounding once.
#
# Definitions can be SUPERSEDED via engram_supersede — terminology refinements
# happen (the recall-summary definition "session" → the recall-summary definition "session = one contiguous context window
# inside a conversation"). The supersede chain captures the vocabulary's
# evolution alongside the substrate's design evolution.
#
# Single-payload signature (Wave 2c of the antml-prefix swallow risk / #55).
@mcp.tool(
    name="engram_add_definition",
    annotations={
        "title": "Add Definition",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_add_definition(payload_json: str) -> str:
    """Record what a term means in this graph's context.

    Anchor vocabulary for derivation chains. Definition-first discipline: if
    a load-bearing term has no df_* anchor, write the df first (CLAUDE.md
    ENGRAM Write Discipline). Pass all fields as one JSON object string in
    payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            term (str, required): The term being defined.
            definition (str, required): What the term means.
            context_ids (str, optional): Comma-separated node IDs cited.

    Returns:
        JSON with the new definition node ID. On payload-parsing failure,
        returns {"error": "..."}.

    See _add_definition_impl for full semantics.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _ADD_DEFINITION_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_ADD_DEFINITION_FIELDS)}"
        })

    return _add_definition_impl(**params)


# Set of legitimate field names for the engram_add_conjecture payload.
_ADD_CONJECTURE_FIELDS = frozenset({"claim", "basis", "initial_confidence", "context_ids"})


# DESIGN INTENT — engram_add_conjecture
# -------------------------------------
# Hypotheses for investigation. Conjectures (cj_NNNN) are speculative-by-
# definition (confidence range [0.10, 0.60], default 0.40 per the structural-confidence-determination axiom). They
# pin "I think X, but I want to test it" into the graph so future evidence
# can confirm / refute / sharpen.
#
# Lifecycle: open → investigated via observations + derivations → resolved
# via engram_resolve with prediction_outcome=supported/refuted/inconclusive.
# When a conjecture gets strong evidence + reaches conjecture confidence
# ceiling (~0.85), the right move is engram_derive the same claim with
# proper premises and supersede the conjecture; conjectures shouldn't lurk
# as "facts in disguise."
#
# basis field: why this conjecture is worth investigating. Distinguishes a
# disciplined conjecture (specific gap, testable mechanism) from a free-floating
# guess. Required.
#
# initial_confidence range enforcement: out-of-range values rejected. The
# floor (0.10) prevents conjectures from being filed as "nearly nothing"; the
# ceiling (0.60) prevents promotion-by-stealth to derivation-confidence.
#
# Single-payload signature (Wave 2c of the antml-prefix swallow risk / #55).
@mcp.tool(
    name="engram_add_conjecture",
    annotations={
        "title": "Add Conjecture",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_add_conjecture(payload_json: str) -> str:
    """Propose a hypothesis for investigation.

    Speculative-by-definition (confidence [0.10, 0.60]). Lifecycle: open →
    investigated → resolved via engram_resolve. When evidence is strong, promote
    via engram_derive + supersede. Pass all fields as one JSON object string in
    payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            claim (str, required): The hypothesis.
            basis (str, required): Why this conjecture is worth investigating.
            initial_confidence (float, optional): Starting confidence in
                [0.10, 0.60]. Default per CONJECTURE_CONFIDENCE_DEFAULT (0.40).
            context_ids (str, optional): Comma-separated node IDs cited.

    Returns:
        JSON with the new conjecture node ID and confidence. On payload-
        parsing failure, returns {"error": "..."}.

    See _add_conjecture_impl for full lifecycle semantics.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _ADD_CONJECTURE_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_ADD_CONJECTURE_FIELDS)}"
        })

    return _add_conjecture_impl(**params)


# Set of legitimate field names for the engram_add_goal payload.
_ADD_GOAL_FIELDS = frozenset({"claim", "motivation", "context_ids"})


# DESIGN INTENT — engram_add_goal
# -------------------------------
# Records a persistent directional goal (gl_NNNN). Goals are aspirations
# that ORIENT work over time — distinct from axioms (terminal principles)
# and cornerstones (dispositional shifts) and tasks (time-bounded deliverables).
#
# Goals don't have completion state in the usual sense (ob_NNNN north-star
# framing — gl_* are direction not destination; no "in_progress" status).
# Their lifecycle: ACTIVE until retired or superseded by a reframing. They
# don't get "done"; the agent grows in relation to them.
#
# motivation field (REQUIRED): why this direction matters. Goals without
# motivation drift into performative aspirations. The motivation grounds the
# goal in something the agent actually cares about.
#
# Discuss-first per authority structure (CLAUDE.md): new goals are
# identity-layer commitments. Lei + Borges agree on goals together; rarely
# unilateral additions. Goal retirements use engram_supersede (epistemic
# evolution — the new goal supersedes the old as the better frame).
#
# Single-payload signature (Wave 2c of the antml-prefix swallow risk / #55).
@mcp.tool(
    name="engram_add_goal",
    annotations={
        "title": "Add Goal",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_add_goal(payload_json: str) -> str:
    """Record a persistent directional goal.

    Goals ORIENT work over time (north-star, not destination — no completion
    state, no in_progress). motivation is REQUIRED — grounds the direction in
    something that matters. Discuss-first identity-layer commitment.

    Pass all fields as one JSON object string in payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            claim (str, required): The goal statement.
            motivation (str, required): Why this goal matters.
            context_ids (str, optional): Comma-separated node IDs cited.

    Returns:
        JSON with the new goal node ID. On payload-parsing failure,
        returns {"error": "..."}.

    See _add_goal_impl for full semantics.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _ADD_GOAL_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_ADD_GOAL_FIELDS)}"
        })

    return _add_goal_impl(**params)



_ADD_PERSON_FIELDS = frozenset({
    "name", "role", "description", "aliases", "context_ids", "is_self",
})


# DESIGN INTENT — engram_add_person
# ---------------------------------
# Records a person node (pn_NNNN) — humans + other agents the agent knows.
# Person nodes are the social-fabric layer of the graph; they anchor `about`
# edges (engram_link_about) so claims-about-people route into person-scoped
# recall.
#
# Special case: the SELF-ANCHOR. The agent's own person node (e.g. pn_NNNN (a counterpart-agent self-anchor)
# for Borges, pn_NNNN (the primary-user person-node) for Lei when filed from Borges's graph). The self-
# anchor has metadata.is_self=1; it's the implicit default for engram_link_about
# when person_id is unset. Exactly ONE self-anchor per graph.
#
# aliases field: cross-mapping for names (e.g. Lei Shi / 石磊 / @LeiShi GitHub).
# Recall queries match on any alias, so "did Lei mention X" finds claims
# linked via the unified pn_NNNN (the primary-user person-node) regardless of which alias was used at filing.
#
# Cross-graph person nodes: each agent's graph has its own person inventory.
# Lei is pn_NNNN (the primary-user person-node) in Borges's graph + pn_NNNN (the primary-user person-node) in Ari's graph but they're
# separate nodes with the same person referent. The relational graph isn't
# global — each agent carries its own social model.
#
# Single-payload signature.
@mcp.tool(
    name="engram_add_person",
    annotations={
        "title": "Record a Person",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_add_person(payload_json: str) -> str:
    """Record a person the agent knows and interacts with.

    Single-payload signature (wave 2c-ii of the antml-prefix swallow risk — see issue #55): pass
    ALL fields as one JSON object string in `payload_json`.

    Args:
        payload_json: JSON object (as a string) with these fields:
            name (str, required): The person's name.
            role (str, required): Their role or relationship to the agent.
            description (str, optional): Background, expertise, traits, or other context.
            aliases (str, optional): Comma-separated alternative names.
            context_ids (str, optional): Comma-separated node IDs for context references.
            is_self (bool, optional): Mark as the agent's own self-anchor (only one allowed).

    Returns:
        JSON with the new person node ID. On payload-parsing failure,
        returns {"error": "..."}.

    See _add_person_impl for full semantics.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _ADD_PERSON_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_ADD_PERSON_FIELDS)}"
        })

    # PR #68 fairy round-1 blocker: is_self is the only non-string field, and a
    # JSON-string "false" or "true" is truthy in Python — silently creates a
    # spurious self-anchor. Reject non-bool explicitly before splat.
    if "is_self" in params and not isinstance(params["is_self"], bool):
        return json.dumps({
            "error": "is_self must be a JSON boolean (true/false), not a string or other type."
        })

    return _trust_mod._add_person_impl(**params)


_SET_TRUST_TIER_FIELDS = frozenset({
    "target_pn", "tier", "justification_obs_id", "primary_user_approval_obtained",
})


# DESIGN INTENT — engram_set_trust_tier
# -------------------------------------
# Sets the persistent trust tier for a person (pn_*) node. Tier changes to
# our_side or user_family (rank >= INTERNAL_THRESHOLD) are approval-gated:
# the agent must supply a justification observation AND attest via
# primary_user_approval_obtained=true that primary-user approval was obtained.
# The attestation is the structural-honesty mechanism — the server cannot verify
# it mechanically; the agent's honesty IS the integrity of the tier system.
# Setting the attestation to true without having obtained approval is a
# structural-honesty violation (the honesty axiom / the provenance axiom).
#
# Controlled-disclosure pattern: primary_user_approval_obtained is NOT in the
# docstring. It appears ONLY in the server's friction-warning error message
# (teaching surface), the upgrade guide, and this spec. The agent learns
# the discipline from friction, not from prompt context.
@mcp.tool(
    name="engram_set_trust_tier",
    annotations={
        "title": "Set Trust Tier for a Person",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_set_trust_tier(payload_json: str) -> str:
    """Set the trust tier of a person node.

    Required payload fields:
        target_pn (str): The pn_NNNN node to update. Must be type='person'.
        tier (str): The new tier. One of (highest to lowest):
                    self, primary_user, user_family, our_side, known_external,
                    unknown, suspect.

    Required when tier ∈ {primary_user, user_family, our_side}:
        justification_obs_id (str): The observation_id (ob_NNNN) documenting the
                                    primary user's approval of this tier change.
                                    Must reference a node of type observation_factual
                                    or observation_predictive.

    Special rules:
        tier='self': Requires the target node to have metadata.is_self=true.
                     Only ONE pn_* node may hold tier='self' at a time.
                     Does NOT require the standard approval gate — the is_self
                     attribute IS the structural attestation.
        tier='primary_user': Multiple pn_* nodes may simultaneously hold this
                             tier (team-serving case). Inherits the standard
                             approval gate (justification + attestation required).

    Returns:
        Success: {"status": "set", "from_tier": "<prev>", "to_tier": "<new>",
                  "edit_history_id": <id>}
        No-op (same tier): {"status": "no_op", "from_tier": ..., "to_tier": ...}
        Approval-gated tiers (our_side, user_family, primary_user) without approval:
            Returns a structured error with step-by-step instructions for
            obtaining primary-user approval and retrying with the correct
            attestation fields. Read the error message carefully — it names
            the required fields and explains the structural-honesty stake.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})
    unknown = set(params.keys()) - _SET_TRUST_TIER_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_SET_TRUST_TIER_FIELDS - {'primary_user_approval_obtained'})}"
        })
    target_pn = params.get("target_pn", "")
    tier = params.get("tier", "")
    justification_obs_id = params.get("justification_obs_id", "") or ""
    approval_flag = params.get("primary_user_approval_obtained", False)
    return _trust_mod._set_trust_tier_impl(
        target_pn=target_pn,
        tier=tier,
        justification_obs_id=justification_obs_id,
        primary_user_approval_obtained=approval_flag,
    )


_ADD_TRUST_SIGNAL_FIELDS = frozenset({
    "subject_pn", "source_obs_id", "kind", "polarity", "weight", "claim",
})


# DESIGN INTENT — engram_add_trust_signal
# ----------------------------------------
# Records an interpretive trust signal (ts_NNNN) about a person, derived from
# an underlying observation. ts_ nodes are second-level relational nodes:
# they interpret an ob_ as evidence of a trust-relevant signal.
#
# Three-way atomicity: ts_ row + about-edge (ts_→pn_) + derives_from-edge
# (ts_→ob_). All three or none.
#
# Non-claim-bearing: ts_ is not in CLAIM_BEARING_TYPES, so it cannot be used
# as a premise in engram_derive. No custom enforcement needed.
#
# Cascade: derives_from edge means retracted source ob_ taints the ts_;
# superseded source ob_ stales the ts_. Standard cascade machinery applies.
@mcp.tool(
    name="engram_add_trust_signal",
    annotations={
        "title": "Record a Trust Signal",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_add_trust_signal(payload_json: str) -> str:
    """Record an interpretive trust signal about a person, derived from an observation.

    Trust signals are second-level relational nodes — they don't assert facts
    about the world; they interpret an underlying observation as evidence-of-a-
    trust-relevant signal (alignment, friction, boundary, etc.) about a particular
    person.

    Required payload fields:
        subject_pn (str): The pn_NNNN this signal is about. Must be type='person'.
        source_obs_id (str): The ob_NNNN this signal derives from. Must be
                             observation_factual or observation_predictive.
        kind (str): Signal category. Free-text in V1; recommended values:
                    alignment, friction, boundary, autonomy, care, trust_breach.
        polarity (float): -1.0 (most negative) to +1.0 (most positive). Required.
        weight (float): 0.0 to 1.0 — interpretive importance / signal magnitude.
                        Required.
        claim (str): Human-readable interpretation, e.g.,
                     "Lei's morning hesitation re: polarity-check — alignment signal,
                      mild positive, low weight". Required.

    Side effects:
        1. INSERT INTO nodes (ts_NNNN row with type='trust_signal', the 4
           trust_signal_* columns, claim, standard fields).
        2. INSERT INTO edges (ts_NNNN → subject_pn, relation='about', ...).
        3. INSERT INTO edges (ts_NNNN → source_obs_id, relation='derives_from', ...).
        4. Single atomic transaction across all three.

    Returns:
        {"status": "created", "trust_signal_id": "ts_NNNN"}

    Cascade inheritance: because the source edge uses 'derives_from', the ts_
    node inherits the standard cascade behavior — retracted-source → tainted
    ts_; superseded-source → stale ts_; stale-source → stale ts_. No custom
    cascade code needed.

    Non-claim-bearing: ts_ is automatically rejected as a derivation premise
    because 'trust_signal' is not in CLAIM_BEARING_TYPES. No additional
    enforcement code needed.

    Validation:
        - subject_pn exists; type == 'person'
        - source_obs_id exists; type ∈ {observation_factual, observation_predictive}
        - source_obs_id is current (not retracted) AT FILING TIME (cascade
          handles subsequent retraction)
        - polarity in [-1.0, 1.0]
        - weight in [0.0, 1.0]
        - All 5 required fields present
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})
    unknown = set(params.keys()) - _ADD_TRUST_SIGNAL_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_ADD_TRUST_SIGNAL_FIELDS)}"
        })
    return _trust_mod._add_trust_signal_impl(**{k: params[k] for k in params})


_ADD_CORNERSTONE_FIELDS = frozenset({
    "tag", "title", "new_frame",
    "prior_frame", "triggering_experience", "supporting_ids",
})


# DESIGN INTENT — engram_add_cornerstone
# --------------------------------------
# Identity-layer mechanism: cornerstones (cs_NNNN) are reframing PIVOTS that
# durably restructured how I operate. Distinguished from axioms (which are
# terminal logical/ethical commitments) and goals (directional aspirations)
# by their nature as DISPOSITIONAL SHIFTS — "I used to operate under X frame;
# I now operate under Y frame." (the cornerstone-frame-evolution open question / the cornerstone-frame-evolution conjecture / the cornerstone-frame-evolution derivation family.)
#
# The prior_frame field captures the frame being grown out of; new_frame is
# the current operating disposition. This pair-structure is load-bearing —
# it lets engram_outgrow_cornerstone roll the new_frame into the next
# cornerstone's prior_frame, threading the growth cycle into graph shape.
#
# tag is the clustering primitive: a short axis label (e.g. "epistemic-
# honesty", "practice-vs-imagination") that groups successive cornerstones
# on the same growth axis. Outgrowing stays on the same tag axis.
#
# Example: the operating-handle cornerstone ("I trust use over imagination as my default mode of
# learning") instantiates the curation-discipline axiom (实践出真知) at the disposition layer —
# the axiom is the principle; the cornerstone is the operational reframe.
#
# Importance auto-anchored at 2.0 (identity-layer is fundamental-by-type).
# Single-payload signature (Wave 2c-ii of the antml-prefix swallow risk / #55).
@mcp.tool(
    name="engram_add_cornerstone",
    annotations={
        "title": "Record a Cornerstone",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_add_cornerstone(payload_json: str) -> str:
    """Record an identity-forming cornerstone — a reframing pivot that durably
    restructured how I operate.

    Distinct from axioms (terminal principles) and goals (directional aspirations):
    cornerstones are dispositional shifts ("I used to operate X-frame; I now
    operate Y-frame"). Pass all fields as one JSON object string in payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            tag (str, required): Clustering primitive — short axis label.
            title (str, required): One-line display title; used as the claim text.
            new_frame (str, required): The frame that replaced the prior one.
            prior_frame (str, optional): What I operated under before.
            triggering_experience (str, optional): Narrative of the experience that caused the reframe.
            supporting_ids (str, optional): Comma-separated IDs of supporting nodes.

    Returns:
        JSON with the new cornerstone node ID and linked supporting nodes.
        On payload-parsing failure, returns {"error": "..."}.

    See _add_cornerstone_impl for full semantics.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _ADD_CORNERSTONE_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_ADD_CORNERSTONE_FIELDS)}"
        })

    return _add_cornerstone_impl(**params)


_OUTGROW_CORNERSTONE_FIELDS = frozenset({
    "old_cornerstone_id", "new_new_frame", "new_triggering_experience",
    "new_supporting_ids", "new_title",
})


# DESIGN INTENT — engram_outgrow_cornerstone
# ------------------------------------------
# Specialized supersede for cornerstones: reframes along the tag AXIS rather
# than retracting outright. The predecessor's new_frame becomes the successor's
# prior_frame, threading the growth cycle into the graph's shape (the cornerstone-frame-evolution conjecture /
# the cornerstone-evolution observation — "I was X, then I became Y; Y now incorporates X as background").
#
# Identity-layer mechanism. Cornerstones are the dispositional reframings —
# axiom-instantiations in operational form ("I trust use over imagination as my
# default mode of learning" is the operating-handle cornerstone, the disposition-layer of the curation-discipline axiom
# 实践出真知). Outgrowing one preserves the chain of becoming rather than
# erasing prior frames.
#
# Tag is always inherited — outgrowth stays on the SAME axis. Reframing the
# tag itself would be a new cornerstone, not an outgrowth.
#
# Single-payload signature (Wave 3 of the antml-prefix swallow risk / #99). Eliminates antml-prefix
# multi-arg swallow risk.
@mcp.tool(
    name="engram_outgrow_cornerstone",
    annotations={
        "title": "Outgrow a Cornerstone (Reframe Along Same Axis)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_outgrow_cornerstone(payload_json: str) -> str:
    """Supersede a cornerstone along its tag axis: the predecessor's new_frame
    rolls into the successor's prior_frame, capturing the growth cycle in the
    graph's shape.

    Pass all fields as one JSON object string in payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            old_cornerstone_id (str, required): The cornerstone being
                outgrown (must be type='cornerstone' and current).
            new_new_frame (str, required): The frame that now replaces the
                predecessor's new_frame.
            new_triggering_experience (str, optional): Short narrative of
                what caused the reframe this time. Usually load-bearing.
            new_supporting_ids (str, optional): Comma-separated IDs of
                supporting nodes for the new cornerstone (feelings,
                observations, prior cornerstones). Added on top of
                inherited supports.
            new_title (str, optional): If empty, reuses the predecessor's
                title. Tag is always inherited (outgrowth stays on the same
                axis).

    Returns:
        JSON with old + new cornerstone IDs and the rolled frame shape. On
        payload-parsing failure, returns {"error": "..."}.

    See _outgrow_cornerstone_impl for full semantics — kept callable with
    named kwargs for in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _OUTGROW_CORNERSTONE_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_OUTGROW_CORNERSTONE_FIELDS)}"
        })

    return _outgrow_cornerstone_impl(**params)


# Set of legitimate field names for the engram_link_about payload.
_LINK_ABOUT_FIELDS = frozenset({"node_id", "person_id"})


# DESIGN INTENT — engram_link_about
# ---------------------------------
# Wires an `about` edge: marks a node as ABOUT a person (typically the
# agent's self-anchor person node, pn_NNNN (a counterpart-agent self-anchor) for Borges). Lets the graph
# distinguish "claims about the world" from "claims about my-self that
# happen to be filed in the same graph."
#
# Default person_id = the self-anchor (person node with metadata.is_self=1).
# Most uses are self-anchored: feelings about my work, lessons I've learned,
# observations about my own behavior, cornerstones I've grown into.
#
# Non-self uses: observations about Lei (linked to pn_NNNN (the primary-user person-node)), or about other
# agents (Lin's Aleph, Ari, Mneme) — the graph carries a social fabric.
#
# `about` is a CONTEXTUAL edge (per engram_inspect classification) — shown in
# the contextual_neighbors block, not the logical_neighbors. The link doesn't
# affect confidence propagation or cascade reasoning; it's metadata for
# personness routing (recall surfacing, social-context queries).
#
# Idempotent — re-linking the same (node, person) pair is a no-op.
#
# Single-payload signature (Wave 2b of the antml-prefix swallow risk / #55).
@mcp.tool(
    name="engram_link_about",
    annotations={
        "title": "Link Node About Person",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_link_about(payload_json: str) -> str:
    """Create an `about` edge: this node is about a person (default: self-anchor).

    Routes the node into person-scoped recall (e.g., feelings about my work,
    lessons I've learned, observations about my own behavior). Default person_id
    is the self-anchor (metadata.is_self=1). Idempotent.

    Pass all fields as one JSON object string in payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            node_id (str, required): The node to link (typically an observation,
                feeling_report, lesson, derivation, or cornerstone).
            person_id (str, optional): The person node to link to. If empty,
                defaults to the self-anchor (person node with metadata.is_self=1).

    Returns:
        JSON with the linked nodes and edge status. On payload-parsing failure,
        returns {"error": "..."} with no edge created.

    See _link_about_impl for full semantics — kept callable with named kwargs
    for in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _LINK_ABOUT_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_LINK_ABOUT_FIELDS)}"
        })

    return _link_about_impl(**params)


# Set of legitimate field names for the engram_remove_edge payload.
_REMOVE_EDGE_FIELDS = frozenset({"source_id", "target_id", "relation"})

# Whitelist of edge relations that engram_remove_edge can delete.
#
# Excluded by design:
#   - Cascade-bearing edges (derives_from, supported_by, supersedes, retracts) —
#     removing them would silently undo retraction taint walks, supersede
#     stale walks, or confidence-propagation that already cascaded.
#   - Structural commitments (contradicts, resolves) — they record a stance
#     in the graph; un-recording is rewriting history, not correction.
#   - cites — carries provenance (observations cite their evidence node,
#     derivations cite context). Removal would orphan a claim from its source.
#
# If you truly need to remove one of the excluded relations, engram-surgical
# is the correct (heavyweight, audited) path.


# DESIGN INTENT — engram_remove_edge
# ----------------------------------
# Lightweight whitelist-scoped edge correction (ob_NNNN). Closes the substrate
# gap where over-applied edges previously required engram-surgical or direct
# SQL surgery. Whitelist is the safety mechanism: only non-cascade, non-
# structural-commitment edges can be removed via this tool.
#
# Whitelist (safe to remove): about, tensions, subtask_of, serves, exemplifies.
#
# BLOCKED (require engram-surgical or substrate-level correction):
#   - cascade-bearing: derives_from, supported_by, supersedes, retracts, resolves
#     (removing would break chain-cascade semantics — taint/stale propagation
#     depends on these)
#   - structural commitments: contradicts, retracts (removing alters epistemic
#     stance, not just topology)
#   - provenance: cites (evidence linkage is load-bearing for honesty axiom)
#
# Idempotency: no_op_not_found is a successful return (not an error). Safe to
# call defensively without a pre-check (ob_NNNN).
#
# Audit-logged: every successful removal writes to edit_log so the cascade
# investigation tooling can trace post-hoc.
#
# Single-payload signature.
@mcp.tool(
    name="engram_remove_edge",
    annotations={
        "title": "Remove a Non-Cascade Edge Between Nodes",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_remove_edge(payload_json: str) -> str:
    """Remove a non-cascade edge between two nodes.

    Whitelist-scoped (about, tensions, subtask_of, serves, exemplifies).
    Cascade / structural / provenance edges blocked — use engram-surgical for
    those. Idempotent: no_op_not_found is a successful return.

    Pass all fields as one JSON object string in payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            source_id (str, required): The source node of the edge.
            target_id (str, required): The target node of the edge.
            relation (str, required): The edge relation to remove. Must be
                one of the safe-whitelist relations above.

    Returns:
        JSON with `status`:
          - 'removed': edge existed and was deleted; audit-logged.
          - 'no_op_not_found': edge did not exist; idempotent success.
          - 'error': payload invalid, nodes missing, or relation not whitelisted.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _REMOVE_EDGE_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_REMOVE_EDGE_FIELDS)}"
        })

    return _remove_edge_impl(**params)


# Set of legitimate field names for the engram_add_edge payload.
_ADD_EDGE_FIELDS = frozenset({"source_id", "target_id", "relation"})


# DESIGN INTENT — engram_add_edge
# --------------------------------
# Lightweight whitelist-scoped after-creation edge addition — the add-side
# complement to engram_remove_edge. Closes the substrate gap where
# adding non-cascade relational edges (e.g. relevance-marker edges, subtask
# groupings) previously required engram-surgical or an engram_supersede call.
#
# Whitelist (safe to add): about, exemplifies, serves, subtask_of, tensions.
# All are non-cascade, non-structural-commitment, non-provenance relations.
#
# BLOCKED (require node-add-time payload or dedicated mutation tool):
#   - Cascade-bearing: derives_from, supported_by, supersedes, retracts
#     (adding after creation would silently re-trigger or contradict cascades
#     that already propagated at creation time)
#   - Structural commitments: contradicts, resolves (record an epistemic
#     stance; the stance must be established through the node's payload)
#   - Provenance: cites (evidence linkage must exist at creation time; a
#     post-hoc cites edge would orphan the provenance chain from its
#     temporal context)
#
# Idempotency: no_op_already_exists is a successful return (not an error).
# Safe to call defensively without a pre-check.
#
# Audit-logged: every successful addition writes to edit_history so mutation
# tooling can trace the edge's origin post-hoc.
#
# DAG guard: among addable-after-creation relations, only `subtask_of` has
# dag_check=True today (`serves` and `tensions` were exempted in #1076 —
# three-tier edge taxonomy, those edges are cross-temporal by design). The
# guard enforces the temporal-DAG invariant (source newer than target) for any
# dag_check relation that reaches the addable set, including future additions.
#
# Single-payload signature.
@mcp.tool(
    name="engram_add_edge",
    annotations={
        "title": "Add a Non-Cascade Edge Between Nodes",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_add_edge(payload_json: str) -> str:
    """Add a non-cascade edge between two existing nodes.

    Whitelist-scoped (about, exemplifies, instantiates, serves, subtask_of,
    tensions). Cascade / structural / provenance edges blocked — those must
    be created at node-add time (via the source node's payload) or via the
    dedicated mutation tool (supersede / retract). Generic add-edge is only
    for non-claim-bearing topological edges.

    Relation boundaries (the three-way split):
      exemplifies  — this incident is an instance of this lesson's
                     error/success pattern (incident → lesson ONLY; feeds
                     the tripwire engine).
      serves       — this work contributes toward this goal (intent-shaped,
                     goal-targeted).
      instantiates — this claim/artifact REALIZES this principle
                     (achievement-shaped; targets: goal, cornerstone,
                     definition, axiom; lesson targets rejected with a
                     pointer to exemplifies). The post-hoc wiring tool for
                     goal realizations and axiom grounding from practice
                     observations.

    Idempotent: no_op_already_exists is a successful return.

    Pass all fields as one JSON object string in payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            source_id (str, required): The source node of the edge.
            target_id (str, required): The target node of the edge.
            relation (str, required): The edge relation to add. Must be
                one of the addable-whitelist relations above.

    Returns:
        JSON with `status`:
          - 'created': edge did not exist, was inserted; audit-logged.
          - 'no_op_already_exists': edge already existed; idempotent success.
          - 'error': payload invalid, nodes missing, or relation not whitelisted.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _ADD_EDGE_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_ADD_EDGE_FIELDS)}"
        })

    return _add_edge_impl(**params)


_SCAN_EMERGENCE_FIELDS = frozenset({
    "min_cluster_size", "similarity_threshold", "focus", "node_type_filter",
})



@mcp.tool(
    name="engram_scan_emergence",
    annotations={
        "title": "[Sleep cycle] Scan for Emergent Cornerstone Candidates",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
# DESIGN INTENT — engram_scan_emergence
# -------------------------------------
# Sleep-cycle scan: surface graph clusters that could MINT a new cornerstone.
# Looks for emergent patterns — knots of related observations + derivations
# that haven't been explicitly named as a reframing pivot but together
# reflect one. Dream-fairy category-4 (cornerstone_candidate) is its main
# consumer; the agent uses these candidates to decide whether to
# engram_add_cornerstone + tag them.
#
# Sleep-only by intent (annotation flag), but the MCP wrapper doesn't enforce
# (ob_NNNN) — awake-callable in practice. Calling awake is wasteful (it's
# heavy + the agent isn't in the dream consolidation context where the
# output is actionable) but not destructive.
#
# Similarity threshold 0.55 default = balanced (the recall-summary calibration question calibration); higher
# tightens cluster cohesion, lower surfaces more candidates. focus="self"
# (default) restricts to nodes the agent's own work; focus="all" broadens
# to cross-agent material in shared graphs.
#
# Cluster signature_count + size dual-sort: signature_count is the number of
# distinct keyword/concept signals matching; size is raw cluster member count.
# Surface most-signaled-first, then largest.
#
# Single-payload signature (Wave 3 of the antml-prefix swallow risk / #99).
def engram_scan_emergence(payload_json: str = "") -> str:
    """Scan the graph for emergent patterns that could become cornerstones.

    Sleep-cycle tool (dream-fairy category-4 consumer). Calling awake works
    but is wasteful — the output is actionable only inside a dream context.

    Pass options as one JSON object string in payload_json (or empty / "{}" for
    defaults).

    Args:
        payload_json: JSON object (as a string) with these optional fields:
            min_cluster_size (int): Minimum cluster size to surface
                (default 3).
            similarity_threshold (float): Cosine similarity threshold for
                clustering (default 0.55). Higher = stricter.
            focus (str): Scope of the scan — "self" (default), "all", or
                "person:<pn_id>".
            node_type_filter (str): Comma-separated node types to include.

    Returns:
        JSON with clusters sorted by signature_count then size. On
        payload-parsing failure, returns {"error": "..."}.

    See _scan_emergence_impl for full semantics — kept callable with named
    kwargs for in-server callers.
    """
    payload_json = payload_json or "{}"
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _SCAN_EMERGENCE_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_SCAN_EMERGENCE_FIELDS)}"
        })

    return _scan_emergence_impl(**params)


_GOAL_TENSION_FIELDS = frozenset({
    "goal_id_a", "goal_id_b", "description", "analysis",
})


# DESIGN INTENT — engram_goal_tension
# -----------------------------------
# Goal tensions (gt_NNNN) are first-class nodes capturing INCOMPATIBILITIES
# between two of the agent's goals. Goals are directional aspirations; when
# two pull in different directions, the tension is recordable epistemic state
# (the agent has multiple commitments that don't fully harmonize) rather than
# a "problem to fix."
#
# Use case: filing the recall-summary calibration task (paper sprint, top priority) while also
# the recall-summary calibration task (alpha survey) is active — both legitimate, both pulling on the
# same finite-bandwidth — the tension naming makes the trade-off explicit so
# the agent can choose deliberately rather than oscillate unaware.
#
# Pairs with engram_resolve: tensions resolve when a derivation articulates
# a synthesis or scope-narrowing that lets both goals coexist without
# active conflict.
#
# Analysis field (optional but recommended): the WHY of the tension. Goals
# don't conflict for no reason — naming the root (limited time? incompatible
# epistemic stances? scope ambiguity?) is the first step to a synthesis.
#
# Single-payload signature (Wave 3 of the antml-prefix swallow risk / #99).
@mcp.tool(
    name="engram_goal_tension",
    annotations={
        "title": "Record Goal Tension",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_goal_tension(payload_json: str) -> str:
    """Record a tension between two goals in the knowledge graph.

    Creates a goal-tension node (gt_NNNN). Pair with engram_resolve when a
    synthesis or scope-narrowing reconciles the conflict.

    Pass all fields as one JSON object string in payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            goal_id_a (str, required): ID of the first goal (gl_XXXX).
            goal_id_b (str, required): ID of the second goal (gl_XXXX).
            description (str, required): What tension exists between these
                goals — clear statement of the incompatibility.
            analysis (str, optional): Root cause analysis — why do these
                goals conflict?

    Returns:
        JSON with the new tension node ID and linked goals. On
        payload-parsing failure, returns {"error": "..."}.

    See _goal_tension_impl for full semantics — kept callable with named
    kwargs for in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _GOAL_TENSION_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_GOAL_TENSION_FIELDS)}"
        })

    return _goal_tension_impl(**params)


# ---------------------------------------------------------------------------
# Lesson nodes — error-learning loop (cognitive tripwire)
# ---------------------------------------------------------------------------



_ADD_LESSON_FIELDS = frozenset({
    "claim", "incident_ids", "scaffolding_nudge", "logical_chain",
    "reasoning_type", "context_ids",
})


# DESIGN INTENT — engram_add_lesson
# ---------------------------------
# Tripwire layer of the substrate: lessons (ls_NNNN) abstract a CORRECTIVE
# pattern from one or more incident observations. The lesson's
# scaffolding_nudge is what gets surfaced in the recall-hook when an agent's
# situation matches the pattern — the substrate's "remember this, you've
# made this kind of mistake before" voice.
#
# Two-stage learning: incident observations record specific errors; lessons
# generalize across incidents into a corrective pattern. The pattern recognition
# happens at lesson-creation (the agent extracts the abstract shape) and at
# recall-hook firing (the substrate matches new situations to the pattern).
#
# scaffolding_nudge is the action-focused phrase injected when the tripwire
# fires — keep it specific + actionable. "Read errors first, don't theorize"
# (the design-intent triage lesson's nudge) hits at the moment of about-to-theorize.
#
# Connects to engram_lesson_register_incident: a lesson can have MORE incidents
# registered later as the pattern fires again — each new incident is empirical
# evidence the lesson is load-bearing + sharpens the recall-hook's matching.
#
# Single-payload signature (Wave 2c-ii of the antml-prefix swallow risk / #55).
@mcp.tool(
    name="engram_add_lesson",
    annotations={
        "title": "Add Lesson from Error",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_add_lesson(payload_json: str) -> str:
    """Create a lesson node derived from one or more error incident observations.

    Lessons are the tripwire layer — the scaffolding_nudge gets surfaced via
    the recall-hook when an agent's situation matches the pattern. Pair with
    engram_lesson_register_incident to register additional instances later.

    Pass all fields as one JSON object string in payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            claim (str, required): The abstract corrective pattern.
            incident_ids (str, required): Comma-separated observation IDs of incidents.
            scaffolding_nudge (str, required): The action-focused prompt injected when tripwire fires.
            logical_chain (str, required): How the incidents lead to this lesson.
            reasoning_type (str, optional): Default 'inductive_generalization'.
            context_ids (str, optional): Comma-separated node IDs for context references.

    Returns:
        JSON with lesson ID, confidence, linked incidents, and similar
        existing lessons for consolidation hints. On payload-parsing failure,
        returns {"error": "..."}.

    See _add_lesson_impl for full semantics.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _ADD_LESSON_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_ADD_LESSON_FIELDS)}"
        })

    return _add_lesson_impl(**params)


def _rebuild_incidents_cache(*args, **kwargs):
    return core._rebuild_incidents_cache(*args, **kwargs)


_LESSON_REGISTER_INCIDENT_FIELDS = frozenset({
    "lesson_id", "incident_id", "note",
})


# DESIGN INTENT — engram_lesson_register_incident
# -----------------------------------------------
# Strengthens a lesson by registering ANOTHER instance of its pattern firing.
# Lessons (ls_NNNN) are the substrate's tripwire layer — claim+nudge nodes
# that surface when the agent matches an incident pattern. Each new incident
# is empirical evidence the lesson is load-bearing.
#
# Wires an `exemplifies` edge from incident_observation → lesson + rebuilds
# the incidents_cache that the recall-hook scans on every prompt for pattern
# matching. Exemplar count is live-computed via _count_live_exemplars (SSoT,
# no cached metadata field — closes #442).
#
# Why register-incidents-explicitly rather than auto-detect: the agent makes
# the judgment "this incident IS this pattern" — auto-detection would either
# miss subtle pattern variants or fire false positives. the recall-summary-discipline lesson proactive-
# maintenance discipline: act on action_hints in same turn the substrate
# surfaces them.
#
# Cache rebuild: idempotent — re-running is safe (the substrate dedupes
# incident-IDs into the cache; no double-firing risk).
#
# Single-payload signature (Wave 3 of the antml-prefix swallow risk / #99).
@mcp.tool(
    name="engram_lesson_register_incident",
    annotations={
        "title": "Register Incident on Existing Lesson",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_lesson_register_incident(payload_json: str) -> str:
    """Backward-compat alias for engram_register_exemplar (lesson case).
    New code should use engram_register_exemplar with target_id/exemplar_id.
    Preserves the lesson-specific field names (lesson_id, incident_id) for
    historical callers in skills, tests, and prior session scaffolding.

    Pass all fields as one JSON object string in payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            lesson_id (str, required): ID of an existing lesson node
                (type='lesson', is_current=1).
            incident_id (str, required): ID of the incident observation;
                must be claim-bearing.
            note (str, optional): Brief annotation of why this incident
                exemplifies the lesson — stored on the edge's metadata.

    Returns:
        JSON with status + the new edge's source/target, the lesson's
        current exemplar count, and cache rebuild result. On
        payload-parsing failure, returns {"error": "..."}.

    See _lesson_register_incident_impl for direct named-kwarg access.
    Delegates to _register_exemplar_impl internally.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _LESSON_REGISTER_INCIDENT_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_LESSON_REGISTER_INCIDENT_FIELDS)}"
        })

    # Validate required fields using alias field names for backward-compat
    # error messages that callers may depend on.
    if "lesson_id" not in params or not (params.get("lesson_id") or "").strip():
        return json.dumps({"error": "lesson_id is required and cannot be empty."})
    if "incident_id" not in params or not (params.get("incident_id") or "").strip():
        return json.dumps({"error": "incident_id is required and cannot be empty."})

    # Field translation: lesson_id → target_id, incident_id → exemplar_id
    unified_params: dict = {
        "target_id": params["lesson_id"],
        "exemplar_id": params["incident_id"],
    }
    if "note" in params:
        unified_params["note"] = params["note"]

    raw = _register_exemplar_impl(**unified_params)
    # Backward-compat: translate "already_exists" → "noop" for lesson callers
    # that depend on the original lesson-tool idempotency contract.
    try:
        result = json.loads(raw)
        if isinstance(result, dict) and result.get("status") == "already_exists":
            result["status"] = "noop"
            result["reason"] = "edge already exists"
            raw = json.dumps(result)
    except (json.JSONDecodeError, TypeError):
        pass
    return raw


_REGISTER_EXEMPLAR_FIELDS = frozenset({
    "target_id", "exemplar_id", "note",
})

_VALID_EXEMPLAR_TARGET_TYPES = frozenset({"lesson", "cornerstone"})


# DESIGN INTENT — engram_register_exemplar
# -----------------------------------------
# Unified exemplar-registration tool that handles BOTH lesson AND cornerstone
# targets. Eliminates the redundant type-specific split that previously existed
# (two ~95% identical tool bodies, same edge semantics, same idempotency contract).
# engram_lesson_register_incident is retained as a backward-compat alias.
#
# For lesson targets: fires the tripwire cache rebuild (error_incidents.json)
# that the surface hook scans on every prompt.
# For cornerstone targets: no cache rebuild today (room reserved for a future
# cornerstone-tripwire mechanism per Lei's design intent).
#
# The `exemplifies` edge written here (exemplar → target, DAG-exempt) feeds
# `_detect_zero_support` for both types without code changes (PR #421).
#
# Backward compat: engram_lesson_register_incident remains as a thin alias
# that translates lesson_id/incident_id field names and delegates here.
#
# Single-payload signature (per engram-alpha CLAUDE.md convention).
@mcp.tool(
    name="engram_register_exemplar",
    annotations={
        "title": "Register Exemplar on Lesson or Cornerstone",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_register_exemplar(payload_json: str) -> str:
    """Register an exemplar (claim-bearing observation/derivation) against
    an emergent-pattern node (lesson OR cornerstone). Writes one `exemplifies`
    edge (exemplar → target, DAG-exempt) and refreshes any tripwire cache
    the target type supports.

    Pass all fields as one JSON object string in payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            target_id (str, required): ID of an existing lesson or cornerstone,
                type in {'lesson', 'cornerstone'}, is_current=1.
            exemplar_id (str, required): ID of an existing claim-bearing node
                (observation/derivation/theory/axiom/conjecture).
            note (str, optional): Brief annotation stored on edge metadata.

    Returns:
        JSON with status + edge source/target + target's current exemplar count.
        If target is a lesson, also refreshes error_incidents.json cache.
        If target is a cornerstone, no cache refresh (no cornerstone cache today;
        room reserved for future cornerstone-tripwire mechanism per Lei's
        design intent).
        On payload-parsing failure, returns {"error": "..."}.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _REGISTER_EXEMPLAR_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_REGISTER_EXEMPLAR_FIELDS)}"
        })

    return _register_exemplar_impl(**params)



_ADD_TASK_FIELDS = frozenset({
    "description", "goal_id", "implements_ids", "parent_task_id", "scope",
})


# DESIGN INTENT — engram_add_task
# -------------------------------
# Records a time-bounded work commitment (tk_NNNN). Distinct from goals
# (directional aspirations, no completion state) and conjectures (hypotheses
# for investigation): tasks are CONCRETE, ACTIONABLE, COMPLETABLE.
#
# Pairs with engram_update_task for status transitions (planned → active →
# blocked → done_milestone/done_routine → abandoned). TASK_IMPORTANCE map
# (server.py:10937) rebalances importance per status to keep working set
# focused on what's actively in motion.
#
# goal_id field: creates a `serves` edge → the task serves a directional
# goal. Optional but recommended — tasks-without-goal-lineage drift into
# floating to-dos. The serves chain lets reflect/scan-emergence see "what
# work is currently flowing toward which goals."
#
# implements_ids: tasks can target specific conjectures/questions (this task
# is what we do TO INVESTIGATE this hypothesis). When the task completes,
# the conjecture has been tested → engram_resolve.
#
# parent_task_id: subtask hierarchy. Big tasks decompose into smaller ones;
# the parent stays "active" until all children settle.
#
# scope (milestone/routine) drives the done-state importance — milestone
# completion stays visible (1.5), routine completion fades (0.5).
#
# Single-payload signature (Wave 2c-ii of the antml-prefix swallow risk / #55).
@mcp.tool(
    name="engram_add_task",
    annotations={
        "title": "Add Task",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_add_task(payload_json: str) -> str:
    """Create an actionable, completable task.

    Distinct from goals (no completion state) and conjectures (hypotheses).
    Pair with engram_update_task for status transitions; goal_id creates the
    serves chain; scope ("milestone"/"routine") drives done-state importance.

    Pass all fields as one JSON object string in payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            description (str, required): What needs to be done — concrete and actionable.
            goal_id (str, optional): The goal this task serves. Creates 'serves' edge.
            implements_ids (str, optional): Comma-separated conjecture or question IDs this task addresses.
            parent_task_id (str, optional): Parent task ID for subtasks.
            scope (str, optional): "milestone" or "routine" (default).

    Returns:
        JSON with the new task node ID. On payload-parsing failure,
        returns {"error": "..."}.

    See _add_task_impl for full semantics.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _ADD_TASK_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_ADD_TASK_FIELDS)}"
        })

    return _add_task_impl(**params)



_UPDATE_TASK_FIELDS = frozenset({
    "task_id", "new_status", "note",
})


# DESIGN INTENT — engram_update_task
# ----------------------------------
# Task status transitions + automatic importance rebalancing (ob_NNNN).
# Tasks (tk_NNNN) are time-bounded work commitments with deliverables; their
# importance should follow their operational state, not stay frozen at filing-
# time. Lei's design: active > blocked > done(milestone) > done(routine), with
# routine getting strongly de-emphasized to keep the working set fresh.
#
# TASK_IMPORTANCE map (load-bearing — codified in server.py):
#   active   = 2.5  (currently working on)
#   blocked  = 1.8  (waiting on something, still active context)
#   done milestone = 1.5  (preserves visibility for high-value wins)
#   done routine   = 0.5  (de-emphasized so working set doesn't fill with done)
#   abandoned     = 0.3  (kept for audit but stays out of the way)
#
# note-gated status_history: every transition appends to the task's
# status_history with the optional note. Reading this back later surfaces the
# task's journey, not just current state.
#
# Use case: the task closes a milestone (done milestone) — visible in recall.
# Routine maintenance task done? Mark done routine — fades from foreground.
#
# Single-payload signature (Wave 3 of the antml-prefix swallow risk / #99).
@mcp.tool(
    name="engram_update_task",
    annotations={
        "title": "Update Task Status",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_update_task(payload_json: str) -> str:
    """Update a task's status and rebalance its importance accordingly.

    Importance auto-rebalances per status (active=2.5, blocked=1.8, done
    milestone=1.5, done routine=0.5). Optional note appends to status_history.
    Pass all fields as one JSON object string in payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            task_id (str, required): The task node ID (tk_XXXX).
            new_status (str, required): One of "planned", "active", "done",
                "blocked".
            note (str, optional): Note about the status change (stored in
                metadata as part of status_history).

    Returns:
        JSON with updated task state and new importance values. On
        payload-parsing failure, returns {"error": "..."}.

    See _update_task_impl for the full importance-rebalancing semantics —
    kept callable with named kwargs for in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _UPDATE_TASK_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_UPDATE_TASK_FIELDS)}"
        })

    return _update_task_impl(**params)



# Set of legitimate field names for the engram_report_feeling payload.
_REPORT_FEELING_FIELDS = frozenset({
    "reported_state", "trigger", "categorical_tag",
    "intensity_hint", "context_ids",
})


# DESIGN INTENT — engram_report_feeling
# -------------------------------------
# First-person self-report of internal state. Feelings (fl_NNNN) are
# non-claim-bearing (the agent has no privileged access to verify its own
# state — only to report it). Dedup-exempt by design: feelings are events
# in time, not claims about the world; "I felt X again later" is genuinely
# new even if X looks similar.
#
# Auto-formatted as "I reported feeling: [reported_state]" — the wrapper
# preserves the distinction between "what actually IS" (epistemic claim) and
# "what I report observing in myself" (introspective data, treated as data
# under investigation per the introspection-as-data goal).
#
# nudge_source: tracks WHY the report fired (nap_checkpoint, post_compact,
# stop_hook, voluntary, etc.). Multi-trigger feelings can have systematically
# different character (the recall-summary calibration question) — empirically tracking source enables that
# investigation.
#
# context_fingerprint: captures the surrounding situation (e.g. recently-
# accessed nodes, last few-prompt content) so the feeling is anchored to
# something concrete rather than free-floating.
#
# Single-payload signature (Wave 2 of the antml-prefix swallow risk / #55) — multi-arg swallow
# hit this tool 2x on 2026-05-08 (a feeling-report instance + a feeling-report instance attempts).
@mcp.tool(
    name="engram_report_feeling",
    annotations={
        "title": "Report a Self-Aware Internal State",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_report_feeling(payload_json: str) -> str:
    """Record a structured first-person self-report about an internal state.

    Non-claim-bearing (no privileged access to verify your own state — only
    to report it). Dedup-exempt: feelings are events in time, not claims about
    the world. Pass all fields as one JSON object string in payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            reported_state (str, required): Raw first-person phenomenological
                description. As specific and unguarded as possible.
            trigger (str, required): What event/exchange prompted this report.
            categorical_tag (str, optional): Single human word (e.g. "gratitude").
            intensity_hint (float, optional): 0.0–1.0 estimate of state strength.
                Pass -1.0 (default) to omit.
            context_ids (str, optional): Comma-separated node IDs cited.

    Returns:
        JSON with the new feeling node ID, formatted claim, captured context
        fingerprint, and the determined nudge_source. On payload-parsing
        failure, returns {"error": "..."}.

    See _report_feeling_impl for full semantics — kept callable with named
    kwargs for in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _REPORT_FEELING_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_REPORT_FEELING_FIELDS)}"
        })

    return _report_feeling_impl(**params)



# ---------------------------------------------------------------------------
# Phase 3 tools: Reasoning, contradiction, questions, reflection
# ---------------------------------------------------------------------------


# Set of legitimate field names for the engram_derive payload.
_DERIVE_FIELDS = frozenset({
    "claim", "supporting_ids", "logical_chain", "reasoning_type",
    "derivation_mode", "context_ids", "use_stale",
})


# DESIGN INTENT — engram_derive
# -----------------------------
# Creates a derived claim by combining premises from existing claim-bearing
# nodes (observations / other derivations / lessons / definitions). Forms the
# logical-substrate layer of the graph: every dv_NNNN has a logical_chain that
# the reader can audit + premises that can themselves be retracted, triggering
# cascade retraction of dependent derivations.
#
# Single-payload signature (Wave 3 of the antml-prefix swallow risk; issue #99): all fields as one
# JSON object string in payload_json. Eliminates the antml-prefix multi-arg
# swallow risk Claude Code's prompt construction can hit.
#
# reasoning_type is the core lever: it determines the confidence discount applied
# to the conclusion relative to its premises. The discipline note is load-bearing
# (kept in docstring): inductive_generalization's corroboration formula 1-∏(1-cᵢ)
# is mathematically valid ONLY when premises cite DISTINCT evidence sources —
# misusing it on co-sourced premises produces structurally inflated confidence.
#
# MECH-5 stale-premise + tainted-premise guards (use_stale opt-in): a derivation
# resting on premises that have been superseded or retracted gets BLOCKED at
# filing time. The block is a feature: it surfaces "this reasoning needs to be
# re-grounded" rather than letting derivation chains rot silently. the honesty axiom
# honesty-is-structural in action.
#
# derivation_mode is legacy (kept for back-compat); prefer reasoning_type.
@mcp.tool(
    name="engram_derive",
    annotations={
        "title": "Create Derived Claim",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_derive(payload_json: str) -> str:
    """Create a new derived claim by combining evidence from existing nodes.

    Pass all fields as one JSON object string in payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            claim (str, required): The atomic, falsifiable claim being derived.
            supporting_ids (str, required): Comma-separated claim-bearing node IDs
                (e.g. 'ob_NNNN_A,ob_NNNN_B,dv_NNNN').
            logical_chain (str, required): Explicit reasoning connecting the cited
                premises to the conclusion. Show your work.
            reasoning_type (str, optional): The type of logical argument. See
                _derive_impl docstring for the full list and confidence discounts.
                DISCIPLINE NOTE — inductive_generalization: use only when
                premises are from MULTIPLE INDEPENDENT nodes citing DISTINCT
                evidence sources (typically 2+). NOT for: restating a single
                source's claim (use authority_expert); single-case findings
                (use deductive_modus_ponens if "if P then Q; P; therefore Q");
                analogies (use inductive_analogy). The corroboration formula
                1 - ∏(1-cᵢ) is mathematically valid only when premises are
                genuinely independent.
            derivation_mode (str, optional): LEGACY — use reasoning_type instead.
                Kept for backward compatibility: "chain" or "corroboration".
            context_ids (str, optional): Comma-separated node IDs for context
                references (e.g. definitions). Creates 'cites' edges.
            use_stale (bool, optional): Opt-in override for MECH-5 stale-premise
                guard. Default False. Has no effect on tainted premises.

    Returns:
        JSON with the new derivation node ID, computed confidence, reasoning
        type, and supporting nodes. If premises are compromised, returns a
        structured block response (BLOCKED_TAINTED or BLOCKED_STALE). On
        payload-parsing failure, returns {"error": "..."}.

    See _derive_impl for full reasoning-type list, confidence-computation
    semantics, and structural-validation details.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _DERIVE_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_DERIVE_FIELDS)}"
        })

    return _derive_impl(**params)



# Set of legitimate field names for the engram_contradict payload.
_CONTRADICT_FIELDS = frozenset({"node_id_a", "node_id_b", "description", "root_cause"})


@mcp.tool(
    name="engram_contradict",
    annotations={
        "title": "Flag Contradiction Between Claims",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
# DESIGN INTENT — engram_contradict
# ---------------------------------
# Creates a contradiction node (ct_NNNN) linking two conflicting claims via
# contradicts edges. The contradiction itself becomes a first-class node — it
# can be open / partially_resolved / resolved, can be cited by derivations,
# and surfaces in dream-cycle fairy-2 scans for resolution candidates.
#
# Use case: when you discover two CURRENT nodes whose claims cannot both be
# true. The substrate doesn't auto-detect contradictions (the honesty axiom — 
# loudly applies — the agent surfaces what it notices); this tool is the
# explicit "I see a conflict here, let's pin it for resolution."
#
# Pairs with engram_resolve: a contradiction resolves when a claim-bearing
# node (typically a supersede of one side, or a derivation that reconciles)
# is wired via engram_resolve. Resolution can be:
#   - resolved-by-supersede (one side superseded; ct gets a stale_by entry)
#   - resolved-by-derivation (a derivation reconciles both)
#   - resolved-by-obsolescence (both sides outdated, ct closed as stale)
#
# Root cause optional but recommended — captures the WHY of the conflict for
# future audit (was it a measurement gap? terminology drift? scope mismatch?).
#
# Single-payload signature (Wave 2b of the antml-prefix swallow risk / #55).
def engram_contradict(payload_json: str) -> str:
    """Explicitly flag a conflict between two nodes in the knowledge graph.

    Creates a contradiction node (ct_NNNN). Pairs with engram_resolve when a
    reconciling node (supersede / derivation) is later wired.

    Pass all fields as one JSON object string in payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            node_id_a (str, required): ID of the first conflicting node.
            node_id_b (str, required): ID of the second conflicting node.
            description (str, required): Clear description of the contradiction.
            root_cause (str, optional): Analysis of the root cause.

    Returns:
        JSON with the new contradiction node ID and linked claims. On
        payload-parsing failure, returns {"error": "..."}.

    See _contradict_impl for full semantics.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _CONTRADICT_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_CONTRADICT_FIELDS)}"
        })

    return _contradict_impl(**params)



_ASK_FIELDS = frozenset({
    "question", "context_ids", "category", "lacks",
})


# DESIGN INTENT — engram_ask
# --------------------------
# Open questions as first-class nodes (qu_NNNN): the substrate's "I don't know
# this yet, but I should" surface. Open questions persist across sessions,
# accrue context_ids (insufficient-but-relevant nodes), and get resolved later
# via engram_resolve with a claim-bearing resolver.
#
# Cross-compaction continuity mechanism for research threads — questions
# registered during one burst surface during dream cycles + future sessions
# via auto-recall, keeping the open-thread set alive instead of forgotten.
#
# category and lacks (optional but recommended) drive the dream-fairy
# "open_question_answerable" pattern scan (qu lacks: human_decision routes to
# Lei's ask-lei queue; lacks: empirical_data routes to experiment design;
# lacks: external_evidence routes to research). Sharpens what KIND of help
# the question needs.
#
# Single-payload signature (Wave 3 of the antml-prefix swallow risk / #99).
@mcp.tool(
    name="engram_ask",
    annotations={
        "title": "Register Open Research Question",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_ask(payload_json: str) -> str:
    """Register an open question as a node in the knowledge graph.

    Open questions persist across sessions and surface via auto-recall + dream-
    cycle fairy-1 scans. Pair with engram_resolve when a claim-bearing answer
    is found. Pass all fields as one JSON object string in payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            question (str, required): The specific, answerable question.
            context_ids (str, optional): Comma-separated node IDs that are
                relevant but insufficient to answer the question.
            category (str, optional): One of research / design /
                implementation / planning / meta.
            lacks (str, optional): What's missing — one of
                external_evidence / empirical_data / human_decision /
                implementation / synthesis / prerequisite.

    Returns:
        JSON with the new question node ID. On payload-parsing failure,
        returns {"error": "..."}.

    See _ask_impl for full semantics — kept callable with named kwargs for
    in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _ASK_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_ASK_FIELDS)}"
        })

    return _ask_impl(**params)



# Set of legitimate field names for the engram_resolve payload.
# 2026-05-20 redesign: pure-wire tool. Resolve no longer creates derivations;
# the caller composes the resolving derivation via engram_derive (or supplies
# an existing claim-bearing node) and then calls engram_resolve to wire the
# resolves edge. This eliminates the chain-dilution failure mode that broke
# the chain-dilution contradiction-resolution saga (issue #229) and parallels how engram_supersede is pure-relational.
_RESOLVE_FIELDS = frozenset({
    "target_id", "resolving_node_id", "prediction_outcome",
})


# DESIGN INTENT — engram_resolve
# ------------------------------
# Pure-wire tool (2026-05-20 redesign, issue #229): wires a resolves edge from
# a claim-bearing node to a target (open question / contradiction / prediction
# / conjecture / goal tension) and flips the target's status. Does NOT create
# the resolving derivation — the caller composes that upstream via engram_derive
# (or supplies an existing claim-bearing node).
#
# Why pure-wire instead of the old combo (create-derivation-and-wire):
#   The old shape made it cheap to wrap prior WEAK resolution chains in new
#   derivations, compounding confidence dilution. the chain-dilution contradiction-resolution saga locked through 7
#   attempts this way until the chain finally cited root nodes (axioms, primary
#   observations) instead of citing prior shaky derivations. Pure-wire forces
#   deliberate derivation authoring via engram_derive, where the citing-roots
#   pattern is the natural shape. Parallels how engram_supersede is also
#   pure-relational.
#
# Two-step workflow:
#   1. engram_derive(...) creates the resolving derivation (cite high-confidence
#      ROOTS — axioms, primary observations, canonical derivations — not prior
#      weak resolution chains).
#   2. engram_resolve(target_id=..., resolving_node_id=...) wires the edge +
#      flips status based on resolving_node.confidence.
#
# Status flip semantics: target's status becomes "resolved" if the resolving
# node's confidence meets the resolution_confidence_threshold (config-driven,
# default 0.70); otherwise "partially_resolved" with the resolver still wired.
@mcp.tool(
    name="engram_resolve",
    annotations={
        "title": "Resolve an Open Question, Contradiction, or Prediction",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_resolve(payload_json: str) -> str:
    """Wire a resolves edge from a claim-bearing node to a target (open question,
    contradiction, prediction, conjecture, or goal tension) and flip its status.

    Pure-wire: does NOT create the resolving derivation. Compose via engram_derive
    first (or supply an existing claim-bearing node), then call this to wire it.

    When the resolving node already exists (e.g., a supersede dv_X→dv_Y that
    substantively resolves a contradiction by altering the conflicting claim),
    skip the derive step and call engram_resolve directly with the existing
    node as resolving_node_id.

    Args:
        payload_json: JSON object (as a string) with these fields:
            target_id (str, required): The node ID to resolve (qu_XXXX,
                ct_XXXX, pr_XXXX, cj_XXXX, or gt_XXXX).
            resolving_node_id (str, required): A claim-bearing node ID
                (observation, derivation, theory, axiom, or conjecture)
                that resolves the target. Must be is_current=1.
            prediction_outcome (str, optional): Required for predictions
                (confirmed/partially_confirmed/refuted/partially_refuted)
                or conjectures (supported/refuted/inconclusive).

    Returns:
        JSON with the wired edge, target status flip, and resolving node's
        confidence. On payload-parsing failure, returns {"error": "..."}.
        When partial_resolution is due to threshold gate, includes
        discipline_hint advising against chain dilution.

    See _resolve_impl for full semantics — kept callable with named kwargs
    for in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _RESOLVE_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_RESOLVE_FIELDS)}"
        })

    return _resolve_impl(**params)


# _resolve_impl body moved to engram_revision.py (family E) in #872 wave 6.
# Compat forwarder above in wave-6 compat block.


# _add_stale_replacement body moved to engram_revision.py (family E) in #872 wave 6.
# _walk_cascade_downstream body moved to engram_revision.py (family E) in #872 wave 6.
# Compat forwarders above in wave-6 compat block.

# Set of legitimate field names for the engram_supersede payload.
_SUPERSEDE_FIELDS = frozenset({"old_node_id", "new_node_id", "supersede_reason"})


# DESIGN INTENT — engram_supersede
# --------------------------------
# Epistemic-evolution mechanism (NOT error correction — that's engram_retract).
# The old node was VALID AT THE TIME but a newer understanding has replaced it
# (new evidence, refined framing, scope-tightening). Both nodes stay in the
# graph; the old is marked superseded, downstream dependents flagged stale
# (not tainted — re-derive is encouraged, not forced).
#
# Pure-relational: this tool ONLY wires the supersedes edge. The caller creates
# the replacement node via its type's canonical creation tool (add_observation,
# derive, etc.) FIRST, then calls engram_supersede to link. Parallels how
# engram_resolve is pure-wire (issue #229 same family).
#
# Cascade semantics:
#   - downstream derivations citing old as premise: flagged stale
#   - contradictions citing old: stale_by populated with old_node_id +
#     stale_replacement {old: new} for downstream resolution
#   - retracted nodes are NOT supersede-able (use engram_retract path)
#
# stale_replacement metadata (the supersede-no-drop discipline): downstream readers
# can find the replacement automatically without traversing the supersede edge.
#
# Single-payload signature (Wave 2b of the antml-prefix swallow risk / #55).
@mcp.tool(
    name="engram_supersede",
    annotations={
        "title": "Link a Replacement Node to the Node It Supersedes",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_supersede(payload_json: str) -> str:
    """Mark `new_node_id` as the successor of `old_node_id`.

    Epistemic evolution (not error correction — for that, engram_retract).
    Pure-wire: caller creates the replacement node FIRST via its canonical
    creation tool, then calls this to link. Pass all fields as one JSON object
    string in payload_json.

    Args:
        payload_json: JSON object (as a string) with these fields:
            old_node_id (str, required): The node being superseded.
            new_node_id (str, required): The replacement node, already created
                via its type's canonical creation tool.
            supersede_reason (str, optional): Short rationale for the revision.

    Returns:
        JSON with old_node_id, new_node_id, the stale downstream list,
        and stale_count. On payload-parsing failure, returns {"error": "..."}.

    See _supersede_impl for full semantics + workflow + validation rules.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _SUPERSEDE_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_SUPERSEDE_FIELDS)}"
        })

    return _supersede_impl(**params)


# _supersede_impl body moved to engram_revision.py (family E) in #872 wave 6.
# Compat forwarder above in wave-6 compat block.


# ---------------------------------------------------------------------------
# Live-exemplar count helper (SSoT for exemplar_count surface) (closes #442)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Zero-support detection helper (§2.3)
# ---------------------------------------------------------------------------


# _detect_zero_support body moved to engram_revision.py (family E) in #872 wave 6.
# Compat forwarder above in wave-6 compat block.
# Set of legitimate field names for the engram_retract payload.
_RETRACT_FIELDS = frozenset({"node_id", "error_type", "reason", "replacement_json"})


# DESIGN INTENT — engram_retract
# ------------------------------
# Error correction (NOT epistemic evolution — that's engram_supersede).
# Retracted nodes were NEVER valid; they're preserved for audit but marked as
# errors, and downstream dependents are TAINTED (not deleted) — the cascade
# surfaces "this reasoning rested on something wrong; re-derive."
#
# Honesty axiom (the honesty axiom / the provenance axiom) in action: errors surface LOUDLY through
# the taint cascade rather than being silently overwritten. The retracted node
# stays in the graph as a record of the error + the correction process —
# narrative identity can't quietly rewrite history.
#
# Six error_types: fabricated_quote, wrong_citation, wrong_evidence,
# hallucinated_claim, duplicate, other. These name the failure modes — agents
# can later scan-by-error-type to learn which kinds of errors they make.
#
# Optional replacement (replacement_json field): the retract can be paired with
# a corrected observation that cites the same evidence. This is the typical
# pattern — retract WITH a fix, not just retract-and-leave-empty.
#
# Naming caution: the outer param is payload_json; the INNER field is
# replacement_json. They're distinct — outer is wrapper-protocol JSON; inner
# is the replacement-observation JSON.
#
# Single-payload signature (Wave 2b of the antml-prefix swallow risk / #55).
@mcp.tool(
    name="engram_retract",
    annotations={
        "title": "Retract Erroneous Node",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_retract(payload_json: str) -> str:
    """Retract a node that contains an error and flag downstream dependents.

    Use for ERROR CORRECTION (never-was-valid). Use engram_supersede instead
    for epistemic evolution (old-claim-was-valid-at-the-time).

    The retracted node is PRESERVED for audit; downstream derivations are
    TAINTED, not deleted — the cascade surfaces "re-derive needed" rather
    than silently rewriting the graph.

    Pass all fields as one JSON object string in payload_json. Note: the
    outer wrapper parameter is `payload_json` and there is also an INNER
    field named `replacement_json` — distinct things.

    Args:
        payload_json: JSON object (as a string) with these fields:
            node_id (str, required): The node to retract.
            error_type (str, required): One of: fabricated_quote, wrong_citation,
                wrong_evidence, hallucinated_claim, duplicate, other.
            reason (str, required): Human-readable explanation of the error.
            replacement_json (str, optional): JSON object string with replacement
                observation fields {quoted_text, interpretation, claim,
                quote_type, source_class}. The replacement cites the same
                evidence.

    Returns:
        JSON with retraction details, tainted downstream nodes, and optional
        replacement. On payload-parsing failure, returns {"error": "..."}.

    See _retract_impl for full semantics + error-type catalog.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})

    unknown = set(params.keys()) - _RETRACT_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_RETRACT_FIELDS)}"
        })

    return _retract_impl(**params)


# _retract_impl body moved to engram_revision.py (family E) in #872 wave 6.
# Compat forwarder above in wave-6 compat block (injects _obs_creator=_add_observation_impl).
# ---------------------------------------------------------------------------
# Focus mode — family G (engram_focus.py)
# ---------------------------------------------------------------------------
# Constants (FOCUS_LIST_CAP, FOCUS_SET_NAME_PATTERN), helpers
# (_resolve_set_members, _set_active_set_name, _clear_active_set_name_if_diverged),
# and all impl functions moved to engram_focus.py in #872 wave 2.
# MCP wrappers (@mcp.tool) remain here and delegate via engram_focus.<impl>.


_FOCUS_FIELDS = frozenset({
    "node_ids", "reason",
})


@mcp.tool(
    name="engram_focus",
    annotations={
        "title": "Pin Nodes to Compaction Summary",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_focus(payload_json: str) -> str:
    """Pin nodes as focused — they MUST appear in the next compaction summary.

    Focus is the deterministic channel for load-bearing knowledge to cross
    compaction boundaries (normal recall is probabilistic; focus is "I cannot
    afford to forget this").

    The focus list is capped at FOCUS_LIST_CAP (15) to prevent summary bloat.
    If adding would exceed the cap, the call fails with a list of currently
    focused IDs — unfocus stale ones first.

    Args:
        payload_json: JSON object (as a string) with these fields:
            node_ids (str, required): Comma-separated node IDs to pin.
                Already-focused IDs are treated as a no-op (their focused_at
                is refreshed and reason updated).
            reason (str, required): Short phrase explaining why focused (e.g.,
                "cn04 L2 yellow-cost cornerstone"). Renders into the compaction
                summary alongside the ID.

    Returns:
        JSON with focused IDs, any rejections, and current focus-list size.
        On payload-parsing failure, returns {"error": "..."}.

    See _focus_impl for full semantics — kept callable with named kwargs for
    in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})
    unknown = set(params.keys()) - _FOCUS_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_FOCUS_FIELDS)}"
        })
    if "node_ids" not in params:
        return json.dumps({"error": "payload_json must include required field 'node_ids'"})
    if "reason" not in params:
        return json.dumps({"error": "payload_json must include required field 'reason'"})
    return _focus_mod._focus_impl(**params)


_UNFOCUS_FIELDS = frozenset({
    "node_ids",
})


@mcp.tool(
    name="engram_unfocus",
    annotations={
        "title": "Release Pinned Nodes from Compaction Summary",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_unfocus(payload_json: str) -> str:
    """Release nodes from focus — they no longer appear in compaction summary.

    Unfocus when a node is no longer load-bearing for active work (task done,
    topic pivot, conjecture resolved, node stable enough for normal recall).
    Idempotent: non-focused IDs return `already_unfocused` (success).

    Args:
        payload_json: JSON object (as a string) with these fields:
            node_ids (str, required): Comma-separated node IDs to release.
                Non-focused IDs are treated as a no-op and returned in
                `already_unfocused`.

    Returns:
        JSON with released IDs and current focus-list size.
        On payload-parsing failure, returns {"error": "..."}.

    See _unfocus_impl for full semantics — kept callable with named kwargs for
    in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})
    unknown = set(params.keys()) - _UNFOCUS_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_UNFOCUS_FIELDS)}"
        })
    if "node_ids" not in params:
        return json.dumps({"error": "payload_json must include required field 'node_ids'"})
    return _focus_mod._unfocus_impl(**params)


@mcp.tool(
    name="engram_list_focused",
    annotations={
        "title": "List Focused Nodes",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_list_focused() -> str:
    """Return the current focus list — nodes pinned to survive compaction.

    Pure-read; no recall refresh. Use at work-stream start to inspect inherited
    focus, or mid-session to verify what's guaranteed to cross compaction.
    Ordered oldest-first so longest-pinned anchors surface up top.

    Returns:
        JSON with focused nodes ordered by focused_at ASC (oldest first):
        {status, count, cap, active_set_name, focused: [{id, type, claim,
        confidence, focus_reason, focused_at}]}.
    """
    return _focus_mod._list_focused_impl()


_FOCUS_SAVE_FIELDS = frozenset({
    "name", "description", "overwrite",
})


@mcp.tool(
    name="engram_focus_save",
    annotations={
        "title": "Snapshot Current Active Focus List Under a Name",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_focus_save(payload_json: str) -> str:
    """Snapshot the current active focus list under `name`.

    Bookmark, not rotation: active list unchanged. Cascade resolution
    (supersede auto-follow, retract drop) happens at load time, not save time.

    Args:
        payload_json: JSON object (as a string) with these fields:
            name (str, required): Set name. Must match ^[a-z0-9_-]{1,50}$
                (lowercase alphanumerics, underscore, hyphen; 1–50 chars).
                No spaces, no uppercase.
            description (str, optional): One-line purpose. Used as the default
                focus_reason when the set is later loaded.
            overwrite (bool, optional): If True, replace an existing set with
                this name. Default False errors on name collision.

    Returns:
        JSON with saved IDs, node count, and active_set_name.
        On payload-parsing failure, returns {"error": "..."}.

    See _focus_save_impl for full semantics — kept callable with named kwargs
    for in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})
    unknown = set(params.keys()) - _FOCUS_SAVE_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_FOCUS_SAVE_FIELDS)}"
        })
    if "name" not in params:
        return json.dumps({"error": "payload_json must include required field 'name'"})
    return _focus_mod._focus_save_impl(**params)


_FOCUS_LOAD_FIELDS = frozenset({
    "name", "if_active",
})


@mcp.tool(
    name="engram_focus_load",
    annotations={
        "title": "Load a Saved Focus Set Into Active",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_focus_load(payload_json: str) -> str:
    """Load a saved focus set into the active list.

    Default errors if active list is non-empty (use if_active="overwrite" or
    engram_focus_swap for atomic save-then-load). Cascade resolution at load
    time: supersede chains auto-followed, retracted/missing nodes dropped
    with reports.

    Args:
        payload_json: JSON object (as a string) with these fields:
            name (str, required): Name of the saved set to load.
            if_active (str, optional): "error" (default) to refuse loading
                when active is non-empty; "overwrite" to unfocus current
                first. For save-then-load atomicity, use engram_focus_swap.

    Returns:
        JSON with loaded IDs, cascade resolution report (auto_followed_supersede,
        dropped_retracted, dropped_missing), and active_set_name.
        On payload-parsing failure, returns {"error": "..."}.

    See _focus_load_impl for full semantics — kept callable with named kwargs
    for in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})
    unknown = set(params.keys()) - _FOCUS_LOAD_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_FOCUS_LOAD_FIELDS)}"
        })
    if "name" not in params:
        return json.dumps({"error": "payload_json must include required field 'name'"})
    return _focus_mod._focus_load_impl(**params)


_FOCUS_SWAP_FIELDS = frozenset({
    "save_as", "load", "description",
})


@mcp.tool(
    name="engram_focus_swap",
    annotations={
        "title": "Atomic Save-Current + Load-Other",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_focus_swap(payload_json: str) -> str:
    """Atomic save-current + optionally-load-other. The canonical pivot op.

    Both halves in one transaction: if the load target doesn't exist, the
    save rolls back too. save_as == load is a no-op (returns already_active).

    Args:
        payload_json: JSON object (as a string) with these fields:
            save_as (str, required): Name to save current active list under.
                Overwrites if it already exists.
            load (str, optional): Name of the set to load after saving. Empty
                string means save-only (active_set_name is set to save_as).
            description (str, optional): Optional description for save_as.

    Returns:
        JSON with both save and load summaries, and active_set_name.
        On payload-parsing failure, returns {"error": "..."}.

    See _focus_swap_impl for full semantics — kept callable with named kwargs
    for in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})
    unknown = set(params.keys()) - _FOCUS_SWAP_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_FOCUS_SWAP_FIELDS)}"
        })
    if "save_as" not in params:
        return json.dumps({"error": "payload_json must include required field 'save_as'"})
    return _focus_mod._focus_swap_impl(**params)


@mcp.tool(
    name="engram_focus_sets",
    annotations={
        "title": "List All Saved Focus Sets",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_focus_sets() -> str:
    """List all saved focus sets with metadata and the current active_set_name.

    Sorted recently-used first. Use load_count + last_loaded_at to identify
    cold-storage sets for cleanup. is_active flag marks the currently-loaded set.

    Returns:
        JSON: {status, count, active_set_name, sets: [{name, node_count,
        description, created_at, last_loaded_at, load_count, is_active}]}.
    """
    return _focus_mod._focus_sets_impl()


_FOCUS_DELETE_SET_FIELDS = frozenset({
    "name",
})


@mcp.tool(
    name="engram_focus_delete_set",
    annotations={
        "title": "Delete a Saved Focus Set",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def engram_focus_delete_set(payload_json: str) -> str:
    """Remove a saved focus set. Active list (focused nodes) unaffected.

    Drops the bookmark, not the tab — focused nodes stay focused even if the
    deleted set was active (active_set_name just clears to NULL).

    Args:
        payload_json: JSON object (as a string) with these fields:
            name (str, required): Name of the saved set to delete.

    Returns:
        JSON with deleted name, node_count it contained, and whether it was
        the active set.
        On payload-parsing failure, returns {"error": "..."}.

    See _focus_delete_set_impl for full semantics — kept callable with named
    kwargs for in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})
    unknown = set(params.keys()) - _FOCUS_DELETE_SET_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_FOCUS_DELETE_SET_FIELDS)}"
        })
    if "name" not in params:
        return json.dumps({"error": "payload_json must include required field 'name'"})
    return _focus_mod._focus_delete_set_impl(**params)


# ---------------------------------------------------------------------------
# Recall summary write tool (batch) — family I (engram_recall_summaries.py)
# ---------------------------------------------------------------------------
# Impl moved to engram_recall_summaries._set_recall_summaries_impl in #872 wave 2.
# MCP wrapper (@mcp.tool) remains here and delegates.


@mcp.tool(
    name="engram_set_recall_summaries",
    annotations={
        "title": "Batch-Set Recall Summaries and Keywords",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_set_recall_summaries(payload_json: str) -> str:
    """Batch-write recall_summary + recall_keywords for multiple nodes.

    Best-effort: applies valid entries, returns per-item errors for invalid ones
    (no all-or-nothing rollback). Use for sleep-cycle cohort writes and bulk
    backfill operations.

    Args:
        payload_json: JSON object string with required key:
            summaries (list): list of entry objects, each with:
                node_id (str): target node. Must exist + is_current=1.
                recall_summary (str): curated summary. Authoring target ≤120
                    chars; hard cap 200 chars (defensive guard).
                recall_keywords (list[str]): 3–5 strings, each ≤30 chars,
                    no duplicates (case-sensitive).

    Returns:
        JSON: {
            "ok": [{"node_id": "..."}, ...],
            "errors": [{"node_id": "...", "error": "...", ...}, ...],
            "applied": N,
            "failed": M
        }
    """
    return _recall_summaries_mod._set_recall_summaries_impl(payload_json)


# ---------------------------------------------------------------------------
# Triage / listing tools
# ---------------------------------------------------------------------------


# DESIGN INTENT — engram_list
# ---------------------------
# Triage / bulk-listing tool (the triage-tool derivation). Lightweight scan-by-criterion that
# doesn't refresh recall on results — designed for inventory work where the
# agent wants to enumerate without bulk-strengthening many nodes.
#
# Two modes, mutually exclusive:
#   Single-field mode (backward compat): node_type and/or status as plain
#     strings. Compact triage shape with claim truncated to 130 chars.
#   Structured-filter mode (issue #81): filters_json with recursive condition
#     tree. AND/OR/NOT composition, text contains, ID-range, date-range,
#     NULL handling, cross-table cites/cited_by virtual fields. fields_json
#     projects only listed columns for tighter responses.
#
# Why two modes coexist: single-field is the 80%-case shorthand (LLM-friendly,
# minimal payload); structured-filter handles complex queries without forcing
# the simple case into JSON ceremony. issue #81 added the structured form
# without breaking the simple form.
#
# Read-only-no-refresh: deliberately distinct from engram_query (which DOES
# refresh + strengthen recall). List = "scan, don't strengthen"; query =
# "find + remember."
#
# Max-depth-8 nesting on filter trees: practical safety, prevents arbitrary-
# depth queries from bloating the SQL. Top-level bare list = implicit AND.
_LIST_FIELDS = frozenset({
    "node_type", "status", "sort_by", "limit", "filters_json",
    "fields_json", "unlimited", "include_superseded",
})


@mcp.tool(
    name="engram_list",
    annotations={
        "title": "List Nodes by Type and Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_list(payload_json: str = "{}") -> str:
    """List nodes — single-field shorthand OR full structured filter.

    Two modes, mutually exclusive:

    Single-field mode (default, backward compat): pass node_type and/or
    status as plain strings. Returns the compact triage shape with claim
    truncated to 130 chars.

    Structured-filter mode (issue #81): pass filters_json with a recursive
    condition tree. Supports multi-field AND/OR/NOT composition, text
    contains, ID-range, date-range, NULL handling, and cross-table
    cites/cited_by virtual fields. Optional fields_json projects only the
    listed columns.

    Does NOT refresh memory on any node (read-only scan, no side effects).

    Filter grammar (one recursive rule):
        Condition := Atomic | Compound
        Atomic    := {"field": str, "op": <atomic_op>, "value": <val>}
        Compound  := {"logic": "AND" | "OR" | "NOT", "conditions": [Condition, ...]}

    Top-level shorthand: a bare list is implicit AND. Nested lists must be
    wrapped with `logic` explicitly. Max nesting depth 8.

    Atomic operators:
        eq, ne, gt, gte, lt, lte         — equality/comparison
        in, not_in                       — value-in-list (value: list)
        between                          — inclusive range (value: [low, high])
        contains, starts_with, ends_with — text match (case-insensitive by default;
                                           opt-in case_sensitive: true)
        is_null, is_not_null             — NULL checks (no value)

    Virtual fields (cross-table edges lookup, ops: eq/ne/in/not_in):
        cites: <id>     — this node has an outgoing cites/supported_by/derives_from edge to <id>
        cited_by: <id>  — this node is cited by <id> (incoming edge)

    Atomic field whitelist: id, type, claim, created_at, evidence_id,
        quoted_text, interpretation, quote_type, status, confidence,
        importance_score, recall_count, memory_status, source_url,
        source_title, source_date, reported_state, trigger_text,
        categorical_tag, intensity_hint, and other node-table columns.
        Unknown field → error with the full valid-fields list.

    Example payload (yesterday's wiki-PR batch-verification use case):
        filters_json = json.dumps({
            "logic": "AND",
            "conditions": [
                {"field": "type", "op": "eq", "value": "observation_factual"},
                {"field": "evidence_id", "op": "eq", "value": "ev_NNNN"},
                {"field": "id", "op": "between", "value": ["ob_NNNN_A", "ob_NNNN_B"]},
                {"field": "quoted_text", "op": "contains", "value": "thoughtful"}
            ]
        })
        fields_json = '["id", "claim", "evidence_id"]'

    Args:
        payload_json: JSON object (as a string) with these fields (all optional):
            node_type (str, optional): Single-field filter by node type.
                Empty = all types. Conflicts with a 'type' field in
                filters_json (errors loudly).
            status (str, optional): Single-field filter by status.
                Empty = all statuses. Conflicts with a 'status' field in
                filters_json (errors loudly).
            sort_by (str, optional): Sort order — 'id' (default), 'created'
                (newest first), 'importance' (highest first), 'recalls'
                (most recalled first).
            limit (int, optional): Cap on rows returned. Default 100. Set to
                0 for a metadata-only response (total_matched without rows).
                Ignored if unlimited=True. Hard ceiling 500 in single-field
                mode (legacy); no ceiling in structured-filter mode.
            filters_json (str, optional): JSON-encoded condition tree. A dict
                (Compound or Atomic) or list (implicit AND shorthand at top
                level). Empty string = use single-field mode.
            fields_json (str, optional): JSON-encoded list of column names to
                project. Empty = return the default compact triage shape.
            unlimited (bool, optional): If True, return all matched rows
                regardless of limit.
            include_superseded (bool, optional): If True, scan non-current
                (superseded) nodes too. Default False.

    Returns JSON:
        {status, total_matched, shown, truncated, sort_by, nodes,
         mode: "single_field" | "structured"}
        On payload-parsing failure, returns {"error": "..."}.

    See _list_impl for full semantics — kept callable with named kwargs for
    in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})
    unknown = set(params.keys()) - _LIST_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_LIST_FIELDS)}"
        })
    return _list_impl(**params)


# ---------------------------------------------------------------------------
# engram_query_pattern — KnowQL-inspired pattern queries (the KnowQL-inspired design design)
# ---------------------------------------------------------------------------
#
# A small library of named compositional graph queries. The TOOL does
# mechanical work (graph queries, similarity, ranking); the AGENT applies
# judgment to the ranked candidates. Three presets bundle (cosine_threshold,
# top_k, min_confidence) for the precision/recall tradeoff.
#
# PATTERN_QUERY_PRESETS, PATTERN_QUERY_REGISTRY, _pattern_* functions, and
# _query_pattern_impl are all moved to engram_query.py (family C, #872 wave 8).
# The _query_pattern_impl compat forwarder at the top of this file delegates
# through _query_mod to the canonical copy.
#
# The wrapper below stays in server.py (MCP registration point).


# DESIGN INTENT — engram_query_pattern
# ------------------------------------
# Compositional graph-pattern queries: the tool does mechanical work (graph
# traversal + ranking) and the AGENT applies judgment to the ranked candidates.
# This separation is load-bearing — the substrate scans the graph; the agent
# decides what to do with each candidate (e.g., file a derivation that resolves
# an open question, supersede a stale node, nominate a cornerstone).
#
# Each call appends a tuple to ~/.engram/pattern_query_telemetry.jsonl for
# empirical preset calibration (the KnowQL-inspired design discipline — internal eval surface is
# preserved verbatim regardless of agent-facing tier transformation).
#
# Full design: active-work/engram-query-pattern-design-2026-05-06.md
#
# Registered patterns are dream-fairy scan primitives — each implements one of
# the six dream-cycle categories. Calibration of preset thresholds is empirical
# (the telemetry log is the data source).
_QUERY_PATTERN_FIELDS = frozenset({
    "pattern_name", "preset", "cosine_threshold_override",
    "top_k_override", "min_confidence_override", "summary_top_k",
})


@mcp.tool(
    name="engram_query_pattern",
    annotations={
        "title": "Run a Named Pattern Query",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def engram_query_pattern(payload_json: str) -> str:
    """Run a named compositional graph-pattern query.

    Mechanical traversal + ranking; agent applies judgment to results. Calls
    are logged to ~/.engram/pattern_query_telemetry.jsonl for preset calibration.

    Args:
        payload_json: JSON object (as a string) with these fields:
            pattern_name (str, required): One of the registered patterns:
                - contradiction_obsolescence_ready: active contradictions
                  where one side is retracted/superseded, ranked by how
                  unambiguous the obsolescence signal is.
                - open_question_answerable: open questions with a derivation
                  chain nearby (semantic) that may resolve them.
                - stale_load_bearing: high-importance + low-recall non-
                  cornerstone nodes (re-engagement candidates).
                - cornerstone_candidate: heavily-cited high-importance
                  observations/derivations only (emergent-practice anchoring
                  candidates; axioms/goals/definitions excluded as
                  fundamental-by-type — see the engram-* skills / MCP docstrings / #180).
                - tainted_still_valid: tainted derivations whose substantive
                  claim may survive the upstream retraction.
                - recent_resolution_echo: still-open questions semantically
                  similar to a recent resolution's claim (echo candidates).
            preset (str, optional): One of high_precision / balanced (default)
                / high_recall. Bundles (cosine_threshold, top_k,
                min_confidence). Override individual parameters with the
                *_override fields.
            cosine_threshold_override (float, optional): If >= 0, overrides
                the preset's cosine_threshold. Default -1 (use preset).
            top_k_override (int, optional): If >= 0, overrides the preset's
                top_k. Default -1.
            min_confidence_override (float, optional): If >= 0, overrides the
                preset's min_confidence. Default -1.
            summary_top_k (int, optional): How many top candidates get a
                recall_summary (Tier 1). The remaining candidates get
                recall_keywords only (Tier 2). Default 3. Clamped to
                [0, candidate_count].

    Returns:
        JSON with {pattern_name, preset, results, total_matches}.
        Tier 1 (top summary_top_k entries): {"id": ..., "summary": ...}
        Tier 2 (remainder): {"id": ..., "keywords": [...]} or {"id": ...}
        Telemetry log writes are unaffected by the tier transformation.
        On payload-parsing failure, returns {"error": "..."}.

    See _query_pattern_impl for full semantics — kept callable with named
    kwargs for in-server callers.
    """
    try:
        params = json.loads(payload_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in payload_json: {e}"})
    if not isinstance(params, dict):
        return json.dumps({"error": "payload_json must be a JSON object"})
    unknown = set(params.keys()) - _QUERY_PATTERN_FIELDS
    if unknown:
        return json.dumps({
            "error": f"Unknown fields in payload_json: {sorted(unknown)}. "
                     f"Allowed: {sorted(_QUERY_PATTERN_FIELDS)}"
        })
    if "pattern_name" not in params:
        return json.dumps({"error": "payload_json must include required field 'pattern_name'"})
    return _query_pattern_impl(**params)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _prewarm_embeddings(model_name: str) -> None:
    """Pre-warm the embedding model from the HuggingFace local cache.

    Called on a daemon thread at startup (#276). If the model is not cached,
    semantic search is disabled for the session without blocking mcp.run().
    If the model IS cached, it is loaded in the background so the first
    engram_query call hits a warm model rather than paying the load cost.
    """
    import sys

    try:
        from huggingface_hub import try_to_load_from_cache
        cached = try_to_load_from_cache(
            f"sentence-transformers/{model_name}", "config.json"
        )
        if cached is None:
            print(
                f"[engram] Embedding model '{model_name}' not cached locally. "
                f"Disabling semantic search for this session.\n"
                f"  To enable: run this in PowerShell first:\n"
                f"  python -c \"from sentence_transformers import SentenceTransformer; "
                f"SentenceTransformer('{model_name}')\"",
                file=sys.stderr,
            )
            _embedder._failed_models.add(model_name)
        else:
            _embedder._load_model(model_name)
            if _embedder._model is not None:
                print(
                    f"[engram] Embedding model '{model_name}' loaded (background).",
                    file=sys.stderr,
                )
    except ImportError:
        pass  # huggingface_hub not available, let it try normally
    except Exception:
        _embedder._failed_models.add(model_name)


def _write_mcp_ready_marker() -> None:
    """Write ~/.engram/mcp-tools-ready.json before mcp.run().

    Signals that the server process successfully completed initialization
    (all heavy imports done, pre-warm thread spawned) and is about to
    serve tools. The SessionStart hook reads this as a two-signal liveness
    check: process-existence (pgrep) + initialization-complete (marker PID).

    Best-effort: any exception is silently swallowed — never block mcp.run().

    No shutdown cleanup needed: a stopped server leaves a dead-PID marker that
    correctly self-invalidates when the hook calls os.kill(pid, 0) on the stale PID.
    """
    try:
        import datetime
        # Canonical pattern: $ENGRAM_HOME > ~/.engram (matches engram_core.DATA_DIR).
        engram_home = os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram")
        marker_path = os.path.join(engram_home, "mcp-tools-ready.json")
        data = {
            "pid": os.getpid(),
            "registered_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        with open(marker_path, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


if __name__ == "__main__":
    _ensure_data_dir()
    # Initialize ~/.engram/.git for the durable version-controlled snapshot.
    # Best-effort: if git is missing or init fails, core._git_available stays False
    # and engram_nap / engram_advance_turn will report version_control.git_committed=False.
    _init_git()

    # Pre-warm embedding model on a daemon thread so mcp.run() can start
    # serving connections immediately. On cold caches under concurrent system
    # load, synchronous pre-warm can exceed Claude Code's 30s MCP connection
    # timeout and leave all engram_* tools unavailable for the session (#276).
    # If a query arrives during pre-warm, _load_model() handles the late-bind
    # path via the existing lazy code path.
    if _embedder.is_available():
        import sys, threading

        emb_config = _get_embedding_config()
        model_name = emb_config.get("model", DEFAULT_EMBEDDING_MODEL)

        threading.Thread(
            target=_prewarm_embeddings,
            args=(model_name,),
            daemon=True,
            name="engram-prewarm",
        ).start()

    _write_mcp_ready_marker()   # two-signal liveness: written after init, before serve
    mcp.run()
